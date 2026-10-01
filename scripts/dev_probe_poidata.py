"""开发探针：城市内页的 POI 数据到底能从哪来（免费/开源的先拿，高德只补缝）。

同一座城市分别问三个源，**同一批景点**谁有、字段够不够：

1. OpenStreetMap / Overpass —— ODbL 开源、无 key、无配额，只有节点友好度要求；
   除了景点，还能拿到地铁线路（route=subway）用来画市内线路。
2. Wikidata SPARQL —— CC0、无 key；有中文名、坐标、类型、部分有开放时间。
3. 高德 POI 分类检索（个人 key）——字段最全（地址/类型/坐标/评分），按日配额，
   所以只在「开源源查不到」和「要现算市内用时」时才用，且必须落缓存。

用法：
    python scripts/dev_probe_poidata.py [城市名]      # 默认 洛阳
    python scripts/dev_probe_poidata.py 洛阳 --no-net # 只看本地资料，不联网
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from travel_planner import catalog, db  # noqa: E402
from travel_planner.ingest import amap  # noqa: E402

UA = {"User-Agent": "travel-planner-dev-probe/1.0 (personal use; contact: local)"}
#: Overpass 的公共实例会轮着 504/限流，探针挨个试（真实实现里也该这么做）
OVERPASS_HOSTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
OVERPASS = OVERPASS_HOSTS[0]
WIKIDATA = "https://query.wikidata.org/sparql"


def _get(url: str, timeout: int = 60, headers: dict | None = None) -> str:
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _post(url: str, body: str, timeout: int = 90) -> str:
    req = urllib.request.Request(url, data=body.encode("utf-8"),
                                 headers={**UA, "Content-Type": "text/plain; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _dist_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math

    (lon1, lat1), (lon2, lat2) = a, b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def overpass_query(query: str, timeout: int = 90) -> dict:
    """往 Overpass 发一次查询，实例轮着试（官方实例经常 504/限流）。"""
    last = None
    for host in OVERPASS_HOSTS:
        try:
            return json.loads(_post(host, query, timeout=timeout))
        except Exception as exc:                          # noqa: BLE001
            last = exc
            print(f"    (实例 {urllib.parse.urlparse(host).netloc} 失败：{exc}，换下一个)")
    raise last if last else RuntimeError("Overpass 全部实例失败")


def overpass(bbox: tuple[float, float, float, float], kinds: str) -> list[dict]:
    """bbox=(south,west,north,east) → OSM 景点列表（含坐标与少量标签）。"""
    s, w, n, e = bbox
    query = f"""[out:json][timeout:60];
(
  nwr["tourism"~"^(attraction|museum|viewpoint|artwork|gallery|theme_park|zoo)$"]({s},{w},{n},{e});
  nwr["historic"]({s},{w},{n},{e});
);
out center tags 400;"""
    raw = overpass_query(query)
    out = []
    for el in raw.get("elements") or []:
        tags = el.get("tags") or {}
        name = tags.get("name:zh") or tags.get("name") or ""
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if not name or lat is None or lon is None:
            continue
        out.append({"name": name, "lat": lat, "lon": lon,
                    "kind": tags.get("tourism") or tags.get("historic") or "park",
                    "hours": tags.get("opening_hours") or "",
                    "fee": tags.get("fee") or "",
                    "wikidata": tags.get("wikidata") or "",
                    "wikipedia": (tags.get("wikipedia") or "")[:40]})
    return out


def overpass_parks(bbox: tuple[float, float, float, float]) -> list[dict]:
    s, w, n, e = bbox
    query = f'[out:json][timeout:60];nwr["leisure"="park"]["name"]({s},{w},{n},{e});out center tags 200;'
    try:
        raw = overpass_query(query, timeout=60)
    except Exception as exc:                              # noqa: BLE001
        print(f"  ! 公园查询失败：{exc}")
        return []
    out = []
    for el in raw.get("elements") or []:
        tags = el.get("tags") or {}
        name = tags.get("name:zh") or tags.get("name") or ""
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if name and lat is not None and lon is not None:
            out.append({"name": name, "lat": lat, "lon": lon, "kind": "park"})
    return out


def overpass_subway(bbox: tuple[float, float, float, float]) -> list[dict]:
    s, w, n, e = bbox
    query = f'[out:json][timeout:60];rel["route"="subway"]({s},{w},{n},{e});out tags 60;'
    try:
        raw = overpass_query(query, timeout=60)
    except Exception as exc:                              # noqa: BLE001
        print(f"  ! 地铁线路查询失败：{exc}")
        return []
    return [{"name": (el.get("tags") or {}).get("name") or "",
             "from": (el.get("tags") or {}).get("from") or "",
             "to": (el.get("tags") or {}).get("to") or ""}
            for el in raw.get("elements") or []]


def wikidata(bbox: tuple[float, float, float, float], limit: int = 300) -> list[dict]:
    s, w, n, e = bbox
    query = f"""SELECT ?item ?itemLabel ?coord ?kindLabel WHERE {{
  SERVICE wikibase:box {{
    ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:cornerWest "Point({w} {s})"^^geo:wktLiteral .
    bd:serviceParam wikibase:cornerEast "Point({e} {n})"^^geo:wktLiteral .
  }}
  ?item wdt:P31/wdt:P279* ?kind .
  VALUES ?kind {{ wd:Q570116 wd:Q33506 wd:Q22698 wd:Q16970 }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "zh,zh-hans,en". }}
}} LIMIT {limit}"""
    url = WIKIDATA + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
    raw = json.loads(_get(url, timeout=90, headers={"Accept": "application/sparql-results+json"}))
    out = []
    for row in (raw.get("results") or {}).get("bindings") or []:
        coord = (row.get("coord") or {}).get("value") or ""
        # "Point(112.45 34.62)"
        try:
            lon, lat = [float(x) for x in coord[6:-1].split()]
        except (ValueError, IndexError):
            continue
        out.append({"name": (row.get("itemLabel") or {}).get("value") or "",
                    "lat": lat, "lon": lon,
                    "kind": (row.get("kindLabel") or {}).get("value") or "",
                    "qid": ((row.get("item") or {}).get("value") or "").rsplit("/", 1)[-1]})
    return out


def amap_pois(city: str, center: tuple[float, float]) -> dict:
    """高德分类检索：一次请求拿一页景点（字段最全，配额最贵，所以要省着用）。"""
    conn = db.connect()
    try:
        key = amap.get_key(conn)
    finally:
        conn.close()
    if not key:
        return {"error": "没有高德 key"}
    params = urllib.parse.urlencode({
        "key": key, "keywords": "景点", "types": "110000|140000",
        "city": city, "citylimit": "true", "offset": 25, "page": 1,
        "extensions": "all", "output": "json",
    })
    raw = json.loads(_get(f"https://restapi.amap.com/v3/place/text?{params}", timeout=30))
    pois = []
    for item in raw.get("pois") or []:
        loc = (item.get("location") or "").split(",")
        rating = ((item.get("biz_ext") or {}) or {}).get("rating")
        pois.append({"name": item.get("name") or "",
                     "type": item.get("type") or "",
                     "address": item.get("address") or "",
                     "lat": float(loc[1]) if len(loc) == 2 else None,
                     "lon": float(loc[0]) if len(loc) == 2 else None,
                     "rating": rating or "",
                     "tel": item.get("tel") or ""})
    return {"status": raw.get("status"), "count": raw.get("count"),
            "info": raw.get("info"), "pois": pois}


def amap_walk(a: tuple[float, float], b: tuple[float, float]) -> dict:
    """市内一段的真实步行用时（用来验证「按需现算 + 落缓存」这条路）。"""
    conn = db.connect()
    try:
        key = amap.get_key(conn)
    finally:
        conn.close()
    if not key:
        return {"error": "没有高德 key"}
    params = urllib.parse.urlencode({"key": key, "origin": f"{a[0]},{a[1]}",
                                     "destination": f"{b[0]},{b[1]}"})
    raw = json.loads(_get(f"https://restapi.amap.com/v3/direction/walking?{params}", timeout=30))
    path = (raw.get("route") or {}).get("paths") or []
    if not path:
        return {"status": raw.get("status"), "info": raw.get("info"), "paths": 0}
    return {"status": raw.get("status"), "paths": len(path),
            "distance_m": int(path[0].get("distance") or 0),
            "minutes": round(int(path[0].get("duration") or 0) / 60),
            "steps": len(path[0].get("steps") or [])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="城市 POI 数据源探针")
    ap.add_argument("city", nargs="?", default="洛阳")
    ap.add_argument("--no-net", action="store_true", help="只打印本地资料，不联网")
    ap.add_argument("--span", type=float, default=0.35, help="bbox 半边长（度）")
    args = ap.parse_args(argv)

    item = catalog.city(args.city)
    if not item or item.get("lat") is None:
        print(f"✗ 城市目录里没有「{args.city}」或没坐标，换个城市或先补坐标")
        return 1
    lat, lon = float(item["lat"]), float(item["lon"])
    bbox = (round(lat - args.span, 4), round(lon - args.span * 1.3, 4),
            round(lat + args.span, 4), round(lon + args.span * 1.3, 4))
    print(f"== {item['name']}（{lat},{lon}）bbox={bbox} ==")
    ours = [h["name"] for h in (item.get("highlights") or [])]
    guide = item.get("guide") or {}
    print(f"本地资料：看点 {len(ours)} 个 → {'、'.join(ours)}")
    print(f"          guide {guide.get('days')} 天，"
          f"{len(guide.get('itinerary') or [])} 段动线，"
          f"{len(guide.get('tips') or [])} 条提示")
    if args.no_net:
        return 0

    print("\n-- OpenStreetMap / Overpass --")
    try:
        osm = overpass(bbox, "tourism|historic|park")
        with_hours = [p for p in osm if p["hours"]]
        with_wd = [p for p in osm if p["wikidata"]]
        print(f"  景点/古迹 {len(osm)} 个（{len(with_hours)} 个有开放时间，"
              f"{len(with_wd)} 个带 wikidata 链接）")
        for p in osm[:8]:
            print(f"    · {p['name']}  [{p['kind']}]  {p['lat']:.4f},{p['lon']:.4f}"
                  + (f"  ⏰{p['hours'][:24]}" if p["hours"] else ""))
        hit = [n for n in ours if any(n in p["name"] or p["name"] in n for p in osm)]
        print(f"  命中本地看点：{len(hit)}/{len(ours)} → {'、'.join(hit)}")
        parks = overpass_parks(bbox)
        print(f"  公园 {len(parks)} 个，例：{'、'.join(p['name'] for p in parks[:6])}")
        osm = osm + parks
        subs = overpass_subway(bbox)
        print(f"  地铁线路 {len(subs)} 条：" + "、".join(s["name"] for s in subs[:8]))
    except Exception as exc:                              # noqa: BLE001
        print(f"  ! Overpass 失败：{type(exc).__name__}: {exc}")
        osm = []

    print("\n-- Wikidata SPARQL --")
    try:
        wd = wikidata(bbox)
        print(f"  带坐标的条目 {len(wd)} 个")
        for p in wd[:8]:
            print(f"    · {p['name']}  [{p['kind']}]  {p['lat']:.4f},{p['lon']:.4f}  {p['qid']}")
        hit = [n for n in ours if any(n in p["name"] or p["name"] in n for p in wd)]
        print(f"  命中本地看点：{len(hit)}/{len(ours)} → {'、'.join(hit)}")
    except Exception as exc:                              # noqa: BLE001
        print(f"  ! Wikidata 失败：{type(exc).__name__}: {exc}")
        wd = []

    print("\n-- 高德 POI 分类检索（1 次请求）--")
    try:
        am = amap_pois(item["name"], (lon, lat))
        if am.get("error"):
            print("  ! " + am["error"])
        else:
            print(f"  status={am['status']} count={am['count']} info={am['info']} "
                  f"本页 {len(am['pois'])} 条")
            for p in am["pois"][:8]:
                star = f" ★{p['rating']}" if p["rating"] else ""
                print(f"    · {p['name']}  [{p['type'][:18]}]{star}  {p['address'][:20]}")
            hit = [n for n in ours if any(n in p["name"] or p["name"] in n for p in am["pois"])]
            print(f"  本页命中本地看点：{len(hit)}/{len(ours)} → {'、'.join(hit)}")
            pts = [p for p in am["pois"] if p["lat"] and p["lon"]]
            if len(pts) >= 2:
                a, b = pts[0], pts[1]
                walk = amap_walk((a["lon"], a["lat"]), (b["lon"], b["lat"]))
                gap = _dist_km((a["lon"], a["lat"]), (b["lon"], b["lat"]))
                print(f"  市内一段实测：{a['name']} → {b['name']} 直线 {gap:.1f}km，"
                      f"步行 {walk.get('minutes')} 分钟（{walk.get('distance_m')} 米，"
                      f"{walk.get('steps')} 步）")
    except Exception as exc:                              # noqa: BLE001
        print(f"  ! 高德失败：{type(exc).__name__}: {exc}")

    print("\n== 汇总 ==")
    allnames = {p["name"] for p in (osm or [])} | {p["name"] for p in (wd or [])}
    covered = [n for n in ours if any(n in x or x in n for x in allnames)]
    print(f"  只看开源两个源：本地看点 {len(ours)} 个里能对上 {len(covered)} 个"
          + (f"（缺：{'、'.join(n for n in ours if n not in covered)}）" if len(covered) < len(ours) else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
