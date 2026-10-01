"""城市停留动线（B+）：把散文攻略自动对成**带坐标的有序动线**，按需算市内腿，并画示意图。

为什么这么做（而不是人工结构化 479 天动线）
--------------------------------------------
散文里已经有顺序和时间了（「9:00 地铁2号线中央大街站下…走到防洪纪念塔…」），
缺的只是「名字 → 坐标」。所以：

1. **对齐**：拿城市 POI 池（下方两层）去散文里做最长匹配，命中顺序 = 散文里的先后 = 动线顺序；
   匹配不上的名字**保持纯文字**，不猜。
2. **池子**：第一层是 `city_poi`（名单层已补好坐标的看点）；第二层是 `city_poi_pool`
   （对**你真正打开的城市**按类型抓几页高德 POI）—— 步行街、老街、商圈这些散在别的类型里的点，
   只有第二层才捞得到（「中央大街」在高德那里是「道路名」）。
3. **腿**：相邻两点之间调高德公交/步行路径规划（带缓存），拿到「几号线、在哪上、在哪下、几分钟」。
   这是**实时查询结果，不是时刻表** —— 页面上照原样标出来。

诚实边界：示意图只按经纬度摆位，不是导航；查不到的腿就写「未取到」，不编时间。
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from . import poi as poi_mod

#: 抓池子用的高德类型组（一类一页就能覆盖「景点之外的去处」）
POOL_TYPES = {
    "风景名胜": "110000",
    "科教文化": "140000",
    "购物服务": "060000",
    "体育休闲": "080000",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS city_poi_pool (
  city TEXT NOT NULL,
  name TEXT NOT NULL,
  lon REAL, lat REAL,
  kind TEXT NOT NULL DEFAULT '',
  type_full TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  rating TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'amap-type',
  fetched_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (city, name)
);
CREATE TABLE IF NOT EXISTS city_leg_cache (
  city TEXT NOT NULL,
  frm TEXT NOT NULL,
  dest TEXT NOT NULL,            -- 别叫 to：SQLite 里 to 是关键字，DDL 会直接语法错
  mode TEXT NOT NULL DEFAULT '',
  minutes INTEGER,
  distance_m INTEGER,
  walk_m INTEGER,
  lines TEXT NOT NULL DEFAULT '',
  raw TEXT NOT NULL DEFAULT '',
  fetched_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (city, frm, dest)
);
"""

#: 匹配时忽略的泛词（散文里遍地都是，当点位会闹笑话）
NOISE_NAMES = {
    "博物馆", "公园", "广场", "古城", "古镇", "步行街", "老街", "夜市", "菜市场",
    "火车站", "高铁站", "地铁站", "机场", "游客中心", "索道", "温泉", "滑雪场",
    "观景台", "湿地", "草原", "大桥", "长江", "黄河", "松花江", "市区", "老城",
}

#: 只在**匹配**时抹掉的类型尾巴：散文写「故宫」，高德的正式名是「故宫博物院」。
#: 注意这和 poi.norm_name 的用途不同 —— 那边是判断「是不是同一个地方」，
#: 抹掉「博物馆」会把「上海博物馆」变成「上海」，所以那边不抹。
MATCH_TAILS = ("博物院", "博物馆", "纪念馆", "陈列馆", "展览馆", "美术馆", "艺术馆",
               "国家级风景名胜区", "风景名胜区", "国家重点风景名胜区", "风景区", "景区",
               "国家森林公园", "森林公园", "国家地质公园", "地质公园", "湿地公园",
               "遗址公园", "公园", "广场", "步行街", "历史文化街区", "文化街区", "街区",
               "古城", "古镇", "古村", "寺", "庙", "宫", "塔", "祠", "陵", "观")


def connect(path: Optional[str | Path] = None) -> sqlite3.Connection:
    conn = poi_mod.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 池子
def fetch_pool_page(city: str, type_code: str, page: int = 1, *,
                    key: str, timeout: int = 20) -> List[Dict]:
    """高德分类检索一页（25 条）——抓的是「一座城里这类地方有哪些」。"""
    params = urllib.parse.urlencode({
        "key": key, "types": type_code, "city": city, "citylimit": "true",
        "offset": 25, "page": page, "extensions": "all", "output": "json",
    })
    url = f"{poi_mod.AMAP_PLACE}?{params}"
    last = ""
    for attempt in range(3):
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8", "replace"))
        if raw.get("status") == "1":
            break
        last = str(raw.get("info") or raw.get("status"))
        if "QPS" in last.upper() or "LIMIT" in last.upper():
            time.sleep(1.0 + attempt)
            continue
        raise RuntimeError(f"高德返回 {last}")
    else:
        raise RuntimeError(f"高德一直返回 {last}")
    out: List[Dict] = []
    for item in raw.get("pois") or []:
        loc = (item.get("location") or "").split(",")
        if len(loc) != 2:
            continue
        try:
            lon, lat = float(loc[0]), float(loc[1])
        except ValueError:
            continue
        type_full = str(item.get("type") or "")
        out.append({"name": item.get("name") or "", "lon": lon, "lat": lat,
                    "kind": (type_full.split(";")[-1] if type_full else ""),
                    "type_full": type_full, "address": item.get("address") or "",
                    "rating": str(((item.get("biz_ext") or {}) or {}).get("rating") or "")})
    return out


def ensure_pool(conn: sqlite3.Connection, city: str, *, pages: int = 1,
                delay: float = 0.3, refresh: bool = False, key: str = "",
                fetch: Optional[Callable[[str, str, int], List[Dict]]] = None) -> Dict:
    """给一座城市建/补 POI 池（4 个类型组 × pages 页，默认 4 次请求）。

    ``fetch(city, type_code, page)`` 可注入，测试不联网。
    """
    done = 0
    if not refresh:
        done = int(conn.execute("SELECT COUNT(*) n FROM city_poi_pool WHERE city=?",
                                (city,)).fetchone()["n"])
        if done:
            return {"ok": True, "city": city, "added": 0, "existing": done,
                    "note": "池子已经有了（要重抓加 refresh）"}
    if fetch is None:
        key = key or poi_mod.amap_key()
        if not key:
            return {"ok": False, "error": "没有高德 key"}
        fetch = lambda c, code, page: fetch_pool_page(c, code, page, key=key)  # noqa: E731
    added, failed = 0, []
    for label, code in POOL_TYPES.items():
        for page in range(1, max(1, pages) + 1):
            try:
                rows = fetch(city, code, page)
            except Exception as exc:                      # noqa: BLE001
                failed.append(f"{label} p{page}: {exc}")
                continue
            with conn:
                for row in rows:
                    conn.execute(
                        "INSERT INTO city_poi_pool(city,name,lon,lat,kind,type_full,address,"
                        "rating,source,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(city,name) DO UPDATE SET lon=excluded.lon, lat=excluded.lat,"
                        " kind=excluded.kind, type_full=excluded.type_full,"
                        " address=excluded.address, rating=excluded.rating,"
                        " fetched_at=excluded.fetched_at",
                        (city, row["name"], row["lon"], row["lat"], row["kind"],
                         row["type_full"], row["address"], row["rating"], "amap-type", _now()))
                    added += 1
            if len(rows) < 25:
                break
            time.sleep(max(0.0, delay))
    total = int(conn.execute("SELECT COUNT(*) n FROM city_poi_pool WHERE city=?",
                             (city,)).fetchone()["n"])
    return {"ok": True, "city": city, "added": added, "pool": total,
            "failed": failed[:5],
            "note": f"池子现有 {total} 个点位（4 类 × {pages} 页）"}


def pool_for(conn: sqlite3.Connection, city: str) -> List[Dict]:
    """对齐用的名字池：名单层命中过的看点 + 类型抓来的池子（看点优先，去重）。"""
    out: Dict[str, Dict] = {}
    for row in conn.execute(
            "SELECT name,poi_name,lon,lat,kind,rating,address,source FROM city_poi "
            "WHERE city=? AND status='ok'", (city,)).fetchall():
        key = poi_mod.norm_name(row["poi_name"] or row["name"])
        if key and key not in NOISE_NAMES:
            out[key] = {"name": row["poi_name"] or row["name"], "lon": row["lon"],
                        "lat": row["lat"], "kind": row["kind"] or "", "rating": row["rating"] or "",
                        "address": row["address"] or "", "source": "名单层",
                        "curated": row["name"]}
    for row in conn.execute(
            "SELECT name,lon,lat,kind,rating,address FROM city_poi_pool WHERE city=?",
            (city,)).fetchall():
        key = poi_mod.norm_name(row["name"])
        if key and key not in NOISE_NAMES and key not in out:
            out[key] = {"name": row["name"], "lon": row["lon"], "lat": row["lat"],
                        "kind": row["kind"] or "", "rating": row["rating"] or "",
                        "address": row["address"] or "", "source": "池子"}
    return list(out.values())


# ---------------------------------------------------------------- 对齐
def match_keys(item: Dict, city: str = "") -> List[str]:
    """一个点位在散文里可能被怎么称呼（长的在前，取最长的那个）。

    * 正式名归一（「上海博物馆(人民广场馆)」→「上海博物馆」）；
    * 再去掉类型尾巴（「故宫博物院」→「故宫」）。
    去尾之后如果只剩城市名（上海博物馆→上海）或成了泛词，就丢掉 ——
    否则「上海」两个字会在整篇散文里到处命中。
    """
    full = poi_mod.norm_name(item.get("name") or "")
    keys: List[str] = []
    if full and full not in NOISE_NAMES:
        keys.append(full)
    stripped = full
    for tail in sorted(MATCH_TAILS, key=len, reverse=True):
        if stripped.endswith(tail) and len(stripped) > len(tail) + 1:
            stripped = stripped[: -len(tail)]
            break
    if (stripped and stripped != full and len(stripped) >= 2
            and stripped not in NOISE_NAMES and stripped != city):
        keys.append(stripped)
    return keys


def extract_stops(text: str, index: Sequence[Dict], *, limit: int = 12,
                  city: str = "") -> List[Dict]:
    """在散文里按**出现顺序**找出点位（最长名字优先，避免短名吃掉长名）。

    同一个地方可能有好几种叫法（正式名 / 去掉类型尾巴的简称），取最长的那个命中。
    """
    flat = re.sub(r"\s+", "", str(text or ""))
    found: List[tuple] = []
    taken: List[tuple] = []
    candidates: List[tuple] = []
    for item in index:
        for key in match_keys(item, city or item.get("city") or ""):
            candidates.append((len(key), key, item))
    for _, key, item in sorted(candidates, key=lambda x: -x[0]):
        start = flat.find(key)
        if start < 0:
            continue
        end = start + len(key)
        if any(not (end <= a or start >= b) for a, b in taken):   # 与更长名字重叠，跳过
            continue
        taken.append((start, end))
        found.append((start, {**item, "matched": key}))
    found.sort(key=lambda x: x[0])
    return [item for _, item in found[:limit]]


def city_days(conn: sqlite3.Connection, city: str, *,
              index: Optional[Sequence[Dict]] = None) -> Dict:
    """把一座城的 guide 每天对齐成动线（不联网；池子不够时只有名单层的点）。"""
    from . import catalog

    item = catalog.city(city)
    if not item:
        return {"ok": False, "error": f"城市目录里没有「{city}」"}
    guide = item.get("guide") or {}
    pool = list(index) if index is not None else pool_for(conn, city)
    days = []
    for day in guide.get("itinerary") or []:
        text = str(day.get("detail") or "")
        stops = extract_stops(text, pool, city=item["name"])
        days.append({
            "day": str(day.get("day") or ""),
            "title": str(day.get("title") or ""),
            "detail": text,
            "stops": stops,
            "matched": len(stops),
            "drawable": len(stops) >= 2,
        })
    matched_total = sum(d["matched"] for d in days)
    return {"ok": True, "city": item["name"], "days_count": int(guide.get("days") or len(days)),
            "summary": str(guide.get("summary") or ""),
            "transport": str(guide.get("transport") or ""),
            "stay": str(guide.get("stay") or ""), "eat": str(guide.get("eat") or ""),
            "tips": list(guide.get("tips") or []), "budget": str(guide.get("budget") or ""),
            "pool": len(pool), "days": days, "matched_total": matched_total,
            "note": ("点位是按散文里的先后自动对上的（名字 → 坐标），没对上的保持纯文字；"
                     "池子越大对得越全，点「补这座城的点位池」可以再抓几页。")}


# ---------------------------------------------------------------- 市内腿
def fetch_leg(city: str, a: Dict, b: Dict, *, key: str, timeout: int = 20) -> Dict:
    """一段市内腿：先问公交/地铁，太近或没有方案再问步行。都是**实时查询**。

    直接传经纬度（不走 amap.transit_guide 的「站名」拼法）——城市内的点是景点，
    不是车站，拼「站」字会查不到。
    """
    def call(path: str) -> dict:
        params = urllib.parse.urlencode({
            "key": key, "origin": f"{a['lon']},{a['lat']}",
            "destination": f"{b['lon']},{b['lat']}", "city": city, "strategy": "0",
        })
        url = f"https://restapi.amap.com/v3/direction/{path}?{params}"
        for attempt in range(3):
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8", "replace"))
            if raw.get("status") == "1":
                return raw
            info = str(raw.get("info") or "")
            if "QPS" in info.upper() or "LIMIT" in info.upper():
                time.sleep(1.0 + attempt)
                continue
            return {}
        return {}

    gap_km = poi_mod._haversine_km((a["lon"], a["lat"]), (b["lon"], b["lat"]))
    raw = call("transit/integrated")
    transits = ((raw.get("route") or {}).get("transits") or []) if raw else []
    if transits:
        pick = sorted(transits, key=lambda t: int(t.get("duration") or 0))[0]
        lines: List[Dict[str, str]] = []
        for seg in pick.get("segments") or []:
            for bl in ((seg or {}).get("bus") or {}).get("buslines") or []:
                lines.append({"line": bl.get("name") or "",
                              "from": (bl.get("departure_stop") or {}).get("name") or "",
                              "to": (bl.get("arrival_stop") or {}).get("name") or ""})
        if lines or int(pick.get("distance") or 0) > 0:
            return {"mode": "transit", "minutes": round(int(pick.get("duration") or 0) / 60),
                    "distance_m": int(pick.get("distance") or 0),
                    "walk_m": int(pick.get("walking_distance") or 0),
                    "lines": lines, "straight_km": round(gap_km, 1)}
    raw_walk = call("walking")
    paths = ((raw_walk.get("route") or {}).get("paths") or []) if raw_walk else []
    if paths:
        first = paths[0]
        return {"mode": "walk", "minutes": round(int(first.get("duration") or 0) / 60),
                "distance_m": int(first.get("distance") or 0), "walk_m": int(first.get("distance") or 0),
                "lines": [], "straight_km": round(gap_km, 1)}
    return {}


def legs_for(conn: sqlite3.Connection, city: str, stops: Sequence[Dict], *,
             live: bool = True, delay: float = 0.3,
             fetch: Optional[Callable[[str, Dict, Dict], Dict]] = None) -> List[Dict]:
    """相邻点之间的市内腿（带缓存；``live=False`` 时只读缓存，不联网）。"""
    pairs: List[Dict] = []
    key = "" if fetch is not None else (poi_mod.amap_key() if live else "")
    for index in range(len(stops) - 1):
        a, b = stops[index], stops[index + 1]
        frm, to = a["name"], b["name"]
        row = conn.execute(
            "SELECT * FROM city_leg_cache WHERE city=? AND frm=? AND dest=?",
            (city, frm, to)).fetchone()
        if row:
            pairs.append({"frm": frm, "to": to, "cached": True,
                          "mode": row["mode"], "minutes": row["minutes"],
                          "distance_m": row["distance_m"], "walk_m": row["walk_m"],
                          "lines": json.loads(row["lines"] or "[]"),
                          "straight_km": ((json.loads(row["raw"] or "{}") or {}).get("straight_km")),
                          "fetched_at": row["fetched_at"]})
            continue
        straight = round(poi_mod._haversine_km((a["lon"], a["lat"]), (b["lon"], b["lat"])), 1)
        if not live or (fetch is None and not key):
            pairs.append({"frm": frm, "to": to, "cached": False, "mode": "",
                          "minutes": None, "lines": [], "straight_km": straight,
                          "note": "还没算这段（要联网查一次）"})
            continue
        try:
            if fetch is not None:
                leg = fetch(city, a, b)
            else:
                leg = fetch_leg(city, a, b, key=key)
        except Exception as exc:                          # noqa: BLE001
            leg = {}
            pairs.append({"frm": frm, "to": to, "cached": False, "mode": "",
                          "minutes": None, "lines": [], "straight_km": straight,
                          "note": f"查询失败：{type(exc).__name__}"})
            time.sleep(max(0.0, delay))
            continue
        if not leg:
            leg = {"mode": "", "minutes": None, "lines": [], "straight_km": straight,
                   "note": "高德没给出方案（可能太近、或该城市无公交数据）"}
        leg = {**leg, "straight_km": leg.get("straight_km", straight)}
        with conn:
            conn.execute(
                "INSERT INTO city_leg_cache(city,frm,dest,mode,minutes,distance_m,walk_m,lines,"
                "raw,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(city,frm,dest) DO UPDATE SET mode=excluded.mode,"
                " minutes=excluded.minutes, distance_m=excluded.distance_m,"
                " walk_m=excluded.walk_m, lines=excluded.lines, raw=excluded.raw,"
                " fetched_at=excluded.fetched_at",
                (city, frm, to, leg.get("mode") or "", leg.get("minutes"),
                 leg.get("distance_m"), leg.get("walk_m"),
                 json.dumps(leg.get("lines") or [], ensure_ascii=False),
                 json.dumps({"straight_km": leg.get("straight_km")}, ensure_ascii=False),
                 _now()))
        pairs.append({"frm": frm, "to": to, "cached": False, **leg})
        time.sleep(max(0.0, delay))
    return pairs


# ---------------------------------------------------------------- 示意图
def sketch_points(stops: Sequence[Dict], *, width: float = 520, height: float = 240,
                  pad: float = 34) -> List[Dict]:
    """把经纬度摆到画布上（等比缩放 + 纬度余弦修正），点太少就原样返回。"""
    pts = [s for s in stops if s.get("lon") is not None and s.get("lat") is not None]
    if not pts:
        return []
    lons = [float(p["lon"]) for p in pts]
    lats = [float(p["lat"]) for p in pts]
    lon0, lon1 = min(lons), max(lons)
    lat0, lat1 = min(lats), max(lats)
    mid = math.radians((lat0 + lat1) / 2)
    span_x = max((lon1 - lon0) * math.cos(mid), 1e-6)
    span_y = max(lat1 - lat0, 1e-6)
    scale = min((width - pad * 2) / span_x, (height - pad * 2) / span_y)
    out: List[Dict] = []
    for index, item in enumerate(pts, 1):
        x = pad + (float(item["lon"]) - lon0) * math.cos(mid) * scale
        y = height - pad - (float(item["lat"]) - lat0) * scale
        out.append({"n": index, "x": round(x, 1), "y": round(y, 1),
                    "name": item["name"], "kind": item.get("kind") or ""})
    return out


def sketch_svg(stops: Sequence[Dict], legs: Sequence[Dict] = (), *,
               width: float = 520, height: float = 240) -> str:
    """一张轻量示意图（纯 SVG，不依赖地图库）：按真实经纬度摆位，连线是顺序不是路径。

    页面上必须配一句「示意图 · 非导航」——它只说「这几个点大概在哪、按什么顺序走」。
    """
    pts = sketch_points(stops, width=width, height=height)
    if len(pts) < 2:
        return ""
    lines = []
    for index in range(len(pts) - 1):
        a, b = pts[index], pts[index + 1]
        leg = legs[index] if index < len(legs) else {}
        dashed = "" if leg.get("mode") else ' stroke-dasharray="4 4"'
        lines.append(f'<line x1="{a["x"]}" y1="{a["y"]}" x2="{b["x"]}" y2="{b["y"]}"'
                     f' stroke="#4b7fc7" stroke-width="2"{dashed} />')
        if leg.get("minutes") is not None:
            mx, my = (a["x"] + b["x"]) / 2, (a["y"] + b["y"]) / 2
            label = f'{leg["minutes"]}′'
            lines.append(f'<text x="{mx:.0f}" y="{my - 4:.0f}" font-size="10" fill="#8a5c1f"'
                         f' text-anchor="middle">{label}</text>')
    dots = []
    for point in pts:
        dots.append(f'<circle cx="{point["x"]}" cy="{point["y"]}" r="9" fill="#14574c" />'
                    f'<text x="{point["x"]}" y="{point["y"] + 3.5:.0f}" font-size="10"'
                    f' fill="#fff" text-anchor="middle">{point["n"]}</text>'
                    f'<text x="{point["x"]}" y="{point["y"] - 13:.0f}" font-size="10.5"'
                    f' fill="#33565a" text-anchor="middle">{_short(point["name"])}</text>')
    return (f'<svg class="guide-sketch" viewBox="0 0 {width:.0f} {height:.0f}" '
            f'width="100%" height="{height:.0f}" role="img">'
            f'<rect width="{width:.0f}" height="{height:.0f}" fill="#f7fbf9" rx="12" />'
            + "".join(lines) + "".join(dots) + "</svg>")


def _short(name: str, limit: int = 7) -> str:
    text = str(name or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def day_view(conn: sqlite3.Connection, city: str, day: str, *, live: bool = True,
             limits: int = 1) -> Dict:
    """一天的全部内容：动线点位 + 市内腿 + 示意图（腿按需现算并缓存）。"""
    data = city_days(conn, city)
    if not data.get("ok"):
        return data
    target = None
    for item in data["days"]:
        if item["day"] == day or item["day"].upper() == str(day).upper():
            target = item
            break
    if target is None:
        return {"ok": False, "error": f"「{city}」没有 {day} 这一天",
                "days": [d["day"] for d in data["days"]]}
    legs = legs_for(conn, data["city"], target["stops"], live=live)
    svg = sketch_svg(target["stops"], legs)
    return {"ok": True, "city": data["city"], "day": target["day"], "title": target["title"],
            "detail": target["detail"], "stops": target["stops"], "legs": legs, "svg": svg,
            "live": bool(live), "pool": data["pool"],
            "note": ("点位按散文顺序自动对齐；市内腿是**实时查询**高德得到的，不是时刻表；"
                     "图只是按经纬度摆位的示意图，不做导航。")}
