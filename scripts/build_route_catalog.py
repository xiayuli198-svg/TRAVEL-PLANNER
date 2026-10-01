"""把 data/route_content/*.json 合并校验成 src/travel_planner/data/route_catalog.json。

校验项（都是真踩过或容易出错的）：
* 必填字段、id 唯一、起点终点能在 city_coords 里找到（否则地图上落不了点）
* 分段的时间/价格不能为空，``total_hours`` 与分段之和不能差太多
* ``cost_low <= cost_high``，评分在 0-100
* 每条必须写 ``source`` 与口径日期，避免「不知道哪来的数字」

用法：
    python scripts/build_route_catalog.py            # 合并 + 校验 + 写目录
    python scripts/build_route_catalog.py --check    # 只校验，不写
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows 控制台默认 GBK，打印 ✓/✗ 会 UnicodeEncodeError；统一切到 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from travel_planner import routes as routes_mod  # noqa: E402

CONTENT_DIR = ROOT / "data" / "route_content"
STAGING_DIR = CONTENT_DIR / "_staging"
OUT = ROOT / "src" / "travel_planner" / "data" / "route_catalog.json"

REQUIRED = ("id", "name", "from_city", "to_city", "summary", "segments",
            "cost_low", "cost_high", "source")
VALID_MODES = {"train", "rail", "high_speed", "bus", "car", "boat", "fly", "walk", "metro"}
VALID_CONFIDENCE = {"high", "medium", "low"}


def load_content(*, include_staging: bool = False) -> tuple[dict, list[str]]:
    """合并素材。``include_staging`` 时把暂存区（子代理产出）也读进来。

    暂存区里的条目**不允许覆盖**已审过的素材（同 id 直接报错），
    这样「先审后收」的流程不会被悄悄绕过。
    """
    merged: dict = {"updated": "", "note": "", "routes": []}
    problems: list[str] = []
    by_id: dict[str, dict] = {}
    files = [(p, "content") for p in sorted(CONTENT_DIR.glob("*.json"))]
    if include_staging:
        files += [(p, "staging") for p in sorted(STAGING_DIR.glob("*.json"))]
    if not files:
        problems.append(f"{CONTENT_DIR} 下没有 *.json")
        return merged, problems
    for path, origin in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{path.name}: 读不了或不是合法 JSON（{exc}）")
            continue
        if not isinstance(raw, dict):
            # 暂存区里子代理留下的临时草稿（比如一个数组清单）不该把校验整体卡住；
            # 正式素材目录里的文件仍然当成错误报出来。
            if origin == "staging":
                print(f"  · 跳过 {path.name}（不是素材文件，顶层不是对象）")
                continue
            problems.append(f"{path.name}: 顶层应当是对象")
            continue
        merged["updated"] = max(merged["updated"], str(raw.get("updated") or ""))
        if raw.get("note"):
            merged["note"] = raw["note"]
        for item in raw.get("routes") or []:
            rid = str(item.get("id") or "")
            if not rid:
                problems.append(f"{path.name}: 有一条路线没有 id")
                continue
            if rid in by_id:
                other = by_id[rid].get("_origin")
                if origin == "staging" and other == "content":
                    problems.append(f"{path.name}: id {rid} 与已审素材冲突（暂存不能覆盖正式素材）")
                else:
                    problems.append(f"{path.name}: id 重复 {rid}")
                continue
            item["_file"] = path.name
            item["_origin"] = origin
            by_id[rid] = item
    merged["routes"] = sorted(by_id.values(), key=lambda r: r["id"])
    return merged, problems


def validate(data: dict) -> list[str]:
    problems: list[str] = []
    coords = routes_mod._load_coords()
    seen: set[str] = set()
    names: dict[str, str] = {}          # 名称 → id：页面上重名会像「同一条出现两次」
    for item in data["routes"]:
        rid = item["id"]
        where = f"{item.get('_file', '?')}:{rid}"
        for field in REQUIRED:
            if item.get(field) in (None, "", []):
                problems.append(f"{where}: 缺 {field}")
        if rid in seen:
            problems.append(f"{where}: id 重复")
        seen.add(rid)
        name = str(item.get("name") or "").strip()
        if name:
            if name in names and names[name] != rid:
                problems.append(f"{where}: 名称与 {names[name]} 重复（「{name}」）"
                                "—— 两条内容不同就改名区分，别让页面看着像同一条")
            else:
                names[name] = rid
        low, high = item.get("cost_low"), item.get("cost_high")
        if isinstance(low, (int, float)) and isinstance(high, (int, float)) and low > high:
            problems.append(f"{where}: cost_low({low}) > cost_high({high})")
        hours = item.get("total_hours")
        if not isinstance(hours, (int, float)) or hours <= 0:
            problems.append(f"{where}: total_hours 应当是正数")
        elif not item.get("alternatives"):
            # alternatives=true 时分段是「几种走法」而不是「连续几段」，不能相加
            seg_hours = sum(s.get("hours") or 0 for s in item.get("segments") or [])
            if seg_hours and abs(seg_hours - hours) > max(4, hours * 0.5):
                problems.append(f"{where}: total_hours={hours} 与分段之和 {seg_hours} 差太多"
                                "（若分段是多种走法，请加 \"alternatives\": true）")
        for score_key in ("value_score", "comfort_score", "clever_score", "clever_extra"):
            value = item.get(score_key)
            if value is None:
                continue
            limit = 40 if score_key == "clever_extra" else 100
            if not (0 <= value <= limit):
                problems.append(f"{where}: {score_key}={value} 超出 0-{limit}")
        # 巧思分：标签必须在白名单里（防止随手编标签刷分），高分必须写清「妙在哪」
        tags = item.get("clever_tags") or []
        if not isinstance(tags, list):
            problems.append(f"{where}: clever_tags 应当是数组")
            tags = []
        for tag in tags:
            if tag not in routes_mod.CLEVER_WEIGHTS:
                problems.append(f"{where}: 未知 clever_tags 标签 {tag!r}"
                                f"（可用：{'、'.join(routes_mod.CLEVER_WEIGHTS)}）")
        score = routes_mod.clever_score(item)
        if score >= 70 and not (item.get("smart_tips") or []):
            problems.append(f"{where}: 巧思分 {score} 却没写 smart_tips（说清妙在哪才能给高分）")
        if score >= 60:
            if item.get("cost_baseline") is None:
                problems.append(f"{where}: 巧思路线要填 cost_baseline（直达基线价，用来对比多花了多少）")
            if not item.get("extra_value"):
                problems.append(f"{where}: 巧思路线要写 extra_value（多花的钱换到了什么）")
        for seg in item.get("segments") or []:
            if not seg.get("frm") or not seg.get("to"):
                problems.append(f"{where}: 分段缺 frm/to")
            mode = seg.get("mode")
            if mode not in VALID_MODES:
                problems.append(f"{where}: 未知交通方式 {mode!r}")
            if not seg.get("hours") or not seg.get("price"):
                problems.append(f"{where}: 分段 {seg.get('frm')}->{seg.get('to')} 缺 hours/price")
        if item.get("confidence") not in VALID_CONFIDENCE:
            problems.append(f"{where}: confidence 应为 high/medium/low")
        for key in ("from_city", "to_city"):
            city = item.get(key)
            # 坐标查找走 coord_of：先精确、再去方位后缀（北京西 → 北京），
            # 这样素材里写站名也能落点，不必为每个站单配坐标。
            if city and routes_mod.coord_of(city, coords) is None:
                problems.append(f"{where}: {key}={city} 找不到坐标"
                                "（跑 scripts/fill_route_coords.py 自动补，或改写成地级市名）")
        if not item.get("name") or "·" not in str(item.get("name")):
            problems.append(f"{where}: name 建议写成「主题·起终点」的形式")
        # 环线（起点=终点）要显式标记，否则「起终点相同」看起来像填错
        if item.get("from_city") and item.get("from_city") == item.get("to_city"):
            if not item.get("loop"):
                problems.append(f"{where}: 起终点相同，请加 \"loop\": true")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="合并校验省钱/舒适/巧思路线素材")
    ap.add_argument("--check", action="store_true", help="只校验，不写目录")
    ap.add_argument("--include-staging", action="store_true",
                    help="把 _staging 里子代理产出的路线一起合并（默认只读已审素材）")
    args = ap.parse_args(argv)

    data, problems = load_content(include_staging=args.include_staging)
    problems += validate(data)
    if problems:
        print("✗ 校验不通过：", file=sys.stderr)
        for line in problems:
            print("  -", line, file=sys.stderr)
        return 1
    staged = [r for r in data["routes"] if r.get("_origin") == "staging"]
    for item in data["routes"]:
        item.pop("_file", None)
        item.pop("_origin", None)
    print(f"✓ 校验通过：{len(data['routes'])} 条路线"
          + (f"（其中暂存区 {len(staged)} 条）" if staged else ""))
    cats: dict[str, int] = {}
    for item in data["routes"]:
        cats[item.get("category") or "其他"] = cats.get(item.get("category") or "其他", 0) + 1
    print("  分类：", "、".join(f"{k} {v}" for k, v in cats.items()))
    print("  口径日期：", data["updated"])
    clever = [r for r in data["routes"] if routes_mod.clever_score(r) >= 60]
    if clever:
        print(f"  巧思路线 {len(clever)} 条：" +
              "、".join(f"{r['name']}({routes_mod.clever_score(r)})" for r in clever[:6]))
    if staged:
        print("  暂存区条目（建议审过再正式化）："
              + "、".join(r["id"] for r in staged[:8]))
    if args.check:
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 已写入 {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
