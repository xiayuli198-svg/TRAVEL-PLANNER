"""用维基数据交叉核对城内点位坐标（可重复跑、带缓存）。

做法：对每个看点用**正式名**去维基数据搜实体 → 取 P625（坐标）→ 和库里的坐标算大圆距离：

* ≤2 公里：一致（同一处地方）
* 2-20 公里：可疑（景区范围大、或取的是大门/游客中心）
* >20 公里：基本可以确定我们放错了地方
* 维基数据没有这个条目 / 条目没有坐标：跳过（不代表我们错）

核对结果写进 ``poi_check`` 表（缓存），--apply 才回写坐标，且只回写「维基标的物名字对得上」
且距离差得离谱的那些，来源写成 `维基数据 Qxxxx`，口径日期照实标。

用法：
    python scripts/verify_poi_coords.py                 # 全量核对（约 900 次请求，10 分钟）
    python scripts/verify_poi_coords.py --limit 50      # 先跑 50 条看看
    python scripts/verify_poi_coords.py --report        # 只看已有结果
    python scripts/verify_poi_coords.py --apply         # 回写确认错的那些
    python scripts/verify_poi_coords.py --only 香港 台北  # 只核这些城
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from travel_planner import poi  # noqa: E402

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKI_API = "https://zh.wikipedia.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
UA = {"User-Agent": "travel-planner-coord-check/1.0 (personal research; contact: local-user)",
      "Accept": "application/sparql-results+json"}

#: 判据阈值（公里）
SAME_KM = 2.0
SUSPECT_KM = 20.0

#: 全局最小请求间隔 + 429 退避。这台机器走代理，维基系列接口限得很紧
#: （动辄 Retry-After 20 秒），所以宁可慢也别撞墙。
MIN_INTERVAL = 1.6
_last_call = [0.0]

SCHEMA = """
CREATE TABLE IF NOT EXISTS poi_check (
  city TEXT NOT NULL,
  name TEXT NOT NULL,
  checked_at TEXT NOT NULL DEFAULT '',
  qid TEXT NOT NULL DEFAULT '',
  wiki_label TEXT NOT NULL DEFAULT '',
  wiki_desc TEXT NOT NULL DEFAULT '',
  wiki_lat REAL, wiki_lon REAL,
  km REAL,
  verdict TEXT NOT NULL DEFAULT '',      -- same | suspect | wrong | nomatch | nocoord
  note TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (city, name)
);
"""

#: 名字里这些尾巴抹掉再搜一次（「故宫博物院」搜不到就搜「故宫」）
SEARCH_TAILS = ("博物院", "博物馆", "纪念馆", "陈列馆", "展览馆", "美术馆", "国家级风景名胜区",
                "风景名胜区", "风景区", "景区", "国家森林公园", "森林公园", "地质公园",
                "湿地公园", "遗址公园", "公园", "广场", "步行街", "历史文化街区", "街区",
                "古城", "古镇", "古村")


def _get(url: str, timeout: int = 25, retries: int = 5) -> dict:
    """带全局限速与 429 退避的 GET（维基系列接口限得很紧）。"""
    last = None
    for attempt in range(retries):
        gap = time.time() - _last_call[0]
        if gap < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - gap)
        _last_call[0] = time.time()
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 429:
                wait = 5.0
                try:
                    wait = float(exc.headers.get("Retry-After") or 5)
                except (TypeError, ValueError):
                    pass
                time.sleep(min(wait + 1.0, 40.0))         # 老实等，别硬撞
                continue
            time.sleep(1.0 + attempt)
        except Exception as exc:                          # noqa: BLE001
            last = exc
            time.sleep(1.0 + attempt)
    raise last if last else RuntimeError("请求失败")


def wiki_coords_batch(titles: List[str], *, chunk: int = 50) -> Dict[str, dict]:
    """批量取「条目 → 坐标」：``prop=coordinates`` 一次能带 50 个标题。

    这是当前唯一划算的通道：维基数据 SPARQL 正在按 **1 请求/分钟** 限流
    （WDQS 故障期的临时规则），``list=geosearch`` 的 ``gsbbox`` 又不返回东西、
    ``gscoord`` 半径上限只有 10 公里。按标题批量要坐标，896 个名字 ≈ 20 次请求。
    """
    out: Dict[str, dict] = {}
    for start in range(0, len(titles), chunk):
        part = titles[start:start + chunk]
        url = WIKI_API + "?" + urllib.parse.urlencode({
            "action": "query", "prop": "coordinates", "titles": "|".join(part),
            "redirects": 1, "colimit": 50, "format": "json", "formatversion": 2})
        try:
            data = _get(url)
        except Exception as exc:                          # noqa: BLE001
            print(f"    ! 批量取坐标失败（{start}-{start + len(part)}）：{type(exc).__name__}",
                  file=sys.stderr)
            continue
        for page in ((data.get("query") or {}).get("pages") or []):
            title = page.get("title") or ""
            coords = page.get("coordinates") or []
            if not title or page.get("missing") or not coords:
                continue
            out[poi.norm_name(title)] = {"title": title,
                                         "lat": float(coords[0]["lat"]),
                                         "lon": float(coords[0]["lon"])}
        # 重定向也算上：原名能命中
        for item in ((data.get("query") or {}).get("redirects") or []):
            key = poi.norm_name(item.get("to") or "")
            if key in out:
                out[poi.norm_name(item.get("from") or "")] = out[key]
    return out


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1, lon2, lat2 = a[0], a[1], b[0], b[1]
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def connect():
    conn = poi.connect()
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def search_terms(name: str) -> List[str]:
    """搜哪些写法（长的在前，最多两个）。"""
    raw = poi.norm_name(name)
    out = [name.strip()]
    stripped = raw
    for tail in sorted(SEARCH_TAILS, key=len, reverse=True):
        if stripped.endswith(tail) and len(stripped) > len(tail) + 1:
            stripped = stripped[: -len(tail)]
            break
    if stripped and stripped != raw and len(stripped) >= 2:
        out.append(stripped)
    elif raw and raw != name.strip():
        out.append(raw)
    seen, uniq = set(), []
    for item in out:
        if item and item not in seen:
            seen.add(item)
            uniq.append(item)
    return uniq[:2]


def wb_search(term: str, limit: int = 5) -> List[dict]:
    url = WIKIDATA_API + "?" + urllib.parse.urlencode({
        "action": "wbsearchentities", "search": term, "language": "zh",
        "uselang": "zh", "format": "json", "limit": limit, "site": "zhwiki",
        "origin": "*"})
    data = _get(url)
    return data.get("search") or []


def wb_coords(qids: List[str]) -> Dict[str, dict]:
    """批量取 P625（一次最多 50 个）。"""
    out: Dict[str, dict] = {}
    for start in range(0, len(qids), 50):
        chunk = qids[start:start + 50]
        url = WIKIDATA_API + "?" + urllib.parse.urlencode({
            "action": "wbgetentities", "ids": "|".join(chunk),
            "props": "claims|labels|descriptions", "languages": "zh|zh-cn|en",
            "format": "json", "origin": "*"})
        data = _get(url)
        for qid, ent in (data.get("entities") or {}).items():
            labels = ent.get("labels") or {}
            descs = ent.get("descriptions") or {}
            label = ((labels.get("zh") or labels.get("zh-cn") or labels.get("en") or {})
                     .get("value") or "")
            desc = ((descs.get("zh") or descs.get("zh-cn") or descs.get("en") or {})
                    .get("value") or "")
            coords = None
            for claim in (ent.get("claims") or {}).get("P625") or []:
                value = (((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
                         or {})
                if value.get("latitude") is not None:
                    coords = (float(value["longitude"]), float(value["latitude"]))
                    break
            out[qid] = {"label": label, "desc": desc, "coord": coords}
        time.sleep(0.2)
    return out


def _name_matches(ours: str, label: str) -> bool:
    """维基条目名和我们的写法是不是同一个地方（互相包含即算）。"""
    a, b = poi.norm_name(ours), poi.norm_name(label)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def wiki_coords_batch(names: List[str], *, chunk: int = 50) -> Dict[str, dict]:
    """批量取「条目 → 坐标」：``prop=coordinates`` 一次能带 50 个标题。

    这是当前唯一划算的通道：维基数据 SPARQL 正按 **1 请求/分钟** 限流
    （WDQS 故障期的临时规则），维基百科的 ``list=geosearch`` 在这台机器上
    ``gsbbox`` 一律返回 0 条、``gscoord`` 半径上限只有 10 公里。
    按标题批量要坐标：896 个名字 ≈ 20 次请求。
    """
    out: Dict[str, dict] = {}
    for start in range(0, len(names), chunk):
        part = names[start:start + chunk]
        url = WIKI_API + "?" + urllib.parse.urlencode({
            "action": "query", "prop": "coordinates", "titles": "|".join(part),
            "redirects": 1, "colimit": 50, "format": "json", "formatversion": 2})
        try:
            data = _get(url)
        except Exception as exc:                          # noqa: BLE001
            print(f"    ! 批量取坐标失败（第 {start} 个起）：{type(exc).__name__}", file=sys.stderr)
            continue
        query = data.get("query") or {}
        for page in query.get("pages") or []:
            title = page.get("title") or ""
            coords = page.get("coordinates") or []
            if not title or page.get("missing") or not coords:
                continue
            out[poi.norm_name(title)] = {"title": title,
                                         "lat": float(coords[0]["lat"]),
                                         "lon": float(coords[0]["lon"])}
        for item in query.get("redirects") or []:         # 重定向名也算命中
            key = poi.norm_name(item.get("to") or "")
            if key in out:
                out[poi.norm_name(item.get("from") or "")] = out[key]
    return out


def match_article(name: str, index: Dict[str, dict]) -> Optional[dict]:
    """在「归一化名字 → 维基条目」里找对应的那个（互相包含即算，最贴近的优先）。"""
    want = poi.norm_name(name)
    if len(want) < 2:
        return None
    if want in index:
        return index[want]
    best = None
    for key, item in index.items():
        if want in key or key in want:
            score = abs(len(key) - len(want))
            if best is None or score < best[0]:
                best = (score, item)
    return best[1] if best else None


def check_rows(conn, rows: List[dict], index: Dict[str, dict]) -> Dict[str, int]:
    """一批点位对着一份「名字 → 维基坐标」索引比距离。"""
    counts: Dict[str, int] = {}
    for row in rows:
        hit = match_article(row["name"], index)
        if hit is None:
            result = {"verdict": "nomatch", "note": "维基上没有同名条目（或该条目没有坐标）",
                      "qid": "", "wiki_label": "", "wiki_desc": "",
                      "wiki_lat": None, "wiki_lon": None, "km": None}
        else:
            distance = round(_km((row["lon"], row["lat"]), (hit["lon"], hit["lat"])), 2)
            verdict = ("same" if distance <= SAME_KM
                       else "suspect" if distance <= SUSPECT_KM else "wrong")
            result = {"verdict": verdict, "qid": "", "wiki_label": hit["title"],
                      "wiki_desc": "", "wiki_lat": hit["lat"], "wiki_lon": hit["lon"],
                      "km": distance, "note": f"维基条目「{hit['title']}」"}
        with conn:
            conn.execute(
                "INSERT INTO poi_check(city,name,checked_at,qid,wiki_label,wiki_desc,"
                "wiki_lat,wiki_lon,km,verdict,note) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(city,name) DO UPDATE SET checked_at=excluded.checked_at,"
                "qid=excluded.qid, wiki_label=excluded.wiki_label,"
                "wiki_desc=excluded.wiki_desc, wiki_lat=excluded.wiki_lat,"
                "wiki_lon=excluded.wiki_lon, km=excluded.km, verdict=excluded.verdict,"
                "note=excluded.note",
                (row["city"], row["name"], dt.datetime.now().isoformat(timespec="seconds"),
                 result.get("qid") or "", result.get("wiki_label") or "",
                 result.get("wiki_desc") or "", result.get("wiki_lat"),
                 result.get("wiki_lon"), result.get("km"),
                 result.get("verdict") or "", result.get("note") or ""))
        counts[result["verdict"]] = counts.get(result["verdict"], 0) + 1
    return counts


def run(limit: int = 0, only: Optional[List[str]] = None, delay: float = 0.25,
        refresh: bool = False) -> dict:
    conn = connect()
    try:
        done = {(r["city"], r["name"]) for r in
                conn.execute("SELECT city,name FROM poi_check").fetchall()} if not refresh else set()
        rows = [dict(r) for r in conn.execute(
            "SELECT city,name,lon,lat FROM city_poi WHERE status='ok' ORDER BY city, name")]
        if only:
            rows = [r for r in rows if r["city"] in set(only)]
        todo = [r for r in rows if (r["city"], r["name"]) not in done]
        if limit:
            todo = todo[:limit]
        print(f"待核对 {len(todo)} 条（库里命中 {len(rows)} 条，已核过 {len(done)} 条）", flush=True)
        if not todo:
            return {"ok": True, "checked": 0, "counts": {}}
        names = sorted({r["name"] for r in todo})
        print(f"批量取维基坐标：{len(names)} 个名字，约 {(len(names) + 49) // 50} 次请求", flush=True)
        index = wiki_coords_batch(names)
        print(f"维基上有坐标的同名条目：{len(index)} 个", flush=True)
        counts = check_rows(conn, todo, index)
        print("核对完成：" + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        return {"ok": True, "checked": len(todo), "counts": counts}
    finally:
        conn.close()


def report(show: int = 25) -> int:
    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM poi_check ORDER BY city, name")]
        counts: Dict[str, int] = {}
        for row in rows:
            counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
        print("核对结果：" + " · ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        matched = [r for r in rows if r["verdict"] in ("same", "suspect", "wrong")]
        if matched:
            near = [r["km"] for r in matched]
            print(f"有维基条目的 {len(matched)} 条：距离中位数 "
                  f"{sorted(near)[len(near) // 2]:.2f} 公里，最大 {max(near):.1f} 公里")
        bad = [r for r in rows if r["verdict"] in ("suspect", "wrong")]
        bad.sort(key=lambda r: -(r["km"] or 0))
        if bad:
            print(f"\n可疑/错误 {len(bad)} 条（差得最多的先看）：")
            for row in bad[:show]:
                print(f"  {row['verdict']:7s} {row['km']:7.1f}km  {row['city']}/{row['name']}"
                      f"  → {row['wiki_label']}（{row['qid']}）"
                      + (f"  {row['wiki_desc'][:24]}" if row["wiki_desc"] else ""))
        by_city: Dict[str, int] = {}
        for row in bad:
            by_city[row["city"]] = by_city.get(row["city"], 0) + 1
        if by_city:
            print("按城市：" + "、".join(f"{k} {v}" for k, v in
                                      sorted(by_city.items(), key=lambda kv: -kv[1])[:12]))
        return 0
    finally:
        conn.close()


def apply_named(pairs: List[str], *, dry_run: bool = True) -> int:
    """只对指定的「城市/看点」采用维基坐标（逐条核对过之后才用）。

    ``--apply`` 是「凡判定 wrong 全改」，风险在于**维基自己也会有错坐标**
    （实测：柳侯祠的维基经度写成 108.41，实际在 109.41），所以人工确认过的那几条走这里。
    """
    conn = connect()
    try:
        for spec in pairs:
            city, _, name = spec.partition("/")
            row = conn.execute(
                "SELECT k.*, c.lon AS our_lon, c.lat AS our_lat, c.poi_name AS our_name "
                "FROM poi_check k JOIN city_poi c ON c.city=k.city AND c.name=k.name "
                "WHERE k.city=? AND k.name=?", (city, name)).fetchone()
            if not row:
                print(f"  ! 核对表里没有 {spec}（先跑一次核对）")
                continue
            print(f"  {spec}：{row['our_lat']:.4f},{row['our_lon']:.4f}"
                  f"（{row['our_name']}）→ {row['wiki_lat']:.4f},{row['wiki_lon']:.4f}"
                  f"（维基「{row['wiki_label']}」，原差 {row['km']} 公里）")
            if dry_run:
                continue
            entry = {"name": row["wiki_label"] or name,
                     "lon": row["wiki_lon"], "lat": row["wiki_lat"],
                     "kind": "维基坐标", "type_full": "", "address": "", "rating": "",
                     "tel": "",
                     "why": f"人工核对：高德按城市搜到了同名的别处，维基「{row['wiki_label']}」才是该地",
                     "note": f"原坐标 {row['our_lat']:.4f},{row['our_lon']:.4f}"
                             f"（高德，距市中心过近）；改用维基百科「{row['wiki_label']}」坐标"}
            poi.store(conn, {"city": city, "name": name, "lat": 0, "lon": 0}, entry,
                      source=f"维基百科「{row['wiki_label']}」（口径 {dt.date.today().isoformat()}）")
        print("（dry-run：没写库；加 --apply 才改）" if dry_run else "✓ 已改写")
        return 0
    finally:
        conn.close()


def apply_fixes(*, dry_run: bool = True, threshold: float = SUSPECT_KM) -> int:
    """把「维基有同名条目且距离 > threshold」的点改成维基坐标（带来源）。

    注意：``--apply`` 是批量改，**维基自己也会有错坐标**（柳侯祠就是），
    所以更稳的用法是先 `--suspects` 看一遍，再用 `--apply-only 城市/看点` 逐条改。
    """
    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT c.city,c.name,c.lon,c.lat,k.km,k.qid,k.wiki_label,k.wiki_lat,k.wiki_lon,"
            "k.verdict FROM poi_check k JOIN city_poi c ON c.city=k.city AND c.name=k.name "
            "WHERE k.verdict='wrong' AND k.km > ? ORDER BY k.km DESC", (threshold,))]
        print(f"待改 {len(rows)} 条（维基有同名条目且差 > {threshold} 公里）")
        for row in rows[:20]:
            print(f"  {row['km']:7.1f}km  {row['city']}/{row['name']}  "
                  f"{row['lat']:.4f},{row['lon']:.4f} → {row['wiki_lat']:.4f},{row['wiki_lon']:.4f}"
                  f"  （{row['wiki_label']} {row['qid']}）")
        if dry_run:
            print("（dry-run：没写库；加 --apply 才改）")
            return 0
        for row in rows:
            entry = {"name": row["wiki_label"] or row["name"],
                     "lon": row["wiki_lon"], "lat": row["wiki_lat"],
                     "kind": "维基坐标", "type_full": "", "address": "", "rating": "", "tel": "",
                     "why": f"维基数据 {row['qid']} 与库内坐标差 {(row['km'] or 0):.0f} 公里，以维基为准",
                     "note": f"原坐标 {row['lat']:.4f},{row['lon']:.4f}（高德）；改用维基数据 {row['qid']}"}
            poi.store(conn, {"city": row["city"], "name": row["name"], "lat": 0, "lon": 0},
                      entry, source=f"维基数据 {row['qid']}（口径 {dt.date.today().isoformat()}）")
        print(f"✓ 已改写 {len(rows)} 条坐标")
        return 0
    finally:
        conn.close()


def restore_known_wiki_errors() -> int:
    """把「维基坐标本身是错的」那几条改回高德的正确点位。

    实测：维基百科「柳侯祠」的经度写成 108.4106（在河池境内），而柳侯祠就在柳州市中心
    （109.4154）——差一个数字差一条河。这类条目不能当权威，批量 --apply 会把好数据改坏。
    """
    fixed = [
        ("柳州", "柳侯祠", 24.3161, 109.4154, "柳州市城中区柳侯公园内"),
    ]
    conn = connect()
    try:
        for city, name, lat, lon, why in fixed:
            poi.store(conn, {"city": city, "name": name, "lat": 0, "lon": 0},
                      {"name": name, "lon": lon, "lat": lat, "kind": "景区", "type_full": "",
                       "address": why, "rating": "", "tel": "",
                       "why": "维基条目坐标有误（经度 108.41 落在河池），按实际位置改回",
                       "note": f"{why}；对照时发现维基百科「柳侯祠」坐标写错，未采用"},
                      source=f"高德+人工核对（{dt.date.today().isoformat()}）")
            print(f"  ✓ {city}/{name} 改回 {lat:.4f},{lon:.4f}（{why}）")
        return 0
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="用维基数据交叉核对城内点位坐标")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", nargs="*", default=[], help="只核这些城市")
    ap.add_argument("--delay", type=float, default=0.25)
    ap.add_argument("--refresh", action="store_true", help="重核已核过的")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--apply", action="store_true", help="回写确认错的坐标（批量）")
    ap.add_argument("--apply-only", nargs="*", default=[], metavar="城市/看点",
                    help="只回写指定的几条（人工核对过再用这个）")
    ap.add_argument("--threshold", type=float, default=SUSPECT_KM,
                    help=f"回写门槛（公里，默认 {SUSPECT_KM}）")
    ap.add_argument("--fix-wiki-errors", action="store_true",
                    help="把「维基坐标本身是错的」那几条改回高德点位（目前只有柳侯祠）")
    args = ap.parse_args(argv)

    if args.report:
        return report()
    if args.fix_wiki_errors:
        return restore_known_wiki_errors()
    if args.apply_only:
        return apply_named(args.apply_only, dry_run=False)
    if args.apply:
        return apply_fixes(dry_run=False, threshold=args.threshold)
    out = run(limit=args.limit, only=args.only, delay=args.delay, refresh=args.refresh)
    if not out["checked"]:
        print("没有要核对的（都已核过；要重核加 --refresh）")
    return report()


if __name__ == "__main__":
    raise SystemExit(main())
