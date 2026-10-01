"""补齐路线库用到的城市坐标：先查本地目录，缺的再用高德地理编码兜底。

为什么需要它：路线素材里会出现大量县市级目的地（格尔木、额济纳、集宁、伊犁、罗平…），
DataV 的市级坐标文件里没有它们，校验器就会拦下「地图上落不了点」。手工一个个查太慢，
而这两种来源都是现成且免费的：

1. ``city_catalog.json``（239 城）—— 项目自带、离线、即时；
2. 高德 Web 服务**地理编码** ``/v3/geocode/geo`` —— key 已存在 ``meta.amap_key``，
   与驾车路径规划用的是同一个 key，只是换个 endpoint。

结果写到 ``src/travel_planner/data/city_coords_routes.json``（与手工坐标同文件，合并去重），
以后重跑会跳过已有项；加 ``--refresh`` 可强制重查。

用法：
    python scripts/fill_route_coords.py                 # 扫描 data/route_content/*.json + _staging
    python scripts/fill_route_coords.py --dry-run       # 只看缺哪些、不查不写
    python scripts/fill_route_coords.py --refresh 敦煌   # 强制重查指定城市
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from travel_planner import catalog, db, routes as routes_mod  # noqa: E402

CONTENT_DIRS = [ROOT / "data" / "route_content", ROOT / "data" / "route_content" / "_staging"]
OUT = ROOT / "src" / "travel_planner" / "data" / "city_coords_routes.json"
GEOCODE = "https://restapi.amap.com/v3/geocode/geo"

#: 参考点容差（度）：县城内部的景点（篁岭/江岭/李坑都在婺源县内）会差 0.2-0.3 度，
#: 直接按 HINTS 的 ref 定死更稳，也省一次可能被限流的请求。
MANUAL: dict[str, tuple[float, float]] = {
    "黄龙九寨": (33.2520, 104.2426),      # 九寨黄龙机场/九寨沟方向（素材里的「黄龙九寨」指这里）
    "莫高窟": (40.0400, 94.8100),         # 敦煌莫高窟
    "篁岭": (29.2520, 117.8620),          # 婺源篁岭
    "江岭": (29.1900, 117.9000),          # 婺源江岭梯田
    "李坑": (29.2700, 117.9400),          # 婺源李坑
    # 高德把下面三个景点名查到了完全不同的省（实测：胡杨林→新疆、北极村→青海、
    # 青石嘴观花台→广东），所以定死坐标，不再问它。
    "胡杨林景区": (41.9667, 101.0667),    # 内蒙古额济纳旗胡杨林
    "北极村": (53.4722, 122.3564),        # 黑龙江漠河北极村
    "青石嘴观花台": (37.3667, 101.4833),  # 青海门源青石嘴
    "伊犁": (43.9167, 81.3167),           # 伊犁州伊宁市（素材写的是州名）
    "伊宁": (43.9167, 81.3240),           # 伊宁市（伊犁州首府）
    # 站名/景点名也会被查到别的省（实测：黄土店→湖南、八达岭→广东、汉口→陕西、
    # 螺丝田→贵州、金顶→云南），同样定死，不再问它。
    "黄土店": (40.0786, 116.3448),        # 北京昌平黄土店站（市郊铁路 S2 线起点，霍营地铁旁）
    "八达岭": (40.3567, 116.0147),        # 北京延庆八达岭长城/八达岭站
    "汉口": (30.6220, 114.2540),          # 武汉汉口站（江汉区）
    "螺丝田": (24.9900, 104.3000),        # 云南罗平螺丝田（约：县城北约十余公里，牛街村一带）
    "金顶": (27.4568, 114.1724),          # 江西萍乡武功山金顶（白鹤峰）
    # 境外/口岸城市：高德是境内服务，查不到或会查偏；这里核定常用坐标
    "琅勃拉邦": (19.8860, 102.1350),      # 老挝琅勃拉邦（中老铁路）
    "万象": (17.9757, 102.6331),          # 老挝万象
    "河内": (21.0278, 105.8342),          # 越南河内
    "乌兰巴托": (47.8864, 106.9057),      # 蒙古乌兰巴托
    "海参崴": (43.1155, 131.8855),        # 俄罗斯符拉迪沃斯托克
    "符拉迪沃斯托克": (43.1155, 131.8855),
    "阿拉木图": (43.2220, 76.8512),       # 哈萨克斯坦阿拉木图
    "加德满都": (27.7172, 85.3240),       # 尼泊尔加德满都（中尼吉隆口岸方向）
    "磨丁": (21.1600, 101.7000),          # 老挝磨丁（中老铁路口岸，约：紧邻云南磨憨）
    "磨憨": (21.1900, 101.6800),          # 云南西双版纳勐腊磨憨口岸
    "乌鲁木齐": (43.8256, 87.6168),
}

#: 境外坐标不做省份校验（PROVINCE_BOXES 只覆盖中国）
OVERSEAS = {"琅勃拉邦", "万象", "河内", "乌兰巴托", "海参崴", "符拉迪沃斯托克", "阿拉木图",
            "加德满都"}

#: 省份粗略包围盒：(lat_min, lat_max, lon_min, lon_max)。
#: 高德对县级/景点名会跨省查偏，命中不了任何盒子就丢弃，宁缺勿错。
PROVINCE_BOXES: dict[str, tuple[float, float, float, float]] = {
    "北京": (39.4, 41.1, 115.4, 117.5), "天津": (38.5, 40.3, 116.7, 118.1),
    "河北": (36.0, 42.7, 113.4, 119.9), "山西": (34.5, 40.9, 110.2, 114.6),
    "内蒙古": (37.4, 53.4, 97.1, 126.1), "辽宁": (38.7, 43.5, 118.8, 125.8),
    "吉林": (40.8, 46.3, 121.6, 131.3), "黑龙江": (43.4, 53.6, 121.1, 135.1),
    "上海": (30.6, 31.9, 120.8, 122.2), "江苏": (30.7, 35.2, 116.3, 121.9),
    "浙江": (27.0, 31.4, 118.0, 123.0), "安徽": (29.3, 34.7, 114.8, 119.7),
    "福建": (23.5, 28.4, 115.8, 120.6), "江西": (24.4, 30.1, 113.5, 118.5),
    "山东": (34.3, 38.4, 114.8, 122.7), "河南": (31.3, 36.4, 110.3, 116.7),
    "湖北": (29.0, 33.3, 108.3, 116.2), "湖南": (24.6, 30.2, 108.7, 114.3),
    "广东": (20.1, 25.6, 109.6, 117.3), "广西": (20.8, 26.4, 104.4, 112.1),
    "海南": (18.1, 20.2, 108.5, 111.1), "四川": (26.0, 34.3, 97.3, 108.6),
    "贵州": (24.6, 29.2, 103.5, 109.6), "云南": (21.1, 29.3, 97.5, 106.2),
    "西藏": (26.8, 36.5, 78.3, 99.2), "陕西": (31.7, 39.6, 105.4, 111.3),
    "甘肃": (32.1, 42.8, 92.1, 108.8), "青海": (31.5, 39.4, 89.3, 103.1),
    "宁夏": (35.2, 39.4, 104.2, 107.7), "新疆": (34.2, 49.2, 73.4, 96.4),
    # 港澳台：以前没有这三个盒子，于是这几个地方的点位一律被当成「查偏」丢掉 ——
    # 实测高德库里是有的（台北101、日月潭、维多利亚港都查得到），
    # 前提是 city 参数用县市级全称（city=台湾 会返回内地同名点）。
    "台湾": (21.8, 25.4, 119.3, 122.1),
    "香港": (22.15, 22.60, 113.80, 114.45),
    "澳门": (22.10, 22.23, 113.52, 113.62),
}


def in_any_province(lat: float, lon: float) -> str:
    """落在哪个省框里（不在任何框里返回空串）。"""
    for name, (lat0, lat1, lon0, lon1) in PROVINCE_BOXES.items():
        if lat0 <= lat <= lat1 and lon0 <= lon <= lon1:
            return name
    return ""

#: 高德地理编码对县级/景点名很容易"张冠李戴"（实测：黄龙九寨→陕西、雪乡→西藏、
#: 柳园→广东、莫高窟→深圳、篁岭→江西另一个县）。所以给这些名字指定约束城市
#: （高德请求里的 city 参数）与参考坐标（用于偏离校验），查偏了就丢弃。
HINTS: dict[str, dict] = {
    "雪乡": {"city": "牡丹江", "ref": (44.33, 128.60)},       # 黑龙江大海林双峰林场
    "柳园": {"city": "酒泉", "ref": (40.55, 95.80)},          # 甘肃瓜州柳园镇
    "雪乡双峰": {"city": "牡丹江", "ref": (44.33, 128.60)},
    # 省份包围盒太粗：这些名字被查到了同一个省内的另一个角落（长白山→新疆、东兴市→南宁），
    # 用精确参考点 + 80 公里容差兜住。
    "长白山": {"city": "延边", "ref": (42.02, 128.06)},       # 吉林长白山保护开发区
    "东兴市": {"city": "防城港", "ref": (21.54, 107.97)},     # 广西东兴口岸
    "宜兴": {"city": "无锡", "ref": (31.34, 119.82)},         # 江苏宜兴（环太湖）
    "都昌": {"city": "九江", "ref": (29.27, 116.20)},
    "湖口": {"city": "九江", "ref": (29.73, 116.25)},
    "库车": {"city": "阿克苏", "ref": (41.72, 83.00)},
    # 县级目的地（花季/梯田/梨花这类「县城才是目的地」的条目会用到）
    "金川": {"city": "阿坝", "ref": (31.47, 102.06)},         # 四川阿坝州金川县
    "罗平": {"city": "曲靖", "ref": (24.89, 104.30)},         # 云南曲靖罗平县
    "元阳": {"city": "红河", "ref": (23.22, 102.83)},         # 云南红河州元阳县（南沙）
}
#: 与参考坐标的距离上限（公里）：超过就认为查偏了
MAX_OFFSET_KM = 80


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math

    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(min(1.0, h)))


def route_cities() -> list[str]:
    """素材里出现过的所有城市名（起终点 + 分段端点），保持出现顺序。"""
    names: list[str] = []
    for base in CONTENT_DIRS:
        if not base.exists():
            continue
        for path in sorted(base.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                print(f"  ! 跳过读不了的文件 {path.name}", file=sys.stderr)
                continue
            for item in (raw.get("routes") or []):
                for key in ("from_city", "to_city"):
                    value = routes_mod._clean_city(item.get(key))
                    if value and value not in names:
                        names.append(value)
                for seg in item.get("segments") or []:
                    for key in ("frm", "to"):
                        value = routes_mod._clean_city(seg.get(key))
                        if value and value not in names:
                            names.append(value)
    return names


def local_coords() -> dict[str, list[float]]:
    """本地已有的坐标：三个坐标文件 + 239 城目录，键都做城市名归一。

    归一很重要：素材里会写「北京西」「上海虹桥」「黄山北」这类站名，
    归一到城市就能命中坐标，不必为每个站单独配点（也是校验器的口径）。
    """
    out: dict[str, list[float]] = {}
    for name, point in routes_mod._load_coords().items():
        out[name] = list(point)
        cleaned = routes_mod._clean_city(name)
        if cleaned:
            out.setdefault(cleaned, list(point))
    for city in catalog.cities():
        name = (city.get("name") or "").strip()
        if not name or city.get("lat") is None or city.get("lon") is None:
            continue
        for key in (name, name + "市", routes_mod._clean_city(name)):
            if key:
                out.setdefault(key, [float(city["lat"]), float(city["lon"])])
    return out


def amap_key() -> str:
    """高德 key：优先环境变量 AMAP_KEY，其次 meta 表的 amap_key（与驾车规划同一个 key）。"""
    import os

    env = (os.environ.get("AMAP_KEY") or "").strip()
    if env:
        return env
    try:
        from travel_planner.ingest import amap
    except ImportError:                                   # 兼容旧布局
        amap = None                                          # type: ignore[assignment]
    try:
        conn = db.connect()
    except Exception:                                    # noqa: BLE001
        return ""
    try:
        if amap is not None and hasattr(amap, "get_key"):
            return amap.get_key(conn) or ""
        row = conn.execute("SELECT value FROM meta WHERE key='amap_key'").fetchone()
        return (row["value"] if row else "") or ""
    except Exception:                                    # noqa: BLE001
        return ""
    finally:
        conn.close()


def geocode(city: str, key: str, *, region: str = "") -> tuple[float, float] | None:
    """高德地理编码：返回 (lat, lon)。失败/限流返回 None（调用方继续下一个）。

    高德 Web 服务有 QPS 限制（实测连续请求会返回 ``CUQPS_HAS_EXCEEDED_THE_LIMIT``），
    所以限流时按 1s/2s/3s 退避重试，而不是直接放弃这个城市。
    """
    params = {"key": key, "address": city, "output": "JSON"}
    if region:
        params["city"] = region
    url = GEOCODE + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.load(resp)
        except Exception as exc:                             # noqa: BLE001
            print(f"  ! {city}: 请求失败 {exc}", file=sys.stderr)
            return None
        info = str(data.get("info") or "")
        if str(data.get("status")) != "1":
            if "QPS" in info or "LIMIT" in info.upper():
                time.sleep(1.0 + attempt)
                continue
            print(f"  ! {city}: 高德返回 {info}", file=sys.stderr)
            return None
        geocodes = data.get("geocodes") or []
        if not geocodes:
            print(f"  ! {city}: 查不到坐标", file=sys.stderr)
            return None
        first = geocodes[0]
        try:
            lon, lat = (float(x) for x in str(first["location"]).split(","))
        except (KeyError, ValueError):
            print(f"  ! {city}: 坐标格式异常 {first.get('location')!r}", file=sys.stderr)
            return None
        return lat, lon
    print(f"  ! {city}: 连续限流，跳过（稍后重跑）", file=sys.stderr)
    return None


def load_out() -> dict:
    if OUT.exists():
        try:
            data = json.loads(OUT.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {"schema": 1, "note": "", "cities": []}


def _write_one(name: str, lat: float, lon: float, *, source: str,
               constraint: str = "") -> None:
    """把单个城市写进坐标文件（供命令行与网页导入共用）。

    每次都读-改-写，避免两处同时补坐标时互相覆盖。
    """
    data = load_out()
    cities = {str(c.get("name")): c for c in data.get("cities") or []}
    entry = {"name": name, "lon": round(lon, 4), "lat": round(lat, 4), "source": source}
    if constraint:
        entry["constraint"] = constraint
    cities[name] = entry
    data["cities"] = sorted(cities.values(), key=lambda c: str(c.get("name")))
    if not data.get("note"):
        data["note"] = ("路线库用到的县市级目的地坐标：手工整理的常用点 + 高德地理编码自动补齐"
                        "（source=amap-geocode）。仅供路线图落点，不做导航用途。")
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="补齐路线库城市坐标")
    ap.add_argument("--dry-run", action="store_true", help="只报告缺哪些，不查询不写入")
    ap.add_argument("--refresh", nargs="*", default=[], metavar="城市",
                    help="强制重查这些城市（不给值则重查全部缺失项）")
    ap.add_argument("--delay", type=float, default=0.25, help="高德请求间隔秒数")
    args = ap.parse_args(argv)

    wanted = route_cities()
    have = local_coords()
    out = load_out()
    existing = {str(c.get("name")): c for c in out.get("cities") or []}
    # 「有没有坐标」问 coord_of（含去方位后缀的降级），别自己写 `in have`
    missing = [c for c in wanted
               if routes_mod.coord_of(c, have) is None
               and routes_mod.coord_of(c, existing) is None]
    if args.refresh:
        missing = [c for c in wanted if c in set(args.refresh)] + [
            c for c in missing if c not in set(args.refresh)]
    print(f"素材里出现 {len(wanted)} 个城市；本地坐标覆盖 {len([c for c in wanted if c in have])} 个")
    if not missing:
        print("✓ 没有缺失，无需查询")
        return 0
    print(f"待补 {len(missing)} 个：" + "、".join(missing))
    if args.dry_run:
        return 0

    key = amap_key()
    if not key:
        print("✗ 没有高德 key（先在数据台/CLI 配置 set-amap-key），无法自动补坐标", file=sys.stderr)
        return 1

    added, failed, suspicious = [], [], []
    for city in missing:
        if city in MANUAL:
            lat, lon = MANUAL[city]
            existing[city] = {"name": city, "lon": round(lon, 4), "lat": round(lat, 4),
                              "source": "manual"}
            added.append(city)
            print(f"  ✓ {city} → {lat:.4f}, {lon:.4f}（手工核定）")
            continue
        hint = HINTS.get(city) or {}
        result = geocode(city, key, region=hint.get("city", ""))
        if not result:
            failed.append(city)
            continue
        lat, lon = result
        ref = hint.get("ref")
        if ref:
            offset = _haversine(ref, (lat, lon))
            if offset > MAX_OFFSET_KM:
                print(f"  ✗ {city} → {lat:.4f}, {lon:.4f} 偏离参考点 {offset:.0f} 公里，"
                      "判定查偏，已丢弃", file=sys.stderr)
                suspicious.append(city)
                continue
        # 省份粗筛：命中不了任何省框就丢弃，宁缺勿错。
        # 境外城市不在框内是正常的（高德也基本查不准），所以放行。
        if city in OVERSEAS:
            province = "境外"
        else:
            province = in_any_province(lat, lon)
        if not province:
            print(f"  ✗ {city} → {lat:.4f}, {lon:.4f} 不落在任何省份包围盒内，判定查偏，已丢弃",
                  file=sys.stderr)
            suspicious.append(city)
            continue
        existing[city] = {"name": city, "lon": round(lon, 4), "lat": round(lat, 4),
                          "source": "amap-geocode",
                          **({"constraint": hint["city"]} if hint.get("city") else {})}
        added.append(city)
        print(f"  ✓ {city} → {lat:.4f}, {lon:.4f}"
              + (f"（约束 {hint['city']}）" if hint.get("city") else ""))
        time.sleep(max(0.0, args.delay))

    if not added:
        print("✗ 一个都没查到（检查 key 配额或网络）", file=sys.stderr)
        return 1
    out["cities"] = sorted(existing.values(), key=lambda c: str(c.get("name")))
    out["note"] = ("路线库用到的县市级目的地坐标：手工整理的常用点 + 高德地理编码自动补齐"
                   "（source=amap-geocode）。高德对县级/景点名会查偏，故对易错项加了"
                   "约束城市与偏离校验（HINTS）。仅供路线图落点，不做导航用途。")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 写入 {OUT.relative_to(ROOT)}：新增 {len(added)} 个，"
          f"丢弃（查偏）{len(suspicious)} 个，失败 {len(failed)} 个")
    if suspicious:
        print("  查偏丢弃：" + "、".join(suspicious), file=sys.stderr)
    if failed:
        print("  失败：" + "、".join(failed), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
