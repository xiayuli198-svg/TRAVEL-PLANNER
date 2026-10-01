"""自驾 / 租车模式：车程规划与租车费用估算。

和公交模式的区别只在「每段怎么走」：

- 公交模式走 RAPTOR 查真实车次（``service.do_loop``）；
- 自驾模式走高德**驾车路径规划**（Web服务 key 存在 meta 的 ``amap_key`` 或环境变量 ``AMAP_KEY``），
  拿到真实里程 / 耗时 / 过路费，缓存进 ``drive_route_cache``；
  没有 key 或配额用尽时退回「大圆距离 × 道路系数 + 分地形车速」的估算，并在结果里标注。

租车费用 = 日租（按城市档位与车型区间）× 天数 + 异地还车费 + 保险 + 油费 + 过路费，
只能给**区间估算**：国内租车平台（神州/一嗨/携程/飞猪）都没有免费公开的报价 API，
结果里会给出各平台链接，实时价以平台为准。
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import catalog
from .db import PROJECT_ROOT

from .ingest import amap as amap_client  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"

# 长途自驾的现实约束
MAX_DRIVE_HOURS_PER_DAY = 6.5      # 单日驾驶上限（超过就拆成两天）
BREAK_EVERY_HOURS = 2.0            # 每 2 小时休息一次
BREAK_MINUTES = 15
DEFAULT_DEP = 8 * 60               # 08:00 出发

# 地质/地形对平均车速的影响（没有高德 key 时用来估时）
TERRAIN_SPEED = {
    "青藏": 55.0, "西北": 78.0, "西南": 55.0, "华北": 80.0, "东北": 78.0,
    "江南": 82.0, "华中": 78.0, "华南": 78.0, "齐鲁": 82.0, "港澳台": 65.0,
}
ROAD_FACTOR = 1.28                 # 大圆距离 → 公路里程的粗略系数


# ---------------------------------------------------------------- 静态资料
def _load_presets() -> dict:
    path = DATA_DIR / "drive_presets.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


_PRESETS = _load_presets()
LOOP_DRIVE: Dict[str, dict] = _PRESETS.get("loops") or {}
RENTAL_MODEL: dict = _PRESETS.get("rental_model") or {}
TIER_OVERRIDES: Dict[str, str] = _PRESETS.get("city_tier_overrides") or {}

DEFAULT_RENTAL_MODEL = {
    "tiers": {
        "A": {"经济型": [180, 320], "SUV": [260, 450], "商务": [380, 650]},
        "B": {"经济型": [120, 220], "SUV": [180, 320], "商务": [260, 450]},
        "C": {"经济型": [90, 160], "SUV": [140, 240], "商务": [200, 330]},
        "D": {"经济型": [130, 230], "SUV": [200, 350], "商务": [280, 480]},
    },
    "one_way_per_km": 0.9,
    "insurance_per_day": [30, 80],
    "fuel_price": 7.8,
    "consumption_per_100km": 8.0,
    "platforms": [
        {"name": "神州租车", "url": "https://www.zuche.com/", "note": "网点多、车型标准化"},
        {"name": "一嗨租车", "url": "https://www.ehi.com.cn/", "note": "异地还车政策较友好"},
        {"name": "携程租车", "url": "https://car.ctrip.com/", "note": "聚合比价，可筛选异地还车"},
        {"name": "飞猪租车", "url": "https://www.fliggy.com/car/", "note": "常有券"},
    ],
}

# 租车最贵（A）与车源最少（D）的城市
TIER_A = {
    "北京", "上海", "广州", "深圳", "三亚", "丽江", "拉萨", "呼伦贝尔", "阿拉善盟",
    "阿勒泰", "喀什", "香港", "澳门", "台北", "大理", "西双版纳", "迪庆", "甘孜", "九寨沟",
}
TIER_D = {
    "阿里", "那曲", "玉树州", "果洛州", "昌都", "甘南", "阿坝", "大兴安岭", "海西州",
    "和田", "黄南州", "海南州", "海北州", "山南", "林芝", "日喀则", "锡林郭勒",
    "巴音郭楞", "阿克苏", "甘孜", "阿拉善盟", "兴安盟", "白山", "伊春", "黑河",
}

# 省会 / 直辖市 / 计划单列市 / 热门旅游城市（租车供应充足、价格中等）
TIER_B = {
    "北京", "天津", "上海", "重庆", "石家庄", "太原", "呼和浩特", "沈阳", "长春",
    "哈尔滨", "南京", "杭州", "合肥", "福州", "南昌", "济南", "郑州", "武汉",
    "长沙", "广州", "南宁", "海口", "成都", "贵阳", "昆明", "拉萨", "西安",
    "兰州", "西宁", "银川", "乌鲁木齐", "大连", "青岛", "宁波", "厦门", "深圳",
    "苏州", "无锡", "温州", "佛山", "东莞", "珠海", "三亚", "桂林", "丽江",
    "泉州", "徐州", "烟台", "威海", "洛阳", "开封", "大同", "承德", "秦皇岛",
    "张家界", "黄山", "绍兴", "嘉兴", "湖州", "扬州", "镇江", "常州", "南通",
    "潮州", "汕头", "北海", "柳州", "宜昌", "襄阳", "敦煌", "嘉峪关", "张掖",
    "银川", "西双版纳", "大理", "遵义", "安顺", "宜宾", "乐山", "绵阳",
}


def rental_model() -> dict:
    model = dict(DEFAULT_RENTAL_MODEL)
    model.update({k: v for k, v in RENTAL_MODEL.items() if v})
    if "tiers" in RENTAL_MODEL:
        model["tiers"] = RENTAL_MODEL["tiers"]
    if "platforms" in RENTAL_MODEL:
        model["platforms"] = RENTAL_MODEL["platforms"]
    return model


def city_tier(name: str) -> str:
    if name in TIER_OVERRIDES:
        return TIER_OVERRIDES[name]
    if name in TIER_A:
        return "A"
    if name in TIER_D:
        return "D"
    if name in TIER_B:
        return "B"
    item = catalog.city(name) or {}
    if item.get("region") == "港澳台":
        return "A"
    if item.get("hub"):
        return "B"
    return "C"


def loop_drive(loop_id: str) -> dict:
    return LOOP_DRIVE.get(loop_id) or {}


# ---------------------------------------------------------------- 车程
def _haversine(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lon1, lat1 = math.radians(a[0]), math.radians(a[1])
    lon2, lat2 = math.radians(b[0]), math.radians(b[1])
    dlon, dlat = lon2 - lon1, lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def estimate_route(from_city: str, to_city: str) -> dict:
    """没有高德 key 时的兜底估算：大圆距离 × 道路系数 / 地形车速。"""
    a, b = catalog.city(from_city) or {}, catalog.city(to_city) or {}
    if a.get("lon") is None or b.get("lon") is None:
        return {"ok": False, "error": "缺少坐标，无法估算车程"}
    straight = _haversine((a["lon"], a["lat"]), (b["lon"], b["lat"]))
    km = straight * ROAD_FACTOR
    speed = min(TERRAIN_SPEED.get(a.get("region") or "", 75.0),
                TERRAIN_SPEED.get(b.get("region") or "", 75.0))
    if km > 800:
        speed = min(95.0, speed + 15)      # 长途多半走高速
    minutes = int(km / speed * 60)
    return {
        "ok": True, "source": "估算",
        "distance_km": round(km), "duration_min": minutes,
        "tolls": round(km * 0.45),
        "note": "按大圆距离与地形车速估算（未配置高德 key 或配额用尽）",
    }


def route(conn: sqlite3.Connection, from_city: str, to_city: str,
          refresh: bool = False) -> dict:
    """城市间车程：优先高德驾车路径规划，带 SQLite 缓存。"""
    if not from_city or not to_city:
        return {"ok": False, "error": "缺少城市"}
    if from_city == to_city:
        return {"ok": True, "source": "本地", "distance_km": 0, "duration_min": 0,
                "tolls": 0, "note": ""}
    row = conn.execute(
        "SELECT distance_m,duration_s,tolls,failed FROM drive_route_cache "
        "WHERE from_city=? AND to_city=? AND strategy=0", (from_city, to_city)).fetchone()
    if row and not refresh:
        if row["failed"]:
            return estimate_route(from_city, to_city)
        return {
            "ok": True, "source": "高德驾车",
            "distance_km": round((row["distance_m"] or 0) / 1000),
            "duration_min": round((row["duration_s"] or 0) / 60),
            "tolls": float(row["tolls"] or 0), "note": "",
        }
    key = amap_client.get_key(conn)
    a, b = catalog.city(from_city) or {}, catalog.city(to_city) or {}
    if not key or a.get("lon") is None or b.get("lon") is None:
        return estimate_route(from_city, to_city)
    import urllib.parse
    import urllib.request
    params = urllib.parse.urlencode({
        "origin": f"{a['lon']},{a['lat']}", "destination": f"{b['lon']},{b['lat']}",
        "strategy": 0, "extensions": "base", "key": key,
    })
    try:
        with urllib.request.urlopen(
                f"https://restapi.amap.com/v3/direction/driving?{params}", timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:                                     # noqa: BLE001
        data = {}
    paths = ((data.get("route") or {}).get("paths") or []) if data.get("status") == "1" else []
    now = dt.datetime.now().isoformat(timespec="seconds")
    if not paths:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO drive_route_cache"
                "(from_city,to_city,distance_m,duration_s,tolls,strategy,raw,fetched_at,failed) "
                "VALUES(?,?,0,0,0,0,?,?,1)", (from_city, to_city, json.dumps(data)[:2000], now))
        return estimate_route(from_city, to_city)
    p = paths[0]
    dist_m = int(float(p.get("distance") or 0))
    dur_s = int(float(p.get("duration") or 0))
    tolls = float(p.get("tolls") or 0)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO drive_route_cache"
            "(from_city,to_city,distance_m,duration_s,tolls,strategy,raw,fetched_at,failed) "
            "VALUES(?,?,?,?,?,0,?,?,0)",
            (from_city, to_city, dist_m, dur_s, tolls, "", now))
    return {
        "ok": True, "source": "高德驾车",
        "distance_km": round(dist_m / 1000), "duration_min": round(dur_s / 60),
        "tolls": tolls, "note": "",
    }


def drive_minutes(duration_min: float) -> Dict[str, int]:
    """把纯驾驶时间加上休息时间，返回 {drive, break, total} 分钟。"""
    drive = max(0, int(round(duration_min)))
    breaks = 0
    if drive > BREAK_EVERY_HOURS * 60:
        breaks = int(drive // (BREAK_EVERY_HOURS * 60)) * BREAK_MINUTES
    return {"drive": drive, "break": breaks, "total": drive + breaks}


# ---------------------------------------------------------------- 行程
def _fmt_dur(minutes: int) -> str:
    minutes = max(0, int(minutes))
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}小时{m}分"
    if h:
        return f"{h}小时"
    return f"{m}分"


def _next_departure(arr_day: dt.date, arr_min: int, mode: str, nights: int,
                    default_dep: int) -> Tuple[dt.date, int]:
    """到站后的出发安排（与公交模式同一套语义：纯中转 / 玩半天 / 住 N 晚）。"""
    if mode == "transit":
        d = arr_min + 30
        if d < 1440:
            return arr_day, d
        return arr_day + dt.timedelta(days=1), default_dep
    if mode == "halfday":
        d = arr_min + 300
        if d < 1380:
            return arr_day, d
        return arr_day + dt.timedelta(days=1), default_dep
    return arr_day + dt.timedelta(days=max(1, nights)), default_dep


def plan_drive(conn: sqlite3.Connection, date: str, cities: Sequence[str],
               closed: bool = True, stays: Optional[Sequence] = None,
               time: str = "08:00", rest_days: int = 0,
               with_rental: bool = False, car_class: str = "SUV",
               max_drive_hours: float = MAX_DRIVE_HOURS_PER_DAY,
               refresh: bool = False) -> dict:
    """按选中的城市顺序排一条自驾行程（含分段里程、过路费、油费与租车估算）。"""
    picked: List[dict] = []
    unknown: List[str] = []
    for name in cities or []:
        item = catalog.city(name)
        if not item:
            unknown.append(name)
            continue
        if closed and picked and item["name"] == picked[0]["name"]:
            continue
        if item["name"] in {c["name"] for c in picked}:
            continue
        picked.append(item)
    if unknown:
        return {"ok": False, "error": "未收录的城市：" + "、".join(unknown)}
    if len(picked) < 2:
        return {"ok": False, "error": "至少选择 2 座城市"}

    order = list(picked)
    stops = [c["name"] for c in order]
    if closed:
        stops.append(stops[0])
    count = len(order)
    provided = list(stays or [])
    per_city: List[dict] = []
    for index, item in enumerate(order):
        spec = None
        if index < len(provided) and provided[index]:
            value = provided[index]
            if hasattr(value, "model_dump"):
                value = value.model_dump()
            if isinstance(value, dict):
                spec = {"mode": value.get("mode") or "nights",
                        "nights": int(value.get("nights") or 0)}
        per_city.append(spec or {"mode": "nights", "nights": max(1, int(round(item.get("days") or 1)))})

    start_spec = per_city[0]
    pre_days = int(start_spec.get("nights") or 0) if start_spec.get("mode") == "nights" else 0
    hh, mm = (time or "08:00").split(":")[:2]
    default_dep = int(hh) * 60 + int(mm)
    cur_date = dt.date.fromisoformat(date) + dt.timedelta(days=pre_days)
    dep_min = default_dep

    legs: List[dict] = []
    total_km = 0
    total_tolls = 0.0
    total_drive_min = 0
    estimated = False
    for i in range(len(stops) - 1):
        frm, to = stops[i], stops[i + 1]
        r = route(conn, frm, to, refresh=refresh)
        if not r.get("ok"):
            legs.append({"day_no": (cur_date - dt.date.fromisoformat(date)).days + 1,
                         "date": cur_date.isoformat(), "frm": frm, "to": to,
                         "stay": None, "error": r.get("error") or "无法计算车程"})
            continue
        if r.get("source") == "估算":
            estimated = True
        t = drive_minutes(r["duration_min"])
        arrive_min = dep_min + t["total"]
        arr_day = cur_date + dt.timedelta(days=arrive_min // 1440)
        day_no = (cur_date - dt.date.fromisoformat(date)).days + 1
        stay_spec = per_city[(i + 1) % count] if closed else (
            per_city[i + 1] if i + 1 < count else {"mode": "transit", "nights": 0})
        legs.append({
            "day_no": day_no,
            "date": cur_date.isoformat(),
            "frm": frm, "to": to,
            "stay": _stay_label(stay_spec),
            "drive": {
                "distance_km": r["distance_km"],
                "minutes": t["drive"], "break_minutes": t["break"], "total_minutes": t["total"],
                "hours_text": _fmt_dur(t["drive"]),
                "total_text": _fmt_dur(t["total"]),
                "dep_text": f"{dep_min // 60:02d}:{dep_min % 60:02d}",
                "arr_text": f"{(arrive_min % 1440) // 60:02d}:{arrive_min % 60:02d}",
                "arr_cal": f"{arr_day.isoformat()} {(arrive_min % 1440) // 60:02d}:{arrive_min % 60:02d}",
                "tolls": r.get("tolls") or 0,
                "fuel": round(r["distance_km"] * rental_model()["consumption_per_100km"] / 100
                              * rental_model()["fuel_price"]),
                "source": r.get("source"),
                "note": r.get("note") or "",
            },
        })
        total_km += r["distance_km"]
        total_tolls += float(r.get("tolls") or 0)
        total_drive_min += t["drive"]
        cur_date, dep_min = _next_departure(arr_day, arrive_min % 1440,
                                           stay_spec.get("mode", "nights"),
                                           int(stay_spec.get("nights") or 1),
                                           default_dep + int(rest_days or 0) * 0)

    total_days = 0
    if legs:
        last = legs[-1]
        arr_day = dt.date.fromisoformat(last.get("drive", {}).get("arr_cal", last["date"])[:10]) \
            if last.get("drive") else dt.date.fromisoformat(last["date"])
        total_days = (arr_day - dt.date.fromisoformat(date)).days + 1

    model = rental_model()
    fuel_total = round(total_km * model["consumption_per_100km"] / 100 * model["fuel_price"])
    result = {
        "ok": True,
        "kind": "drive",
        "date": date,
        "closed": bool(closed),
        "order_cities": [c["name"] for c in order],
        "plan_order": stops,
        "legs": legs,
        "total_days": total_days,
        "summary": {
            "city_count": count,
            "total_days": total_days,
            "total_km": round(total_km),
            "drive_hours": round(total_drive_min / 60, 1),
            "tolls": round(total_tolls),
            "fuel": fuel_total,
            "regions": sorted({c.get("region") or "" for c in order if c.get("region")}),
            "estimated": estimated,
            "date": date,
            "depart_date": (dt.date.fromisoformat(date) + dt.timedelta(days=pre_days)).isoformat(),
            "pre_days": pre_days,
            "max_drive_hours": max_drive_hours,
        },
        "cities": [catalog.city(c["name"]) for c in order],
        "warnings": [],
        "start_notes": [],
    }
    if pre_days:
        result["start_notes"].append(
            f"起点「{order[0]['name']}」先住 {pre_days} 晚，出发日顺延为 {result['summary']['depart_date']}")
    long_legs = [l for l in legs if (l.get("drive") or {}).get("minutes", 0) > max_drive_hours * 60]
    if long_legs:
        result["warnings"].append(
            "有 " + str(len(long_legs)) + " 段单日驾驶超过 "
            f"{max_drive_hours:g} 小时（" +
            "、".join(f"{l['frm']}→{l['to']} {(l['drive']['minutes'] / 60):.1f}h" for l in long_legs[:4]) +
            "），建议拆成两天或中途住一晚。")
    if estimated:
        result["warnings"].append(
            "部分路段用的是里程估算（未调用高德驾车接口）。配置高德 key 后重跑即可拿到真实里程/耗时/过路费。")
    if with_rental:
        # 闭环时在起点还车（无异地费）；单程时按最后一站异地还车
        rental_cities = order[:1] if closed else order
        result["rental"] = rental_quote(rental_cities, result["summary"], car_class=car_class)
    return result


def _stay_label(spec: dict) -> str:
    mode = spec.get("mode") or "nights"
    if mode == "transit":
        return "纯中转"
    if mode == "halfday":
        return "玩半天"
    return f"住{max(1, int(spec.get('nights') or 1))}晚"


# ---------------------------------------------------------------- 租车费用
#: 车型别名。前端下拉写的是「商务型」，而模型里的键叫「商务」——
#: 不归一就会出现「选商务型跟 SUV 一个价」这种看着像没实现的 bug（静默回退到 SUV）。
CAR_ALIASES = {
    "suv": "SUV", "越野": "SUV", "越野车": "SUV",
    "商务": "商务", "商务型": "商务", "商务车": "商务", "mpv": "商务", "gl8": "商务",
    "经济": "经济型", "经济型": "经济型", "轿车": "经济型", "紧凑型": "经济型",
}


def normalize_car_class(name: str, available: Optional[Sequence[str]] = None) -> tuple:
    """把用户写的车型归一成模型里的键。

    返回 ``(车型, 说明)``：说明只在「写了一个不存在的车型、被换成别的」时非空，
    页面照实显示，避免又变成「选了没反应」。
    """
    raw = (name or "").strip()
    key = CAR_ALIASES.get(raw) or CAR_ALIASES.get(raw.lower()) or raw
    options = list(available or [])
    if not options or key in options:
        return key or "SUV", ""
    if raw:
        return (options[0], f"没有「{raw}」这一档，按「{options[0]}」估算")
    return options[0], ""


def rental_quote(cities: Sequence[dict], summary: dict, car_class: str = "SUV") -> dict:
    """租车费用区间估算：日租 + 异地还车 + 保险 + 油费 + 过路费。"""
    model = rental_model()
    tiers = model["tiers"]
    pickup = cities[0]["name"] if cities else ""
    dropoff = cities[-1]["name"] if cities else pickup
    tier = city_tier(pickup)
    options = sorted(tiers.get(tier, {}).keys())
    requested_class = car_class
    car_class, class_note = normalize_car_class(car_class, options)
    if car_class not in tiers.get(tier, {}):
        car_class = "SUV" if "SUV" in tiers.get(tier, {}) else next(iter(tiers.get(tier, {"经济型": [100, 200]})))
    low, high = tiers.get(tier, {}).get(car_class, [120, 220])
    # 天数：按行程天数（含游玩日）算租金
    days = max(1, int(summary.get("total_days") or 1))
    one_way_km = 0
    one_way_tier = None
    if dropoff and dropoff != pickup:
        r = None
        one_way_tier = city_tier(dropoff)
        a, b = catalog.city(pickup) or {}, catalog.city(dropoff) or {}
        if a.get("lon") is not None and b.get("lon") is not None:
            one_way_km = round(_haversine((a["lon"], a["lat"]), (b["lon"], b["lat"])) * ROAD_FACTOR)
    one_way_fee = round(one_way_km * model["one_way_per_km"]) if one_way_km else 0
    ins_low, ins_high = model["insurance_per_day"]
    total_low = low * days + one_way_fee + ins_low * days
    total_high = high * days + one_way_fee + ins_high * days
    return {
        "ok": True,
        "pickup": pickup,
        "dropoff": dropoff,
        "one_way": bool(one_way_km),
        "one_way_km": one_way_km,
        "one_way_fee": one_way_fee,
        "tier": tier,
        "tier_label": {"A": "一线/热门（车价最高）", "B": "省会与热门城市",
                       "C": "普通地级市", "D": "偏远地区（车源少、价格高）"}.get(tier, tier),
        "car_class": car_class,
        "car_class_note": class_note,
        "car_class_requested": requested_class,
        "car_options": sorted(tiers.get(tier, {}).keys()),
        "days": days,
        "daily_low": low, "daily_high": high,
        "insurance_per_day": [ins_low, ins_high],
        "rent_only_low": low * days + ins_low * days,
        "rent_only_high": high * days + ins_high * days,
        "fuel": summary.get("fuel") or 0,
        "tolls": summary.get("tolls") or 0,
        "running_low": (summary.get("fuel") or 0) + (summary.get("tolls") or 0),
        "running_high": round(((summary.get("fuel") or 0) + (summary.get("tolls") or 0)) * 1.15),
        "total_low": total_low + (summary.get("fuel") or 0) + (summary.get("tolls") or 0),
        "total_high": total_high + round(((summary.get("fuel") or 0) + (summary.get("tolls") or 0)) * 1.15),
        "km": summary.get("total_km") or 0,
        "fuel_price": model["fuel_price"],
        "consumption": model["consumption_per_100km"],
        "platforms": model["platforms"],
        "disclaimer": ("国内租车平台没有免费的公开报价 API（神州/一嗨/携程/飞猪都需登录或商务合作），"
                       "这里是按城市档位与车型的区间估算；实时价请用下面的平台链接查询。"),
    }


def city_rental_rates(names: Sequence[str], car_class: str = "SUV") -> List[dict]:
    """给一批城市出日租区间（地图上按城市显示租车行情）。"""
    model = rental_model()
    out = []
    for name in names:
        item = catalog.city(name)
        if not item:
            continue
        tier = city_tier(name)
        options = sorted(model["tiers"].get(tier, {}).keys())
        car, note = normalize_car_class(car_class, options)
        low, high = model["tiers"].get(tier, {}).get(car, [120, 220])
        out.append({"name": name, "tier": tier, "daily_low": low, "daily_high": high,
                    "car_class": car, "car_class_note": note,
                    "car_options": options})
    return out
