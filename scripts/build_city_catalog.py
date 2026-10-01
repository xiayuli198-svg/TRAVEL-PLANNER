"""把人工整理的城市文案与坐标合并成运行时城市目录。

输入
    data/city_content/*.json                     各大区文案（人工/子任务整理）
    src/travel_planner/data/city_coords.json     DataV 抓取的地级市中心点
    src/travel_planner/data/city_coords_extra.json 直辖市/港澳台等手工坐标
    src/travel_planner/data/regions.json         大区划分
输出
    src/travel_planner/data/city_catalog.json    运行时唯一数据源

同时做三件校验并打印报告：
    1. 坐标补齐：按「同省 + 前两字相同」匹配 DataV 中心点，找不到就报错；
    2. 铁路可达：查时刻表库的 stations 表，判断该城市能否直接作为规划点，
       不能的给出替代落脚城市（如 黔东南 → 凯里）或标记 rail=false；
    3. 几何校验：坐标必须落在所属省的轮廓内（用 web/data/china.geo.json）。

用法：
    python scripts/build_city_catalog.py                # 生成 + 校验报告
    python scripts/build_city_catalog.py --check-only   # 只校验
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DATA = PROJECT_ROOT / "src" / "travel_planner" / "data"
CONTENT_DIR = PROJECT_ROOT / "data" / "city_content"
GUIDE_DIR = PROJECT_ROOT / "data" / "city_guides"
CATALOG_PATH = SRC_DATA / "city_catalog.json"
GEO_PATH = PROJECT_ROOT / "web" / "data" / "china.geo.json"
DB_PATH = PROJECT_ROOT / "data" / "timetable.db"

KINDS = {"博物馆", "古迹", "自然", "街区", "古镇", "美食", "夜景", "演出",
         "海岛", "雪山", "湖泊", "寺庙", "园林", "工业", "温泉", "边境", "主题乐园"}

# 适合晚上游玩的城市：卡片优先放夜景照片（内容里已写「夜」的会自动命中，这里是补充名单）
NIGHT_EXTRA = {
    "上海", "重庆", "香港", "澳门", "广州", "深圳", "西安", "长沙", "成都", "武汉",
    "南京", "杭州", "苏州", "厦门", "青岛", "哈尔滨", "沈阳", "天津", "济南",
    "南昌", "福州", "南宁", "贵阳", "昆明", "兰州", "乌鲁木齐", "喀什", "西宁",
    "拉萨", "大理", "丽江", "湘西", "桂林", "三亚", "海口", "台北", "高雄",
    "酒泉", "大同", "洛阳", "开封", "晋中", "张家界", "黄山", "西双版纳", "延边",
    "丹东", "大连", "宁波", "温州", "潮州", "汕头", "泉州", "无锡", "扬州", "镇江",
    "绍兴", "嘉兴", "湖州", "景德镇", "上饶", "宜昌", "呼和浩特", "银川", "太原",
    "石家庄", "郑州", "合肥", "佛山", "珠海",
}


def is_night_city(entry: dict) -> bool:
    """这座城市是否更适合晚上玩（决定卡片用夜景照还是白天照）。"""
    if entry["name"] in NIGHT_EXTRA:
        return True
    text = " ".join([
        " ".join(entry.get("tags") or []),
        " ".join((h.get("name") or "") + (h.get("kind") or "")
                 for h in (entry.get("highlights") or [])),
        entry.get("intro") or "", entry.get("halfday") or "",
        entry.get("oneday") or "", entry.get("note") or "",
    ])
    return "夜" in text

# 州/盟名在 12306 站名表里不出现时，用驻地城市作为规划落脚点。
RAIL_SEAT = {
    "大兴安岭": "加格达奇",
    "巴音郭楞": "库尔勒",
    "伊犁": "伊宁",
    "凉山": "西昌",
    "迪庆": "香格里拉",
    "红河": "蒙自",
    "黔东南": "凯里",
    "黔南": "都匀",
    "黔西南": "兴义",
    "湘西": "吉首",
    "海西州": "德令哈",
    "海北州": "海晏",
}

# 确认没有铁路客运的城市（机场/公路可达），由脚本复核，避免误判。
NO_RAIL = {"舟山", "阿里", "果洛州", "玉树州", "昌都", "甘南", "海南州", "黄南州", "甘孜",
           "澳门", "台北", "新北", "基隆", "新竹", "桃园", "台中", "台南", "高雄", "嘉义",
           "南投", "彰化", "苗栗", "云林", "屏东", "宜兰", "花莲", "台东", "澎湖"}


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 坐标
def norm(name: str) -> str:
    value = (name or "").strip()
    for suffix in ("市", "地区", "自治州", "盟", "特别行政区", "省"):
        if value.endswith(suffix) and len(value) > len(suffix):
            return value[: -len(suffix)]
    return value


def coord_index() -> Dict[str, List[dict]]:
    """province -> 城市中心点列表。"""
    index: Dict[str, List[dict]] = {}
    for source in ("city_coords.json", "city_coords_extra.json"):
        data = load_json(SRC_DATA / source) or {}
        for item in data.get("cities") or []:
            index.setdefault(item["province"], []).append(item)
    return index


def find_coord(index: Dict[str, List[dict]], name: str, province: str) -> Optional[dict]:
    pool = index.get(province) or []
    target = norm(name)
    exact = [c for c in pool if norm(c["name"]) == target]
    if exact:
        return exact[0]
    head = target[:2]
    if len(head) < 2:
        return None
    fuzzy = [c for c in pool if norm(c["name"])[:2] == head]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if fuzzy:
        # 多个候选时取名字最长的（通常是最具体的那个）
        return sorted(fuzzy, key=lambda c: -len(c["name"]))[0]
    return None


# ---------------------------------------------------------------- 铁路
def rail_probe(db: Path) -> Tuple[Optional[sqlite3.Connection], Dict[str, str]]:
    if not db.exists():
        return None, {}
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    def city_has(q: str) -> bool:
        return bool(conn.execute("SELECT 1 FROM stations WHERE city=? LIMIT 1", (q,)).fetchone())

    def name_has(q: str) -> bool:
        return bool(conn.execute("SELECT 1 FROM stations WHERE name=? LIMIT 1", (q,)).fetchone())

    def contained(q: str) -> bool:
        """站名/所属城市里确实包含这个名字（避免 阿里 → 阿里河 这种误判）。"""
        row = conn.execute(
            "SELECT 1 FROM stations WHERE name LIKE ? OR city LIKE ? LIMIT 1",
            (f"%{q}%", f"%{q}%")).fetchone()
        return bool(row)

    def resolves(q: str) -> bool:
        return city_has(q) or name_has(q) or contained(q)

    return conn, {"resolves": resolves}  # type: ignore[dict-item]


def resolve_rail(probe, name: str, province: str) -> Tuple[bool, str, str]:
    """返回 (是否可铁路直达, 规划用的地点名, 说明)。"""
    if name in NO_RAIL:
        return False, name, "无铁路客运"
    if probe is None:
        return True, RAIL_SEAT.get(name, name), "未检测（缺少时刻表库）"
    resolves = probe["resolves"]
    if resolves(name):
        return True, name, ""
    short = norm(name)
    if short != name and resolves(short):
        return True, short, f"按 {short} 匹配"
    seat = RAIL_SEAT.get(name)
    if seat and resolves(seat):
        return True, seat, f"经 {seat}"
    if seat:
        return False, name, f"驻地 {seat} 也无车站"
    return False, name, "站名表里没有匹配城市"


# ---------------------------------------------------------------- 几何
def point_in_ring(lon: float, lat: float, ring: Sequence[Sequence[float]]) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if (y1 > lat) != (y2 > lat):
            xin = (x2 - x1) * (lat - y1) / (y2 - y1 + 1e-12) + x1
            if lon < xin:
                inside = not inside
    return inside


def geocheck() -> Dict[str, List[List[List[float]]]]:
    geo = load_json(GEO_PATH) or {}
    return {p["short"]: p["rings"] for p in geo.get("provinces") or []}


def inside_province(rings_by_province, province: str, lon: float, lat: float) -> bool:
    for ring in rings_by_province.get(province) or []:
        if point_in_ring(lon, lat, ring):
            return True
    return False


# ---------------------------------------------------------------- 主流程
def main() -> int:
    parser = argparse.ArgumentParser(description="生成 city_catalog.json")
    parser.add_argument("--check-only", action="store_true", help="只校验，不写文件")
    args = parser.parse_args()

    regions = load_json(SRC_DATA / "regions.json") or {}
    province_to_region = {}
    for region in regions.get("regions") or []:
        for province in region.get("provinces") or []:
            province_to_region[province] = region["name"]

    coords = coord_index()
    rings = geocheck()
    probe = rail_probe(DB_PATH)[1] if DB_PATH.exists() else None

    # 完整攻略（data/city_guides/*.json，人工/子任务整理）
    # 注意：notes/problems 要在读攻略之前就初始化 —— 「攻略重复」这条分支
    # 以前从没走到过，一走到就 UnboundLocalError（后加的 zz_deep_dive.json 覆盖旧稿时踩到）。
    notes: List[str] = []
    problems: List[str] = []
    guides: Dict[str, dict] = {}
    for gpath in sorted(GUIDE_DIR.glob("*.json")) if GUIDE_DIR.exists() else []:
        gdata = load_json(gpath) or {}
        for gname, guide in (gdata.get("guides") or {}).items():
            if gname in guides:
                notes.append(f"攻略重复：{gname}（{gpath.name} 覆盖了前一份）")
            guides[gname] = guide
    guide_missing: List[str] = []

    cities: List[dict] = []
    seen = set()

    content_files = sorted(CONTENT_DIR.glob("*.json"))
    if not content_files:
        print(f"没有找到文案文件：{CONTENT_DIR}/*.json", file=sys.stderr)
        return 1

    for path in content_files:
        payload = load_json(path) or {}
        region_name = payload.get("region") or ""
        for raw in payload.get("cities") or []:
            name = (raw.get("name") or "").strip()
            province = (raw.get("province") or "").strip()
            if not name or not province:
                problems.append(f"{path.name}: 缺少 name/province 的条目 {raw!r}")
                continue
            if name in seen:
                problems.append(f"{path.name}: 城市重复 {name}")
                continue
            seen.add(name)

            expected_region = province_to_region.get(province)
            if not expected_region:
                problems.append(f"{name}: 省份「{province}」不在 regions.json 里")
            elif expected_region != region_name:
                problems.append(f"{name}: 大区不符（文案 {region_name} / 应为 {expected_region}）")

            coord = find_coord(coords, name, province)
            lon = lat = None
            if coord:
                lon, lat = float(coord["lon"]), float(coord["lat"])
                if rings and not inside_province(rings, province, lon, lat):
                    notes.append(f"{name}: 坐标 ({lon},{lat}) 落在 {province} 轮廓外（来自 {coord['name']}）")
            else:
                problems.append(f"{name}: 缺坐标（{province}）")

            rail, plan_name, why = resolve_rail(probe, name, province)
            if why and why != "无铁路客运":
                notes.append(f"{name}: {why}")

            highlights = []
            for item in raw.get("highlights") or []:
                if isinstance(item, dict):
                    kind = (item.get("kind") or "").strip()
                    if kind not in KINDS:
                        problems.append(f"{name}: 未知看点类型 {kind!r}")
                    highlights.append({"name": (item.get("name") or "").strip(), "kind": kind})
                else:
                    highlights.append({"name": str(item).strip(), "kind": "看点"})

            score = round(float(raw.get("score") or 7.0), 1)
            if not 0 <= score <= 10:
                problems.append(f"{name}: 评分越界 {score}")

            entry = {
                "name": name,
                "province": province,
                "region": region_name or expected_region or "",
                "lon": lon,
                "lat": lat,
                "score": score,
                "hours": float(raw.get("hours") or 4),
                "days": float(raw.get("days") or 1),
                "best_season": raw.get("best_season") or "",
                "tags": list(raw.get("tags") or []),
                "intro": (raw.get("intro") or "").strip(),
                "halfday": (raw.get("halfday") or "").strip(),
                "oneday": (raw.get("oneday") or "").strip(),
                "highlights": highlights,
                "food": (raw.get("food") or "").strip(),
                "hub": bool(raw.get("hub")),
                "note": (raw.get("note") or "").strip(),
                "rail": rail,
                "plan_name": plan_name,
                "photo": None,
                "night": is_night_city({"name": name, "tags": raw.get("tags"),
                                        "highlights": highlights,
                                        "intro": raw.get("intro"), "halfday": raw.get("halfday"),
                                        "oneday": raw.get("oneday"), "note": raw.get("note")}),
            }
            guide = guides.get(name)
            if guide:
                entry["guide"] = guide
            else:
                guide_missing.append(name)
            if plan_name and plan_name != name:
                entry["aliases"] = [plan_name]
            cities.append(entry)

    cities.sort(key=lambda c: (c["region"], -c["score"], c["name"]))

    # 与现有目录对比，给出增删提示
    old = load_json(CATALOG_PATH) or {}
    old_names = {c["name"] for c in old.get("cities") or []}
    new_names = {c["name"] for c in cities}
    added = sorted(new_names - old_names)
    removed = sorted(old_names - new_names)

    print(f"城市目录：{len(cities)} 个城市，覆盖 {len({c['region'] for c in cities})} 个大区")
    for region in regions.get("regions") or []:
        count = sum(1 for c in cities if c["region"] == region["name"])
        rail = sum(1 for c in cities if c["region"] == region["name"] and c["rail"])
        print(f"  {region['name']:<4} {count:>3} 城（通铁路 {rail}）")
    if added:
        print(f"新增 {len(added)}：{'、'.join(added[:20])}{'…' if len(added) > 20 else ''}")
    if removed:
        print(f"移除 {len(removed)}：{'、'.join(removed[:20])}")
    no_rail = [c["name"] for c in cities if not c["rail"]]
    print(f"无铁路客运 {len(no_rail)}：{'、'.join(no_rail)}")
    with_guide = sum(1 for c in cities if c.get("guide"))
    night_count = sum(1 for c in cities if c.get("night"))
    print(f"完整攻略 {with_guide}/{len(cities)} 城 · 适合夜景 {night_count} 城")
    if guide_missing:
        print(f"缺攻略 {len(guide_missing)}：{'、'.join(guide_missing[:30])}"
              f"{'…' if len(guide_missing) > 30 else ''}", file=sys.stderr)
    if notes:
        print(f"\n提示（{len(notes)} 条）：")
        for line in notes:
            print("  ·", line)
    if problems:
        print(f"\n问题（{len(problems)} 条）：", file=sys.stderr)
        for line in problems:
            print("  !", line, file=sys.stderr)

    if args.check_only:
        return 1 if problems else 0

    payload = {
        "schema": 1,
        "note": ("内置城市旅游目录，由 scripts/build_city_catalog.py 生成；"
                 "运行时个人修改请写入 data/tourism.db（内置目录不会覆盖个人编辑）。"),
        "source_coords": "阿里云 DataV.GeoAtlas 边界服务（地级市中心点）",
        "cities": cities,
    }
    CATALOG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    print(f"\n已写入 {CATALOG_PATH.relative_to(PROJECT_ROOT)}"
          f"（{CATALOG_PATH.stat().st_size / 1024:.0f} KB）")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
