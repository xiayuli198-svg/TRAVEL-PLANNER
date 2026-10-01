"""城市漫游（地图选城 → 自动成环 → 乘车表）服务层。

和「单程规划 / 大环线」同级的一种规划方式，区别只在于**怎么选出城市**：
用户在中国地图上按大区点城市，点成一串（编号 1、2、3…），
本模块把这一串交给大环线引擎（``service.do_loop``）算出逐日乘车表，
同时把城市旅游资料（半日/一日路线、看点、建议停留）拼进每一天。
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import catalog, drive, service, tourism

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PHOTO_DIR = PROJECT_ROOT / "web" / "img" / "cities"
PHOTO_URL = "/static/img/cities"

# 默认换乘预留：地图选城时城市之间多是同站换乘，给 30 分钟更稳。
DEFAULT_BUFFER_MIN = 30
NEAR_ROUTE_KM = 150.0        # 「顺路可加」判定：距所选路线 150 公里内
EARTH_R = 6371.0


# ---------------------------------------------------------------- 小工具
PHOTO_MANIFEST = PHOTO_DIR / "photos.json"
_photo_cache: Dict[str, object] = {"mtime": None, "data": {}}


def _photo_manifest() -> dict:
    """web/img/cities/photos.json（由 scripts/fetch_city_photos.py 生成），按 mtime 缓存。"""
    try:
        mtime = PHOTO_MANIFEST.stat().st_mtime
    except OSError:
        return {}
    if _photo_cache["mtime"] != mtime:
        try:
            _photo_cache["data"] = json.loads(PHOTO_MANIFEST.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            _photo_cache["data"] = {}
        _photo_cache["mtime"] = mtime
    return _photo_cache["data"]  # type: ignore[return-value]


def _photo_urls(city: dict) -> Dict[str, Optional[str]]:
    """白天照 / 夜景照的地址（带版本号，换图后浏览器不会用旧缓存）。"""
    from urllib.parse import quote
    name = city.get("photo") or city["name"]
    manifest = _photo_manifest()
    entry = manifest.get(name) or {}
    version = int(_photo_cache["mtime"] or 0)
    urls: Dict[str, Optional[str]] = {}
    for kind, suffix in (("day", ""), ("night", "@night")):
        if entry:
            exists = bool(entry.get(kind))
        else:
            exists = any((PHOTO_DIR / f"{name}{suffix}{ext}").exists()
                         for ext in (".jpg", ".jpeg", ".webp", ".png"))
        urls[kind] = f"{PHOTO_URL}/{quote(name + suffix)}.jpg?v={version}" if exists else None
    return urls


def city_card(item: dict, with_photo: bool = True) -> dict:
    """城市卡片：地图上的图片卡 + 悬停简介 + 行程里的游玩建议。"""
    photos = _photo_urls(item) if with_photo else {"day": None, "night": None}
    night = bool(item.get("night"))
    # 适合夜游的城市优先用夜景照；缺哪张就退回另一张
    chosen = (photos["night"] if night else photos["day"]) or photos["day"] or photos["night"]
    guide = item.get("guide") or {}
    tier = drive.city_tier(item["name"])
    model = drive.rental_model()
    daily = (model.get("tiers") or {}).get(tier, {}).get("SUV") or [140, 240]
    return {
        "name": item["name"],
        "region": item.get("region") or "",
        "province": item.get("province") or "",
        "lon": item.get("lon"),
        "lat": item.get("lat"),
        "score": round(float(item.get("score") or 0), 1),
        "hours": float(item.get("hours") or 0),
        "days": float(item.get("days") or 0),
        "tags": list(item.get("tags") or []),
        "intro": item.get("intro") or "",
        "halfday": item.get("halfday") or "",
        "oneday": item.get("oneday") or "",
        "highlights": list(item.get("highlights") or []),
        "food": item.get("food") or "",
        "note": item.get("note") or "",
        "best_season": item.get("best_season") or "",
        "hub": bool(item.get("hub")),
        "rail": bool(item.get("rail", True)),
        "plan_name": item.get("plan_name") or item["name"],
        "photo": chosen,
        "photo_day": photos["day"],
        "photo_night": photos["night"],
        "night": night,
        "has_guide": bool(guide),
        "guide_days": guide.get("days"),
        "rental_tier": tier,
        "rental_daily": list(daily),
        "car_class": "SUV",
    }


def city_detail(name: str) -> dict:
    """单座城市的完整攻略（点「需要更多攻略」时才请求，避免首屏过大）。"""
    item = catalog.city(name)
    if not item:
        return {"ok": False, "error": f"未收录城市：{name}"}
    card = city_card(item)
    guide = dict(item.get("guide") or {})
    try:
        conn = tourism.connect()
        try:
            row = tourism.get(conn, item["name"]) or {}
        finally:
            conn.close()
        if row.get("guide"):
            guide = dict(row["guide"])
        for key in ("intro", "halfday", "oneday", "note", "food", "best_season"):
            if row.get(key):
                card[key] = row[key]
        if row.get("highlights"):
            card["highlights"] = row["highlights"]
    except Exception:                       # 攻略优先用内置目录，数据库读不到也不影响
        pass
    return {
        "ok": True,
        "city": card,
        "guide": guide,
        "has_guide": bool(guide),
    }


def map_payload(conn=None) -> dict:
    """地图初始化用的一次性数据：大区 + 全部城市卡 + 经典环线。"""
    regions = []
    for region in catalog.regions():
        cities = catalog.cities(region["id"])
        regions.append({
            "id": region["id"],
            "name": region["name"],
            "subtitle": region.get("subtitle") or "",
            "blurb": region.get("blurb") or "",
            "provinces": list(region.get("provinces") or []),
            "color": region.get("color") or "#6ee7c8",
            "themes": list(region.get("themes") or []),
            "city_count": len(cities),
            "rail_count": sum(1 for c in cities if c.get("rail", True)),
            "top": [c["name"] for c in cities[:4]],
        })
    cities = [city_card(c) for c in catalog.cities()]
    loops = []
    for item in catalog.loops():
        loops.append({
            "id": item["id"],
            "name": item["name"],
            "subtitle": item.get("subtitle") or "",
            "blurb": item.get("blurb") or "",
            "tips": list(item.get("tips") or []),
            "days": item.get("days"),
            "season": item.get("season") or "",
            "plan_mode": item.get("plan_mode") or "rail",
            "cities": item.get("names") or [],
            "plan_names": item.get("plan_names") or [],
            "regions": item.get("regions") or [],
            "cross_region": bool(item.get("cross_region")),
            "bounds": item.get("bounds"),
            "missing": item.get("missing") or [],
            "drive": drive.loop_drive(item["id"]),
            "stops": item.get("stops") or [],
        })
    return {
        "ok": True,
        "regions": regions,
        "cities": cities,
        "loops": loops,
        "stats": catalog.stats(),
    }


def region_cities(region: str) -> dict:
    items = catalog.cities(region)
    if not items:
        return {"ok": False, "error": f"未找到大区「{region}」", "cities": []}
    return {"ok": True, "region": region,
            "cities": [city_card(c) for c in items]}


# ---------------------------------------------------------------- 停留推导
def auto_stay(city: Optional[dict]) -> dict:
    """按城市建议游玩天数推导停留方式（前端可逐城覆盖）。"""
    days = float((city or {}).get("days") or 1.0)
    if days < 0.9:
        return {"mode": "halfday", "nights": 0}
    if days < 2:
        return {"mode": "nights", "nights": 1}
    if days < 3:
        return {"mode": "nights", "nights": 2}
    return {"mode": "nights", "nights": 3}


def _stay_dict(value) -> dict:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):        # pydantic（Web 层传入）
        value = value.model_dump()
    if isinstance(value, str):
        return {"mode": value, "nights": 1 if value == "nights" else 0}
    if isinstance(value, dict):
        mode = value.get("mode") or "nights"
        nights = int(value.get("nights") or 0)
        if mode == "nights" and nights <= 0:
            nights = 1
        return {"mode": mode, "nights": nights,
                "rest_days": max(0, int(value.get("rest_days") or 0))}
    nights = int(value)
    return {"mode": "transit", "nights": 0} if nights <= 0 else {"mode": "nights", "nights": nights}


def _haversine(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lon1, lat1 = math.radians(a[0]), math.radians(a[1])
    lon2, lat2 = math.radians(b[0]), math.radians(b[1])
    dlon, dlat = lon2 - lon1, lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(h)))


def _distance_to_segment(point: Tuple[float, float], a: Tuple[float, float],
                         b: Tuple[float, float]) -> float:
    """点到线段的地面距离（公里）的近似：在局部平面里做投影。"""
    scale = math.cos(math.radians(point[1]))
    px, py = (point[0] - a[0]) * scale, point[1] - a[1]
    bx, by = (b[0] - a[0]) * scale, b[1] - a[1]
    denom = bx * bx + by * by
    t = 0.0 if denom == 0 else max(0.0, min(1.0, (px * bx + py * by) / denom))
    cx, cy = t * bx, t * by
    return math.hypot(px - cx, py - cy) * 111.32


def near_route(selected: Sequence[dict], candidates: Iterable[dict],
               limit: int = 5, max_km: float = NEAR_ROUTE_KM) -> List[dict]:
    """找出「路线附近但没选」的城市，作为可加站建议。"""
    points = [(c["lon"], c["lat"]) for c in selected if c.get("lon") is not None]
    if len(points) < 2:
        return []
    segments = list(zip(points, points[1:]))
    if len(points) > 2 and points[0] == points[-1]:
        pass
    chosen = {c["name"] for c in selected}
    scored = []
    for item in candidates:
        if item["name"] in chosen or item.get("lon") is None:
            continue
        if not item.get("rail", True):
            continue
        point = (item["lon"], item["lat"])
        km = min(_distance_to_segment(point, a, b) for a, b in segments)
        if km <= max_km:
            scored.append((km, -float(item.get("score") or 0), item))
    scored.sort(key=lambda x: (x[0], x[1]))
    out = []
    for km, _, item in scored[:limit]:
        out.append({
            "name": item["name"],
            "region": item.get("region"),
            "score": round(float(item.get("score") or 0), 1),
            "km": int(round(km)),
            "halfday": (item.get("halfday") or "")[:40],
        })
    return out


def _top_candidates(candidates: Iterable[dict], selected: Sequence[dict],
                    limit: int = 5) -> List[dict]:
    chosen = {c["name"] for c in selected}
    pool = [c for c in candidates
            if c["name"] not in chosen and c.get("rail", True)]
    pool.sort(key=lambda c: -float(c.get("score") or 0))
    return [{"name": c["name"], "region": c.get("region"),
             "score": round(float(c.get("score") or 0), 1),
             "halfday": (c.get("halfday") or "")[:40]} for c in pool[:limit]]


# ---------------------------------------------------------------- 规划
def plan(conn, date: str, cities: Sequence[str], closed: bool = True,
         free_order: bool = False, stays: Optional[Sequence] = None,
         time: str = "08:00", mode: str = "mixed", max_transfers: int = 2,
         objective: str = "fast", slack_hours: int = 2, max_days: Optional[int] = None,
         links: bool = False, guide: bool = True, buffer_min: int = DEFAULT_BUFFER_MIN,
         rest_days: int = 0) -> dict:
    """按选中的城市顺序排一条乘车表；closed=True 时回到起点成环。"""
    picked: List[dict] = []
    unknown: List[str] = []
    seen = set()
    for name in cities or []:
        item = catalog.city(name)
        if not item:
            unknown.append(name)
            continue
        if closed and picked and item["name"] == picked[0]["name"]:
            continue          # 前端可能已经带了回到起点的那一项
        if item["name"] in seen:
            continue
        seen.add(item["name"])
        picked.append(item)
    if unknown:
        return {"ok": False, "error": "未收录的城市：" + "、".join(unknown)}
    if len(picked) < 2:
        return {"ok": False, "error": "至少选择 2 座城市（形成环线需要 2 座以上）"}
    if len(picked) > 12:
        return {"ok": False, "error": f"一次最多排 12 站，当前 {len(picked)} 站，请分批规划"}

    order_cities = list(picked)
    stops = [c.get("plan_name") or c["name"] for c in order_cities]
    if closed:
        stops.append(stops[0])

    # 每个城市的停留方式（前端可逐城覆盖），默认按建议游玩天数推导。
    per_city: List[dict] = []
    provided = list(stays or [])
    for index, item in enumerate(order_cities):
        if index < len(provided) and provided[index]:
            per_city.append(_stay_dict(provided[index]) or auto_stay(item))
        else:
            per_city.append(auto_stay(item))

    # do_loop 的 stays[i] 表示「到达 stops[i+1] 后的停留」，
    # 所以：环线 = 每个城市自己的停留整体左移一位（最后一段是回到起点）；
    #       单程 = 起点不参与，终点那段无意义。
    count = len(order_cities)
    if closed:
        stay_specs = [per_city[(i + 1) % count] for i in range(count)]
    else:
        stay_specs = [per_city[i + 1] for i in range(count - 1)]
    while len(stay_specs) < len(stops) - 1:
        stay_specs.append({"mode": "transit", "nights": 0})
    stay_specs = stay_specs[: len(stops) - 1]

    # 起点城市的停留 = 出发前先玩/先住，整体后移出发日
    start_spec = per_city[0] if per_city else {}
    pre_days = int(start_spec.get("nights") or 0) if start_spec.get("mode") == "nights" else 0
    depart_date = (dt.date.fromisoformat(date) + dt.timedelta(days=pre_days)).isoformat()

    result = service.do_loop(
        conn, depart_date, stops, stay_specs,
        free_order=free_order and len(stops) > 2,
        time=time, max_transfers=max_transfers, mode=mode,
        links=links, guide=guide, buffer_min=buffer_min,
        objective=objective, slack_hours=slack_hours,
        max_days=max_days, rest_days=rest_days,
    )
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error") or "规划失败"}

    # 起点城市「住 N 晚」= 出发前先在这里停 N 天：整体后移出发日，
    # 并把行程里的第 N 天编号补回来，保证日历与「第几天」一致。
    if pre_days:
        for leg in result.get("legs") or []:
            leg["day_no"] = int(leg.get("day_no") or 1) + pre_days
        result["total_days"] = (result.get("total_days") or 0) + pre_days

    # 城市资料库（含用户改过的记录）覆盖/补齐每天的城市介绍。
    tourism_conn = tourism.connect()
    try:
        guides = {}
        for item in order_cities:
            row = tourism.get(tourism_conn, item["name"]) or {}
            card = city_card(item)
            for key in ("intro", "halfday", "oneday", "highlights", "tags", "note",
                        "best_season", "food"):
                if row.get(key):
                    card[key] = row[key]
            if row.get("recommendation_score") is not None:
                card["score"] = round(float(row["recommendation_score"]), 1)
            card["highlight_names"] = ([h["name"] for h in card.get("highlights") or []]
                                       if isinstance(card.get("highlights"), list) else [])
            guides[item["name"]] = card
    finally:
        tourism_conn.close()

    for leg in result.get("legs") or []:
        display = catalog.display_name(leg.get("to") or "")
        card = guides.get(display)
        if card:
            leg["guide"] = card
            leg["city"] = display
            leg["city_tourism"] = {
                "city": display,
                "recommendation_score": card["score"],
                "intro": card["intro"],
                "halfday_plan": card["halfday"],
                "tags": ",".join(card["tags"]) if isinstance(card["tags"], list) else card["tags"],
                "highlights": card.get("highlights") or [],
            }
    result["guides"] = guides

    warnings = list(result.get("warnings") or [])
    start_notes: List[str] = []
    if pre_days:
        start_notes.append(
            f"起点「{order_cities[0]['name']}」先住 {pre_days} 晚，出发日顺延为 {depart_date}")
    elif start_spec.get("mode") == "halfday":
        start_notes.append(
            f"起点「{order_cities[0]['name']}」按玩半天安排：当天仍按所选最早时间出发，"
            "想上午先玩可把「每段最早出发」改到 13:00 之后")
    no_rail = [c["name"] for c in order_cities if not c.get("rail", True)]
    if no_rail and mode != "air":
        warnings.append(
            "这些城市目前没有铁路客运（需飞机或包车接驳）：" + "、".join(no_rail)
            + "；跨这些点时建议把「方式」切到空铁联运或纯飞机。")
    result["warnings"] = warnings

    travel_min = 0
    price = 0.0
    has_price = False
    for leg in result.get("legs") or []:
        journey = leg.get("journey") or {}
        if journey:
            travel_min += max(0, int(journey.get("arr_min", 0)) - int(journey.get("dep_min", 0)))
            if journey.get("price") is not None:
                price += float(journey["price"])
                has_price = True
    play_hours = sum(float((guides.get(c["name"]) or {}).get("hours") or 0)
                     for c in order_cities)

    regions: List[str] = []
    for item in order_cities:
        if item.get("region") and item["region"] not in regions:
            regions.append(item["region"])

    all_cities = catalog.cities()
    result.update({
        "ok": True,
        "kind": "roam",
        "closed": bool(closed),
        "order_cities": [c["name"] for c in order_cities],
        "plan_order": stops,
        "cities": [guides[c["name"]] for c in order_cities],
        "regions": regions,
        "start_notes": start_notes,
        "summary": {
            "city_count": len(order_cities),
            "total_days": result.get("total_days") or 0,
            "play_hours": round(play_hours, 1),
            "travel_hours": round(travel_min / 60.0, 1),
            "regions": regions,
            "price": round(price, 0) if has_price else None,
            "price_text": f"约¥{int(round(price))}" if has_price else None,
            "closed": bool(closed),
            "mode": mode,
            "date": date,
            "depart_date": depart_date,
            "pre_days": pre_days,
        },
        "suggestions": {
            "near_route": near_route(
                order_cities + ([order_cities[0]] if closed and len(order_cities) > 2 else []),
                all_cities),
            "top_rated": _top_candidates(all_cities, order_cities),
        },
    })
    return result


def itinerary_rows(result: dict) -> List[dict]:
    """把规划结果摊平成「第几天 / 做什么」的乘车表行，CLI 与前端共用。"""
    rows: List[dict] = []
    for leg in result.get("legs") or []:
        journey = leg.get("journey") or {}
        rows.append({
            "day_no": leg.get("day_no"),
            "date": leg.get("date"),
            "frm": leg.get("frm"),
            "to": leg.get("to"),
            "city": leg.get("city") or leg.get("to"),
            "stay": leg.get("stay"),
            "dep": journey.get("dep_text"),
            "arr": journey.get("arr_text"),
            "rides": journey.get("rides"),
            "price_text": journey.get("price_text"),
            "error": leg.get("error") or "",
        })
    return rows


# ---------------------------------------------------------------- 攻略联动
#: 换乘等待的档位（小时）：低于「吃一顿」就别出站，高于「半日」才值得按半日线走
LAYOVER_MEAL_H = 2.0
LAYOVER_HALFDAY_H = 4.0


def layover_hint(card: dict, minutes) -> str:
    """换乘等待够不够出站逛 —— 只按时间说话，不承诺「一定逛得完」。"""
    if not minutes:
        return ""
    hours = float(minutes) / 60.0
    suggest = float(card.get("hours") or 0)
    if hours < LAYOVER_MEAL_H:
        return (f"换乘只有 {int(minutes)} 分钟：建议别出站，站内解决吃饭与休息")
    if hours < LAYOVER_HALFDAY_H or (suggest and hours < suggest):
        tail = f"；这座城建议玩 {suggest:g} 小时，完整逛要另留时间" if suggest else ""
        return f"换乘约 {hours:.1f} 小时：够出站吃一顿、就近走一走{tail}"
    return (f"换乘约 {hours:.1f} 小时：够按下面那条半日线走一圈 —— "
            "出站前先记好回站时间，别赶不上下一趟")


def city_resolver(conn=None):
    """把「站名」变成「目录里的城市名」：北京南 → 北京、上海虹桥 → 上海。

    规划结果里的 legs 写的是**车站**（北京南/上海虹桥），而城市资料按城市存；
    有数据库时用站名表（与规划引擎同一份口径），没有就退回目录的展示名映射。
    """
    station_index: Dict[str, str] = {}
    if conn is not None:
        try:
            from . import concrete
            station_index = dict(concrete._city_index(conn))
        except Exception:                                 # noqa: BLE001
            station_index = {}

    def resolve(name: str) -> str:
        text = str(name or "").strip()
        if not text:
            return ""
        if text in station_index:
            return station_index[text]
        item = catalog.city(text)
        if item:
            return item["name"]
        # 没有数据库时的退路：站名砍掉方位/后缀（北京南→北京、上海虹桥→上海），
        # 与路线素材补坐标用的是同一套「去尾巴」口径
        for cut in (1, 2, 3):
            if len(text) > cut + 1:
                item = catalog.city(text[:-cut])
                if item:
                    return item["name"]
        return catalog.display_name(text)

    return resolve


def cities_of_plan(result: dict, *, limit: int = 8, conn=None) -> List[dict]:
    """从任意规划结果里挑出「沿途涉及的城市」：起点、终点、以及换乘过的城市。

    这是**攻略与查询联动**的入口：单程、环线、漫游三种结果的形状不同，
    但都能摊出城市序列；有了序列就能把城市资料挂上去 ——
    不再要求「查询恰好命中攻略」才显示。
    """
    out: List[dict] = []
    resolve = city_resolver(conn)

    def add(name, role: str, layover=None) -> None:
        display = resolve(name)
        item = catalog.city(display)
        if not item:
            return
        key = item["name"]
        for existing in out:
            if existing["name"] == key:
                # 同一座城既是起点又换乘过：换乘信息更有用，补上
                if layover and not existing.get("layover_min"):
                    existing["layover_min"] = layover
                    existing["role"] = role
                return
        out.append({"name": key, "role": role, "layover_min": layover})

    for journey in result.get("journeys") or []:
        legs = journey.get("legs") or []
        for index, leg in enumerate(legs):
            if index == 0:
                add(leg.get("from"), "起点")
            if index + 1 < len(legs):
                wait = None
                try:
                    wait = max(0, int(legs[index + 1].get("dep_min")) - int(leg.get("arr_min")))
                except (TypeError, ValueError):
                    wait = None
                add(leg.get("to"), "中转", wait)
            add(leg.get("to"), "终点")
    for leg in result.get("legs") or []:              # 环线 / 漫游的形状
        add(leg.get("frm"), leg.get("role") or "起点")
        add(leg.get("to"), leg.get("role") or "途经")
    for name in result.get("order_cities") or []:     # 漫游显式给了顺序
        add(name, "途经")
    return out[:limit]


def plan_guides(result: dict, *, limit: int = 8, conn=None) -> dict:
    """给规划结果配上沿途城市资料（含换乘等待够不够出去逛的提示）。

    返回 ``{"cities": [...], "note": "..."}``；卡片就是地图上用的那种城市卡，
    前端展开后能直接点「看城内动线」。
    """
    entries = cities_of_plan(result, limit=limit, conn=conn)
    cards: List[dict] = []
    for entry in entries:
        item = catalog.city(entry["name"])
        if not item:
            continue
        card = city_card(item)
        card["role"] = entry.get("role") or ""
        card["layover_min"] = entry.get("layover_min")
        card["layover_hint"] = layover_hint(card, entry.get("layover_min"))
        card["action_hint"] = action_hint(card)
        cards.append(card)
    return {
        "cities": cards,
        "note": ("这些是这次查询沿途涉及的城市（起点/终点/换乘点）的资料："
                 "半日与一日安排来自城市攻略，点「看城内动线」会把它按天对齐成带坐标的动线；"
                 "换乘提示只按等待时间给建议，出站前请自己核对回站时间。"),
    }


def action_hint(card: dict) -> str:
    """这座城市「值得为它多留多久」的一句话（用于攻略卡首屏）。"""
    hours = float(card.get("hours") or 0)
    days = float(card.get("days") or 0)
    if days >= 2:
        return f"建议留 {days:g} 天（半日线 + 一日线都排得下）"
    if hours >= 6:
        return f"建议留一整天（约 {hours:g} 小时）"
    if hours >= 3:
        return f"半天够用（约 {hours:g} 小时）"
    if hours:
        return f"顺路停 2-3 小时即可（约 {hours:g} 小时）"
    return ""
