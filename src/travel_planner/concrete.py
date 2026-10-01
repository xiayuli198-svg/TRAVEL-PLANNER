"""把「参考路线」落成可执行的出行安排：路线分段 × 本地时刻表 × 高德 API。

路线库解决「怎么走更妙」，这一步解决「今天能不能这么走」：

* **火车段**：到本地时刻表里找这一对城市之间真实存在、时间对得上的车次（车次号、发到点、历时）。
* **自驾段**：走高德驾车路径规划（复用 ``drive.route``，带缓存），拿到真实里程与耗时。
* **其他段（大巴/船/飞机）**：没有公开可查的班次数据，明确标成「需自行查询」，不假装知道。

设计原则：**宁可说「查不到」也不编**。每一段都返回 ``status``：
``ok``（有真实数据）/ ``no_data``（这一天没有可用的车次）/ ``manual``（这类需要自己查）。
前端据此把「已核实」和「待确认」分开显示。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Dict, List, Optional, Sequence

from . import drive, maintenance, service

#: 路线分段里的交通方式 → 我们能不能自动核实
AUTO_MODES = {"train": True, "rail": True, "high_speed": True, "car": True,
              "bus": False, "boat": False, "fly": False, "metro": False, "walk": False}

MODE_HINT = {
    "bus": "大巴班次没有公开可查的数据源，请到当地客运站/购票平台确认",
    "boat": "轮渡/游船班次随季节调整，请以船公司公告为准",
    "fly": "航班请用航司/购票平台查询，本项目不缓存国内航班时刻",
    "metro": "市内地铁/公交请用地图 App 查实时路线",
    "walk": "步行段按地图导航即可",
}


def _clean(name) -> str:
    """城市名归一（与路线库同一套规则：去括号注释、取斜杠前、去「站」后缀）。"""
    from . import routes as routes_mod

    return routes_mod._clean_city(name)


def _minutes_to_hhmm(minutes: Optional[int]) -> str:
    if minutes is None:
        return ""
    minutes = int(minutes) % 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _city_codes(conn: sqlite3.Connection, city: str) -> List[str]:
    """城市 → 主站三字码列表（复用规划用的解析规则：只保留主站）。"""
    try:
        rows = service.resolve_stations(conn, city or "")
    except Exception:                                    # noqa: BLE001
        return []
    codes: List[str] = []
    for row in rows:
        code = row.get("code") if isinstance(row, dict) else None
        if code and code not in codes:
            codes.append(code)
    return codes


def _city_names(conn: sqlite3.Connection, codes: Sequence[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not codes:
        return out
    marks = ",".join("?" * len(codes))
    for row in conn.execute(
            f"SELECT code, name, city FROM stations WHERE code IN ({marks})", list(codes)):
        out[row["code"]] = row["name"] or row["city"] or row["code"]
    return out


def train_options(conn: sqlite3.Connection, date: str, frm: str, to: str,
                  *, limit: int = 4) -> Dict:
    """在某日的时刻表里找 frm→to 的真实车次（按城市解析主站）。"""
    from_codes = _city_codes(conn, frm)
    to_codes = _city_codes(conn, to)
    if not from_codes or not to_codes:
        return {"status": "manual", "options": [],
                "note": f"车站表里没找到「{frm if not from_codes else to}」，请写更常见的城市名"}
    have = conn.execute("SELECT COUNT(DISTINCT train_no) c FROM schedules WHERE date=?",
                        (date,)).fetchone()["c"]
    if not have:
        return {"status": "no_data", "options": [],
                "note": f"{date} 这天没有时刻表数据，先用数据台补一天再来核实"}
    names = _city_names(conn, list(from_codes) + list(to_codes))
    trips = db_trips(conn, date)
    options: List[Dict] = []
    for trip in trips:
        idx = {code: i for i, code in enumerate(trip.stations)}
        for start in from_codes:
            if start not in idx:
                continue
            for end in to_codes:
                if end not in idx or idx[end] <= idx[start]:
                    continue
                dep = trip.dep[idx[start]]
                arr = trip.arr[idx[end]]
                if dep is None or arr is None:
                    continue
                hours = (arr - dep) / 60.0
                if hours <= 0:
                    continue
                options.append({
                    "code": trip.code, "frm": names.get(start, start), "to": names.get(end, end),
                    "dep": _minutes_to_hhmm(dep), "arr": _minutes_to_hhmm(arr),
                    "hours": round(hours, 2), "overnight": arr >= 1440 or dep < 0,
                })
                break
    options.sort(key=lambda o: (o["hours"], o["dep"]))
    # 同一车次可能被多组站匹配到，去重
    seen, unique = set(), []
    for opt in options:
        key = (opt["code"], opt["dep"], opt["arr"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(opt)
    if not unique:
        return {"status": "no_data", "options": [],
                "note": f"{date} 这天没有 {frm}→{to} 的直达车次（可考虑换乘或先补一天的定向爬取）"}
    return {"status": "ok", "options": unique[:limit],
            "note": f"{date} 共找到 {len(unique)} 趟直达车次"}


def db_trips(conn: sqlite3.Connection, date: str):
    """读某日车次；没有就直接返回空列表（调用方已判断过有没有数据）。"""
    from . import db as db_mod

    try:
        return db_mod.load_trips(conn, date)
    except Exception:                                    # noqa: BLE001
        return []


def _city_index(conn: sqlite3.Connection) -> Dict[str, str]:
    """站名/三字码 → 城市：分段里写的可能是站名（南昌西、景德镇北），规划引擎要的是停靠点。"""
    cache = _CITY_INDEX.get(id(conn))
    if cache is not None:
        return cache
    out: Dict[str, str] = {}
    try:
        for row in conn.execute("SELECT code, name, city FROM stations"):
            city = row["city"] or ""
            if not city:
                continue
            for key in (row["code"], row["name"]):
                if key:
                    out[str(key)] = city
            out.setdefault(city, city)
    except Exception:                                     # noqa: BLE001
        out = {}
    if len(_CITY_INDEX) > 8:
        _CITY_INDEX.clear()
    _CITY_INDEX[id(conn)] = out
    return out


#: 连接 → 站名到城市的映射（sqlite3.Connection 不能挂属性，用模块级缓存）
_CITY_INDEX: Dict[int, Dict[str, str]] = {}


def _to_city(conn: sqlite3.Connection, name: str) -> str:
    """尽量把分段里的地名归一到城市；归一不了就原样返回（规划引擎会报「未找到」）。"""
    text = _clean(name)
    if not text:
        return ""
    return _city_index(conn).get(text, text)


def route_stops(conn: sqlite3.Connection, route: Dict) -> List[str]:
    """从路线分段里抽出「要去的城市」序列（规划引擎要的是停靠点）。

    规则：
    * 按分段顺序取 frm→to，相邻重复的合并；
    * **站名归一成城市**（南昌西→南昌），否则规划引擎会当作陌生地名；
    * 环线（`kind=loop`）去掉末尾与起点重复的那一个，交给 closed 表达闭环。
    """
    stops: List[str] = []
    for seg in route.get("segments") or []:
        for key in ("frm", "to"):
            city = _to_city(conn, seg.get(key))
            if city and (not stops or stops[-1] != city):
                stops.append(city)
    if route.get("kind") == "loop" and len(stops) > 2 and stops[0] == stops[-1]:
        stops = stops[:-1]
    return stops


#: 停留档位 → 每晚数（None 表示按素材时长自动推断）
STAY_MODES = {
    "transit": 0,      # 只中转，当天接走
    "one_night": 1,    # 每站住一晚
    "two_nights": 2,   # 每站住两晚（慢慢玩）
}


def stays_for(conn: sqlite3.Connection, route: Dict,
              stay_mode: Optional[str] = None) -> List[Dict]:
    """停留建议。

    * ``stay_mode`` 给了就用它（页面上的「每站停留」下拉）；
    * 否则按素材的 ``total_hours`` 推断：≤12 小时当一日游（不安排过夜），
      12-36 小时给 1 晚，更长给 2 晚，并设上限使总天数不超过「每段 2 晚」。
    * ``note`` 里明确写「住/过夜」的站点额外至少给 1 晚。

    为什么要推断：素材里没有 ``days`` 字段，而「北京→拉萨」和「上海→苏州」
    需要的过夜安排完全不同 —— 猜错不如给个合理默认，再让用户在页面上改。
    """
    stops = route_stops(conn, route)
    if stay_mode in STAY_MODES:
        nights = STAY_MODES[stay_mode]                       # type: ignore[assignment]
    else:
        hours = float(route.get("total_hours") or 0)
        nights = 0 if hours <= 12 else (1 if hours <= 36 else 2)
    out: List[Dict] = []
    for name in stops:
        extra = 0
        for seg in route.get("segments") or []:
            if _to_city(conn, seg.get("to")) != name:
                continue
            text = str(seg.get("note") or "")
            if any(k in text for k in ("住", "过夜", "住一晚")):
                extra = 1
        n = max(nights, extra)
        out.append({"mode": "nights", "nights": n} if n else {"mode": "transit", "nights": 0})
    return out


def _leg_facts(day_legs: Sequence[Dict]) -> Dict:
    """把 do_loop 返回的一天折叠成「几段车 + 车次号 + 票价」。"""
    rides: List[Dict] = []
    for item in day_legs or []:
        for leg in (item.get("journey") or {}).get("legs") or []:
            rides.append({
                "kind": leg.get("kind"), "code": leg.get("code"),
                "frm": leg.get("from"), "to": leg.get("to"),
                "dep": leg.get("dep_text"), "arr": leg.get("arr_text"),
                "fare": leg.get("fare"),
            })
    return {"rides": rides}


def _dedupe(stops: Sequence[str]) -> List[str]:
    out: List[str] = []
    for name in stops:
        if name and (not out or out[-1] != name):
            out.append(name)
    return out


def stop_plan(conn: sqlite3.Connection, route: Dict) -> Dict:
    """把素材里的落脚点分成「有铁路站的城市」与「要地面接驳的景区/小镇」。

    为什么需要这一步：素材里大量条目把**景区、县城、口岸**写成落脚点
    （篁岭、黑马河、亚龙湾、塔什库尔干…），规划引擎只认铁路站，于是整条排不出来。
    分开之后：铁路段照样用真实时刻表核实，地面段照素材的时间/价格列出来并**标明是估算**。
    """
    stops = route_stops(conn, route)
    known = _city_index(conn)
    rail, ground = [], []
    for name in stops:
        (rail if name in known else ground).append(name)
    return {"stops": stops, "rail": _dedupe(rail), "ground": ground}


def _ground_legs(conn: sqlite3.Connection, route: Dict, info: Dict) -> List[Dict]:
    """给每个「没有铁路站」的落脚点配一段地面接驳（取自素材里那一段）。"""
    known = _city_index(conn)
    segs = list(route.get("segments") or [])
    by_to: Dict[str, Dict] = {}
    by_frm: Dict[str, Dict] = {}
    for seg in segs:
        to_city = _to_city(conn, seg.get("to"))
        frm_city = _to_city(conn, seg.get("frm"))
        if to_city and to_city not in by_to:
            by_to[to_city] = seg
        if frm_city and frm_city not in by_frm:
            by_frm[frm_city] = seg
    stops = info["stops"]
    anchor_of: Dict[str, str] = {}
    ordered_rails = info["rail"]
    for index, name in enumerate(stops):
        if name in known:
            continue
        # 往前找最近的铁路锚点；找不到就往后找（开头就是景区的情况），再不行用第一个锚点
        anchor = ""
        for back in range(index - 1, -1, -1):
            if stops[back] in known:
                anchor = stops[back]
                break
        if not anchor:
            for fwd in range(index + 1, len(stops)):
                if stops[fwd] in known:
                    anchor = stops[fwd]
                    break
        if not anchor and ordered_rails:
            anchor = ordered_rails[0]
        seg = by_to.get(name) or by_frm.get(name) or {}
        hours = seg.get("hours")
        anchor_of[name] = anchor
        info.setdefault("_ground", []).append({
            "stop": name, "anchor": anchor,
            "mode": str(seg.get("mode") or "car"),
            "hours": float(hours) if isinstance(hours, (int, float)) else None,
            "price": str(seg.get("price") or ""),
            "note": str(seg.get("note") or ""),
            "status": "estimate",
        })
    return list(info.get("_ground") or [])


def plan_trip(conn: sqlite3.Connection, route: Dict, date: str, *,
              objective: str = "fast", mode: str = "mixed", max_transfers: int = 2,
              buffer_min: int = 30, slack_hours: int = 2,
              max_days: Optional[int] = None,
              stay_mode: Optional[str] = None, ground_ok: bool = True) -> Dict:
    """把参考路线**排成真实行程**：城市序列 → 本地时刻表 → 乘车表。

    与 ``concrete_plan`` 的分工：那个是「逐段核实素材里的走法存不存在」，
    这个直接调用规划引擎（与规划页同一个引擎），按你选的日期与偏好出可执行的乘车表 ——
    所以它能处理素材没写、但时刻表里确实存在的换乘方案。

    ``ground_ok``：素材里没有铁路站的落脚点（景区/小镇/口岸）默认**不放弃整条路线**，
    而是拆成「铁路段（真实时刻表核实）+ 地面接驳段（照素材估算、标明需自查）」。
    只有 1 个铁路锚点的条目属于「以某城为基地的周边游」，不是城际路线，仍然如实拒绝。
    """
    info = stop_plan(conn, route)
    stops = info["stops"]
    ground = _ground_legs(conn, route, info) if ground_ok else []
    if not ground_ok:
        unknown = info["ground"]
        if unknown:
            return {"ok": False, "stops": stops, "unknown_stops": unknown,
                    "error": f"这些地点在车站表里找不到：{'、'.join(unknown)}"
                             "（可能是小镇/景区，没有铁路站；把行程拆成有车站的城市再排）"}
    rail_stops = [s for s in info["rail"] if s]
    if len(rail_stops) < 2:
        if ground and rail_stops:
            return {"ok": False, "stops": stops, "kind": "base", "base_city": rail_stops[0],
                    "error": f"这条路线只有「{rail_stops[0]}」一个有铁路站的落脚点，"
                             "其余（" + "、".join(g["stop"] for g in ground) + "）都得地面接驳："
                             "它是「以一座城为基地的周边游」，排不出城际乘车表"
                             "（可以当停留内容看，或先在地图上把它拆成两座城市）"}
        return {"ok": False,
                "error": "这条路线只涉及一个城市（多半是同城选站类条目），排不出行程",
                "stops": stops}
    stops = rail_stops
    closed = route.get("kind") == "loop" or bool(route.get("loop"))
    if closed and len(stops) > 2 and stops[0] == stops[-1]:
        stops = stops[:-1]
    max_days = max_days or route.get("days") or None
    stays = _stays_for_stops(conn, route, stops, stay_mode)
    data_note = _data_note(conn, date)
    try:
        result = service.do_loop(conn, date, stops,
                                 stays,
                                 free_order=False,      # 参考路线的顺序本身就是它的价值，不重排
                                 mode=mode, objective=objective,
                                 max_transfers=max(0, int(max_transfers)),
                                 buffer_min=max(0, int(buffer_min)),
                                 slack_hours=max(0, int(slack_hours)),
                                 links=True, guide=True,
                                 max_days=max_days)
    except Exception as exc:                              # noqa: BLE001
        return {"ok": False, "error": f"排行程失败：{type(exc).__name__}: {exc}", "stops": stops}
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error") or "规划引擎没有给出方案",
                "stops": stops}
    days = result.get("legs") or []
    _attach_ground(days, ground, stops)
    rides: List[Dict] = []
    for day in days:
        rides.extend(_leg_facts([day])["rides"])
    priced = [r for r in rides if r.get("fare")]
    total_fare = sum(float(r.get("fare") or 0) for r in priced)
    has_journey = any((d.get("journey") or {}).get("legs") for d in days)
    if not has_journey:
        return {"ok": False,
                "error": "时刻表里没有把这些点连起来的车次：可能缺这一天/这些方向的数据，"
                         "先在数据台对这几个城市做一次定向爬取",
                "stops": stops}
    warnings = ([data_note] if data_note else []) + list(result.get("warnings") or [])
    if ground:
        warnings.append(
            f"这条素材里有 {len(ground)} 个落脚点没有铁路站（"
            + "、".join(g["stop"] for g in ground) + "）："
            "引擎只核实了铁路段，地面接驳段的时间和价格照素材估算，出行前请自己查一次大巴/包车/轮渡。")
    return {
        "ok": True,
        "route": {"id": route.get("id"), "name": route.get("name"),
                  "kind": route.get("kind"), "kind_label": route.get("kind_label")},
        "date": date,
        "stops": stops,
        "closed": closed,
        "objective": objective,
        "mode": mode,
        "stay_mode": stay_mode or "auto",
        "stays": stays,
        "days": days,
        "ground_legs": ground,
        "ground_count": len(ground),
        "partial": bool(ground),
        "total_days": result.get("total_days"),
        "summary": {
            "days": len(days),
            "rides": len(rides),
            "ground": len(ground),
            "with_fare": len(priced),
            "total_fare": round(total_fare, 1) if priced else None,
            "warnings": len(warnings),
        },
        "warnings": warnings,
        "data_source": _date_source(conn, date),
        "note": ("行程由本地时刻表算出（有偏差、仅作参考）；出行前请以 12306 为准。"
                 + ("含地面接驳段，那几段按素材估算。" if ground else "")),
    }


def _attach_ground(days: Sequence[Dict], ground: Sequence[Dict], anchors: Sequence[str]) -> None:
    """把地面接驳段挂到「到达该锚点」的那一天（找不到就挂到出发那天/最后一天）。"""
    if not days or not ground:
        return
    for item in ground:
        target = None
        for day in days:
            if _clean(day.get("to")) == item["anchor"] or day.get("city") == item["anchor"]:
                target = day
                break
        if target is None:
            for day in days:
                if _clean(day.get("frm")) == item["anchor"]:
                    target = day
                    break
        if target is None:
            target = days[-1]
        target.setdefault("ground", []).append(item)


def _stays_for_stops(conn: sqlite3.Connection, route: Dict, stops: Sequence[str],
                     stay_mode: Optional[str]) -> List[Dict]:
    """按**铁路锚点**算停留（景区点没有铁路段，不参与过夜推导）。

    规则与 ``stays_for`` 一致（下拉优先 → 素材时长推断 → note 里写「住/过夜」至少 1 晚），
    区别只是算在锚点序列上，长度必须与交给引擎的 stops 对齐。
    """
    if stay_mode in STAY_MODES:
        base = STAY_MODES[stay_mode]                         # type: ignore[assignment]
    else:
        hours = float(route.get("total_hours") or 0)
        base = 0 if hours <= 12 else (1 if hours <= 36 else 2)
    out: List[Dict] = []
    for name in stops:
        extra = 0
        for seg in route.get("segments") or []:
            if _to_city(conn, seg.get("to")) != name:
                continue
            if any(k in str(seg.get("note") or "") for k in ("住", "过夜", "住一晚")):
                extra = 1
        nights = max(base, extra)
        out.append({"mode": "nights", "nights": nights} if nights
                   else {"mode": "transit", "nights": 0})
    return out


def _date_source(conn: sqlite3.Connection, date: str) -> Dict:
    """这一天数据的来源档位（页面要能区分「真爬的」和「拷贝的」）。"""
    row = conn.execute("SELECT COUNT(DISTINCT train_no) n FROM schedules WHERE date=?",
                       (date,)).fetchone()
    return maintenance.classify_date(conn, date, row["n"] if row else 0)


def _data_note(conn: sqlite3.Connection, date: str) -> str:
    """数据不够好时给一句人话解释。

    最容易误导的情况：这一天根本没数据，引擎会**自动从基准日拷一份**来算 ——
    排出来的车次看着挺像样，其实不是这一天的。必须说出来。
    """
    info = _date_source(conn, date)
    if info["source"] == "missing":
        rows = maintenance.date_rows(conn)
        base = maintenance.pick_base(rows)
        if base:
            return (f"{date} 这天还没有时刻表数据：下面的车次是从基准日 {base['date']} "
                    "自动拷贝来的，**不是当天的真实车次**。要准就先在数据台补齐或定向爬一天。")
    elif info["source"] == "copy":
        return (f"{date} 这天是拷贝数据（来自 {info.get('copied_from') or '未知日期'}）："
                "隔日开行与调图差异看不出来，出行前请再核对一次。")
    return ""


def concrete_plan(conn: sqlite3.Connection, route: Dict, date: str, *,
                  today: Optional[str] = None) -> Dict:
    """把一条参考路线落到某一天：每一段给出可核实的车次/车程。"""
    today_d = dt.date.fromisoformat(today) if today else dt.date.today()
    try:
        day = dt.date.fromisoformat(date)
    except (TypeError, ValueError):
        return {"ok": False, "error": "日期必须是 YYYY-MM-DD"}
    rows = maintenance.date_rows(conn)
    info = next((r for r in rows if r["date"] == date), None)
    segments = []
    for seg in route.get("segments") or []:
        mode = str(seg.get("mode") or "").lower()
        item = {
            "frm": seg.get("frm"), "to": seg.get("to"), "mode": mode,
            "plan_price": seg.get("price"), "plan_hours": seg.get("hours"),
            "note": seg.get("note") or "",
        }
        if mode in ("train", "rail", "high_speed"):
            found = train_options(conn, date, seg.get("frm") or "", seg.get("to") or "")
            item.update(found)
            item["verified"] = found["status"] == "ok"
        elif mode == "car":
            # 环线（起终点同城，如「西宁→西宁（环湖）」）不该去问高德「从西宁到西宁」：
            # 那不是两地车程，而是环线总里程，只能按素材给的时长估算。
            if _clean(seg.get("frm")) and _clean(seg.get("frm")) == _clean(seg.get("to")):
                item.update({
                    "status": "manual", "verified": False,
                    "note": f"环线自驾：素材给的约 {seg.get('hours') or '?'} 小时是全程时间，"
                            "具体路线与里程建议到当地用地图规划",
                })
            else:
                result = drive.route(conn, seg.get("frm") or "", seg.get("to") or "")
                if result.get("ok"):
                    item.update({
                        "status": "ok", "verified": True,
                        "km": result.get("km"), "hours": result.get("hours"),
                        "tolls": result.get("tolls"),
                        "approximate": bool(result.get("approximate")),
                        "note": (result.get("note") or "")
                        + ("（含估算路段）" if result.get("approximate") else ""),
                    })
                else:
                    item.update({"status": "no_data", "verified": False,
                                 "note": result.get("error") or "驾车路线查询失败"})
        else:
            hint = MODE_HINT.get(mode, "这段需要自行查询班次")
            if seg.get("note"):
                hint = f"{seg['note']}；{hint}"
            item.update({"status": "manual", "verified": False, "note": hint})
        segments.append(item)

    verified = [s for s in segments if s.get("verified")]
    manual = [s for s in segments if s.get("status") == "manual"]
    missing = [s for s in segments if s.get("status") == "no_data"]
    return {
        "ok": True,
        "route": {"id": route.get("id"), "name": route.get("name"),
                  "from_city": route.get("from_city"), "to_city": route.get("to_city")},
        "date": date,
        "data_source": (info or {}).get("source", "missing"),
        "data_label": (info or {}).get("label", "无数据"),
        "segments": segments,
        "summary": {
            "segments": len(segments),
            "verified": len(verified),
            "manual": len(manual),
            "no_data": len(missing),
        },
        "warnings": _warnings(segments, info, day, today_d),
        "note": "车次来自本地时刻表（有偏差、仅作参考）；里程与车程来自高德；"
                "票价仍以 12306 / 航司 / 客运站为准。",
    }


def _warnings(segments: Sequence[Dict], info: Optional[Dict],
              day: dt.date, today: dt.date) -> List[str]:
    out: List[str] = []
    if info is None:
        out.append("这一天还没有时刻表数据：车次核实不了，先在数据台补齐或定向爬一天。")
    elif info.get("source") == "copy":
        out.append(f"这一天是拷贝数据（来自 {info.get('copied_from') or '未知日期'}）："
                   "隔日开行与调图差异看不出来，出行前请再核对一次。")
    if (day - today).days > maintenance.CRAWL_WINDOW_DAYS:
        out.append(f"距离今天还有 {(day - today).days} 天，超出 12306 约 "
                   f"{maintenance.CRAWL_WINDOW_DAYS} 天的预售窗口，现在查不到真实票。")
    no_train = [s for s in segments
                if s.get("mode") in ("train", "rail", "high_speed") and s.get("status") == "no_data"]
    if no_train:
        pairs = "、".join(f"{s['frm']}→{s['to']}" for s in no_train[:3])
        out.append(f"有 {len(no_train)} 段火车没查到直达车次（{pairs}）："
                   "可以改用换乘规划，或用数据台对这几个城市做一次定向爬取。")
    return out
