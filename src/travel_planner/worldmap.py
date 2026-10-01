"""地球模式：世界城市 / 世界经典环线 / 世界之最。

和「地图漫游」是两套数据（那套是国内的 239 城 + 26 条环线，走 12306/高德）；
这里是世界范围的城市与经典长线，交通以飞机为主，因此：

- 城市卡：照片（白天/夜景）、半日/一日路线、看点、美食、提示、完整攻略；
- 经典环线：城市顺序 + 建议天数 + 交通方式 + 实用建议（不对接具体航班时刻）；
- 世界之最：自然/工程/人文/气候四类，带坐标、数值、怎么去；
- 行程单：按选中的城市顺序给出大圆距离、预计飞行时间、时差变化与建议停留天数，
  **不给票价**（世界航班票价必须实时查询，编一个数字没有意义）。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Sequence

DATA_DIR = Path(__file__).resolve().parent / "data"

#: 环境 → 色。球上、卡片上、航线上的颜色都从这里取，
#: 让「沙漠是土黄、海洋是深蓝、极地是冰蓝」由数据决定，而不是随手挑一套浅色。
ENV_COLORS = {
    "沙漠": "#d9a441", "海洋": "#2f6f9f", "极地": "#7fc4dd", "草原": "#7fa650",
    "雪山": "#93b0c4", "古迹": "#b07a4a", "雨林": "#2f7d51", "高原": "#a8846b",
    "城市": "#6b7f95",
}
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PHOTO_DIR = PROJECT_ROOT / "web" / "img" / "world"
PHOTO_URL = "/static/img/world"
MANIFEST = PHOTO_DIR / "photos.json"

_EARTH_R = 6371.0
_FLIGHT_KMH = 820.0          # 含起降的平均巡航速度
_FLIGHT_OVERHEAD_H = 2.0     # 机场往返 + 安检登机

_cache: Dict[str, object] = {"mtime": None, "data": None}


def _catalog() -> dict:
    path = DATA_DIR / "world_catalog.json"
    if not path.exists():
        return {"cities": [], "loops": [], "records": []}
    mtime = path.stat().st_mtime
    if _cache["mtime"] != mtime:
        try:
            _cache["data"] = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            _cache["data"] = {"cities": [], "loops": [], "records": []}
        _cache["mtime"] = mtime
    return _cache["data"]  # type: ignore[return-value]


def _photos() -> dict:
    if not MANIFEST.exists():
        return {}
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {}


def _photo_url(name: str, kind: str) -> Optional[str]:
    """kind: day / night / record"""
    from urllib.parse import quote
    manifest = _photos()
    entry = manifest.get(name) or {}
    if entry:
        exists = bool(entry.get(kind))
    else:
        suffix = {"day": "", "night": "@night", "record": "record_"}[kind] + ".jpg"
        exists = (PHOTO_DIR / f"{name}{suffix}").exists()
    if not exists:
        return None
    if kind == "record":
        return f"{PHOTO_URL}/record_{quote(name)}.jpg"
    suffix = "@night" if kind == "night" else ""
    return f"{PHOTO_URL}/{quote(name + suffix)}.jpg"


def cities() -> List[dict]:
    return list(_catalog().get("cities") or [])


def city(name: str) -> Optional[dict]:
    key = (name or "").strip()
    for item in cities():
        if item["name"] == key or item.get("name_en", "").lower() == key.lower():
            return item
    return None


def loops() -> List[dict]:
    out = []
    for raw in _catalog().get("loops") or []:
        item = dict(raw)
        stops = []
        for name in item.get("cities") or []:
            c = city(name)
            if not c:
                continue
            stops.append({
                "name": c["name"], "name_en": c.get("name_en", ""),
                "country": c.get("country", ""), "area": c.get("area", ""),
                "lat": c.get("lat"), "lon": c.get("lon"),
                "score": c.get("score"), "days": c.get("days"),
            })
        item["stops"] = stops
        item["city_count"] = len(stops)
        item["km"] = _loop_km(item)
        # 环境色：沙漠土黄、海洋深蓝、极地冰蓝…（球上/卡片上按这个上色，
        # 不再用一套与地貌无关的浅色，浅底上也就看不清了）
        env = str(item.get("env") or "")
        item["env"] = env
        item["env_color"] = ENV_COLORS.get(env, "#6b7f95")
        item["env_label"] = env or "跨区"
        # 统一成 mode：海上航线（母港↔登陆点）允许只有两站，陆地环线不行，
        # 所以这个字段前端和后端都要能读到，不能只留在 plan_mode 里。
        item["mode"] = str(item.get("plan_mode") or "")
        if stops:
            item["center"] = [
                sum(s["lat"] for s in stops) / len(stops),
                sum(s["lon"] for s in stops) / len(stops),
            ]
        out.append(item)
    return out


def loop(loop_id: str) -> Optional[dict]:
    for item in loops():
        if item.get("id") == loop_id or item.get("name") == loop_id:
            return item
    return None


def records() -> List[dict]:
    return list(_catalog().get("records") or [])


def record(record_id: str) -> Optional[dict]:
    for item in records():
        if item.get("id") == record_id:
            return item
    return None


def city_card(item: dict) -> dict:
    night = bool(item.get("night"))
    day = _photo_url(item["name"], "day")
    night_photo = _photo_url(item["name"], "night")
    chosen = (night_photo if night else day) or day or night_photo
    guide = item.get("guide") or {}
    return {
        "name": item["name"],
        "name_en": item.get("name_en") or "",
        "country": item.get("country") or "",
        "area": item.get("area") or "",
        "lat": item.get("lat"), "lon": item.get("lon"),
        # 地球渲染统一读 lng（globe.gl 的字段名），这里给别名，避免前端再算一次
        "lng": item.get("lon"),
        "tz": item.get("tz"),
        "score": item.get("score"),
        "days": item.get("days"),
        "best_season": item.get("best_season") or "",
        "tags": list(item.get("tags") or []),
        "intro": item.get("intro") or "",
        "halfday": item.get("halfday") or "",
        "oneday": item.get("oneday") or "",
        "highlights": list(item.get("highlights") or []),
        "food": item.get("food") or "",
        "note": item.get("note") or "",
        "night": night,
        "photo": chosen, "photo_day": day, "photo_night": night_photo,
        "has_guide": bool(guide), "guide_days": guide.get("days"),
    }


def record_card(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "category": item.get("category"),
        "value": item.get("value"),
        "country": item.get("country") or "",
        "lat": item.get("lat"), "lon": item.get("lon"),
        "lng": item.get("lon"),
        "blurb": item.get("blurb") or "",
        "visit": item.get("visit") or "",
        "nearest_city": item.get("nearest_city") or "",
        "best_season": item.get("best_season") or "",
        "tags": list(item.get("tags") or []),
        "photo": _photo_url(item.get("id") or item.get("name"), "record"),
    }


def map_payload() -> dict:
    """地球模式初始化数据：城市卡 + 经典环线 + 世界之最。"""
    city_cards = [city_card(c) for c in cities()]
    loop_cards = []
    for item in loops():
        loop_cards.append({
            "id": item.get("id"), "name": item.get("name"),
            "subtitle": item.get("subtitle") or "",
            "cities": [s["name"] for s in item.get("stops") or []],
            "days": item.get("days"), "season": item.get("season") or "",
            "area": item.get("area") or "", "transport": item.get("transport") or "",
            "blurb": item.get("blurb") or "", "tips": list(item.get("tips") or []),
            "regions": item.get("regions") or [], "cross_region": item.get("cross_region"),
            "center": item.get("center"), "stops": item.get("stops") or [],
            "km": item.get("km") or 0,
            # 环境色：球上航线与列表圆点都按它上色（沙漠土黄/海洋深蓝/极地冰蓝）
            "env": item.get("env") or "", "env_color": item.get("env_color") or "",
            "env_label": item.get("env_label") or "",
            # 海上/极地线路要显眼地标出来（它们不是普通陆地环线）
            "mode": item.get("mode") or "",
        })
    record_cards = [record_card(r) for r in records()]
    areas: Dict[str, int] = {}
    for c in city_cards:
        areas[c["area"]] = areas.get(c["area"], 0) + 1
    return {
        "ok": True,
        "cities": city_cards,
        "loops": loop_cards,
        "records": record_cards,
        "stats": {
            "cities": len(city_cards), "loops": len(loop_cards), "records": len(record_cards),
            "areas": areas,
            "with_photo": sum(1 for c in city_cards if c["photo"]),
            "records_with_photo": sum(1 for r in record_cards if r["photo"]),
        },
    }


def _loop_km(item: dict) -> int:
    stops = item.get("stops") or []
    total = 0.0
    for a, b in zip(stops, stops[1:]):
        total += _haversine((a["lon"], a["lat"]), (b["lon"], b["lat"]))
    return int(round(total))


def city_detail(name: str) -> dict:
    item = city(name)
    if not item:
        return {"ok": False, "error": f"没有这座城市：{name}"}
    return {"ok": True, "city": city_card(item), "guide": item.get("guide") or {}}


def record_detail(record_id: str) -> dict:
    item = record(record_id)
    if not item:
        return {"ok": False, "error": f"没有这条世界之最：{record_id}"}
    return {"ok": True, "record": record_card(item)}


def _haversine(a, b) -> float:
    lon1, lat1 = math.radians(a[0]), math.radians(a[1])
    lon2, lat2 = math.radians(b[0]), math.radians(b[1])
    dlon, dlat = lon2 - lon1, lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_R * math.asin(min(1.0, math.sqrt(h)))


def plan_itinerary(names: Sequence[str], closed: bool = False) -> dict:
    """世界行程单：大圆距离 + 预计飞行/转场时间 + 时差变化 + 建议停留天数。

    世界范围内以飞机为主，这里不查航班时刻、不给票价（必须实时查询）。
    """
    picked: List[dict] = []
    unknown: List[str] = []
    for name in names or []:
        item = city(name)
        if not item:
            unknown.append(name)
            continue
        if not any(p["name"] == item["name"] for p in picked):
            picked.append(item)
    if unknown:
        return {"ok": False, "error": "没有收录的城市：" + "、".join(unknown)}
    if len(picked) < 2:
        return {"ok": False, "error": "至少选择 2 座城市"}
    order = list(picked)
    if closed and order[0]["name"] != order[-1]["name"]:
        order.append(order[0])

    legs = []
    total_km = 0.0
    total_fly = 0.0
    prev_tz = None
    for a, b in zip(order, order[1:]):
        km = _haversine((a["lon"], a["lat"]), (b["lon"], b["lat"]))
        hours = km / _FLIGHT_KMH + _FLIGHT_OVERHEAD_H
        tz_a, tz_b = a.get("tz"), b.get("tz")
        tz_shift = None
        if isinstance(tz_a, (int, float)) and isinstance(tz_b, (int, float)):
            tz_shift = round(float(tz_b) - float(tz_a), 1)
        legs.append({
            "frm": a["name"], "to": b["name"],
            "km": int(round(km)),
            "flight_hours": round(hours, 1),
            "flight_text": f"{int(hours)}小时{int(round((hours - int(hours)) * 60)):02d}分",
            "tz_shift": tz_shift,
            "same_country": a.get("country") == b.get("country"),
        })
        total_km += km
        total_fly += hours
        prev_tz = tz_b
    stay_days = sum(float(c.get("days") or 2) for c in order[: len(picked)])
    visit_days = int(round(stay_days))
    travel_days = max(1, int(round(total_fly / 6)))     # 一天最多安排 ~6 小时飞行
    result = {
        "ok": True,
        "kind": "world",
        "order": [c["name"] for c in order],
        "cities": [city_card(c) for c in picked],
        "legs": legs,
        "summary": {
            "city_count": len(picked),
            "total_km": int(round(total_km)),
            "flight_hours": round(total_fly, 1),
            "visit_days": visit_days,
            "travel_days": travel_days,
            "total_days": visit_days + travel_days,
            "areas": sorted({c.get("area") or "" for c in picked if c.get("area")}),
            "tz_span": _tz_span(picked),
            "closed": bool(closed),
        },
        "warnings": [
            "世界行程的机票必须实时查询：这里只给飞行距离与时间估算，不含航班时刻与票价。",
        ],
    }
    tz = _tz_span(picked)
    if tz and abs(tz[1] - tz[0]) >= 6:
        result["warnings"].append(
            f"跨时差较大（{tz[1] - tz[0]:+d} 小时），落地后建议留 1-2 天适应，别把行程排太满。")
    long_legs = [l for l in legs if l["flight_hours"] >= 10]
    if long_legs:
        result["warnings"].append(
            "有 " + str(len(long_legs)) + " 段飞行超过 10 小时（" +
            "、".join(f"{l['frm']}→{l['to']} {l['flight_text']}" for l in long_legs[:3]) +
            "），建议选夜航或中途转机休息。")
    if any(l["km"] < 400 for l in legs):
        result["warnings"].append("有短途航段（<400 公里），可以比较高铁/自驾是否更省时间。")
    return result


def _tz_span(picked: Sequence[dict]):
    values = [float(c["tz"]) for c in picked if isinstance(c.get("tz"), (int, float))]
    if not values:
        return None
    return [int(min(values)), int(max(values))]


def stats() -> dict:
    return map_payload()["stats"]
