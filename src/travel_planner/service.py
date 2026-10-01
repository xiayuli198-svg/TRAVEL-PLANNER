"""规划服务层：CLI 与 Web API 共用的业务逻辑，返回 JSON 友好的结构化结果。"""
from __future__ import annotations

import datetime as dt
import os
import urllib.parse
from typing import Dict, List, Optional, Sequence, Tuple

from . import db
from .engine import cheapest as cheap_engine
from .engine import fare as fare_mod
from .engine import loop as loop_engine
from .engine import raptor
from .engine.model import Journey, Leg as ModelLeg
from .ingest import amap, fliggy
from . import tourism

TOUR_RIDES_CAP = 12      # 游览模式"换乘无上限"的工程上限（最多 12 趟车）


class _Pricer:
    """单次请求内共享：本地坐标缓存 + 里程计价（估算）。

    车次：票价 = 停站间里程 × 分席别单价（路线折线里程近似）；绕行系数在费率模块。
    航班：飞猪真实票价（flights.price），缺失时按坐标距离兜底估算。
    """

    def __init__(self, conn, trips, date: str):
        self.conn = conn
        self.trips_by_id: Dict[str, dict] = {t.trip_id: t for t in trips}
        self.date = date
        self.coords: Dict[str, Tuple[float, float]] = {}
        self._missing_coords = set()
        self._price_prefix: Dict[int, List[float]] = {}
        self.meta: Dict[str, Tuple[str, str, bool]] = {}   # code -> (name, city, is_airport)
        for r in conn.execute("SELECT code,name,city FROM stations"):
            self.meta[r["code"]] = (r["name"], r["city"], False)
        for r in conn.execute("SELECT code,name,city FROM airports"):
            self.meta[r["code"]] = (r["name"], r["city"], True)
        self._cached_geo: Dict[Tuple[str, str], Optional[Tuple[float, float]]] = {}
        for r in conn.execute("SELECT name,city,lon,lat,failed FROM geo_cache"):
            self._cached_geo[(r["name"], r["city"])] = (
                None if r["failed"] else (r["lon"], r["lat"]))
        self.flight_prices: Dict[str, Optional[float]] = {}
        for r in conn.execute("SELECT flight_no, price FROM flights WHERE date=?", (date,)):
            self.flight_prices.setdefault(r["flight_no"], r["price"])

    def coords_of(self, code: str) -> Optional[Tuple[float, float]]:
        if code in self.coords:
            return self.coords[code]
        if code in self._missing_coords:
            return None
        meta = self.meta.get(code)
        if not meta:
            return None
        name, city, is_air = meta
        key = name if is_air else name + "站"
        xy = self._cached_geo.get((key, city))
        if xy is None and (key, city) not in self._cached_geo:
            # 规划热路径只读取已有坐标缓存。逐站实时地理编码会把一次查询
            # 放大成数百次网络请求，尤其在游览模式的省钱搜索中非常明显；
            # 可通过 scripts/prewarm_coords.py 预热坐标。
            if os.environ.get("TRAVEL_PLANNER_LIVE_GEOCODE") == "1":
                key_amap = amap.get_key(self.conn)
                if key_amap:
                    xy = amap.geocode(self.conn, key_amap, key, city)
        if xy:
            self.coords[code] = xy
        else:
            self._missing_coords.add(code)
        return xy

    def price_trip_idx(self, trips, ti: int, i: int, j: int) -> Optional[float]:
        return self.price_leg(trips[ti], i, j)

    def price_leg(self, trip, i: int, j: int) -> Optional[float]:
        """车费（元）。优先坐标里程；坐标不全时用「平均站间距 × 站数」兜底估算。"""
        if trip.trip_id in self.flight_prices:
            p = self.flight_prices[trip.trip_id]
            if p is not None:
                return float(p)
            # 兜底估算（机场坐标）
            c1 = self.coords_of(trip.stations[i])
            c2 = self.coords_of(trip.stations[j])
            if c1 and c2:
                km = fare_mod.haversine_km(c1[0], c1[1], c2[0], c2[1])
                return round(fare_mod.air_est_price(km), 0)
            return round(fare_mod.air_est_price(
                fare_mod.avg_spacing_km("D") * (j - i) * 2.0), 0)
        # cheapest 会反复询问同一车次的不同下车点；缓存相邻站段里程前缀。
        key = id(trip)
        prefix = self._price_prefix.get(key)
        if prefix is None:
            prefix = [0.0]
            fallback = fare_mod.avg_spacing_km(trip.code)
            for k in range(len(trip.stations) - 1):
                c1 = self.coords_of(trip.stations[k])
                c2 = self.coords_of(trip.stations[k + 1])
                seg = (fare_mod.haversine_km(c1[0], c1[1], c2[0], c2[1])
                       if c1 and c2 else fallback)
                prefix.append(prefix[-1] + seg)
            self._price_prefix[key] = prefix
        km = prefix[j] - prefix[i]
        return round(fare_mod.rail_segment_price(trip.code, km), 0)


def fmt_min(m: int) -> str:
    day = m // 1440
    rem = m % 1440
    return f"第{day + 1}天 {rem // 60:02d}:{rem % 60:02d}"


def fmt_dur(m: int) -> str:
    return f"{m // 60}h{m % 60:02d}m"


def parse_time(s: str) -> int:
    hh, mm = s.split(":")
    return int(hh) * 60 + int(mm)


def resolve_stations(conn, q: str) -> List[dict]:
    """解析为车站列表：三字码 > 城市名（全城车站）> 站名精确 > 站名模糊。

    城市级结果只保留「站名含城市名」的主站（北京南/北京西 ✓，小中甸/鹤庆等县站 ✗），
    避免把下辖县站当作城市目标；无同名站的城市（如潮州-潮汕）退化为全部车站。
    """
    code_row = conn.execute(
        "SELECT code,name,city FROM stations WHERE code=? LIMIT 1", (q,)).fetchone()
    if code_row:
        return [dict(code_row)]
    city_rows = conn.execute(
        "SELECT code,name,city FROM stations WHERE city=? ORDER BY ordinal LIMIT 15",
        (q,)).fetchall()
    if city_rows:
        main = [r for r in city_rows if q in r["name"]]
        return [dict(r) for r in (main or city_rows)]
    name_rows = conn.execute(
        "SELECT code,name,city FROM stations WHERE name=? ORDER BY ordinal", (q,)).fetchall()
    if name_rows:
        return [dict(r) for r in name_rows]
    return [dict(r) for r in conn.execute(
        "SELECT code,name,city FROM stations WHERE name LIKE ? ORDER BY ordinal LIMIT 12",
        (f"%{q}%",)).fetchall()]


def station_suggestions(conn, q: str, limit: int = 12) -> List[dict]:
    """车站/城市自动补全（返回 code/name/city）。"""
    if not q:
        return []
    rows = conn.execute(
        "SELECT code,name,city FROM stations WHERE name LIKE ? OR city LIKE ? "
        "ORDER BY ordinal LIMIT ?", (f"{q}%", f"{q}%", limit)).fetchall()
    out = [dict(r) for r in rows]
    seen = set()
    uniq = []
    for r in out:
        key = (r["name"], r["city"])
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    airport_rows = conn.execute(
        "SELECT code,name,city FROM airports WHERE active=1 AND (name LIKE ? OR city LIKE ?) "
        "ORDER BY city,name LIMIT ?", (f"{q}%", f"{q}%", limit)).fetchall()
    for r in airport_rows:
        item = {"code": db.air_code(r["code"]), "name": r["name"], "city": r["city"]}
        key = (item["name"], item["city"])
        if key not in seen:
            seen.add(key)
            uniq.append(item)
    return uniq[:limit]


def _is_city_query(rows, q: str) -> bool:
    return (len(rows) > 1 or (rows and rows[0]["city"] == q)
            or (rows and rows[0]["code"].startswith(db.AIR_PREFIX)))


def _universe(conn, date, frm_q, frm_rows, to_q, to_rows, mode, base_date=None):
    """构建规划宇宙 → (rail_trips, flight_trips, airport_codes, start_codes, target_codes, notes)。"""
    notes = []
    frm_codes = [r["code"] for r in frm_rows]
    to_codes = [r["code"] for r in to_rows]

    rail_trips = db.load_trips(conn, date) if mode != "air" else []
    if mode != "air" and not rail_trips and base_date:
        db.ensure_date_data(conn, date, base_date)
        rail_trips = db.load_trips(conn, date)
    flight_trips = []
    airport_codes = set()
    if mode in ("air", "mixed"):
        city_f = _is_city_query(frm_rows, frm_q)
        city_t = _is_city_query(to_rows, to_q)
        if city_f and city_t:
            try:
                n = fliggy.fetch_and_store(conn, frm_rows[0]["city"], to_rows[0]["city"], date)
                if n:
                    # 只把本次 OD 的航班放入候选图。数据库可能同时缓存了
                    # 其它城市对的航班，读取“某日全部航班”会让环线/游览
                    # 把无关航段误当成当前航段。
                    oairs = [db.air_code(r["code"]) for r in conn.execute(
                        "SELECT code FROM airports WHERE active=1 AND city=?", (frm_rows[0]["city"],))]
                    dairs = [db.air_code(r["code"]) for r in conn.execute(
                        "SELECT code FROM airports WHERE active=1 AND city=?", (to_rows[0]["city"],))]
                    flight_trips = db.load_flights(conn, date, oairs, dairs)
                    notes.append(f"航班 {n} 班（飞猪接口，已缓存）")
                else:
                    notes.append("该航线无航班数据")
            except Exception as e:  # noqa: BLE001
                notes.append(f"航班数据获取失败（{str(e)[:80]}），仅铁路规划")
        else:
            notes.append("指定了具体车站，仅铁路规划；空铁联运请用城市名")
        if flight_trips:
            for t in flight_trips:
                for s in t.stations:
                    if s.startswith(db.AIR_PREFIX):
                        airport_codes.add(s)
            if city_f:
                frm_codes += [db.air_code(r["code"]) for r in conn.execute(
                    "SELECT code FROM airports WHERE active=1 AND city=?", (frm_rows[0]["city"],))]
            if city_t:
                to_codes += [db.air_code(r["code"]) for r in conn.execute(
                    "SELECT code FROM airports WHERE active=1 AND city=?", (to_rows[0]["city"],))]
    return rail_trips, flight_trips, airport_codes, frm_codes, to_codes, notes


def _names(conn) -> dict:
    names = {r["code"]: r["name"]
             for r in conn.execute("SELECT code,name FROM stations")}
    names.update({db.air_code(r["code"]): r["name"]
                  for r in conn.execute("SELECT code,name FROM airports")})
    return names


def available_dates(conn) -> List[Dict]:
    """有数据的日期（车次数按降序）。"""
    rows = conn.execute(
        "SELECT date, COUNT(DISTINCT train_no) n FROM schedules "
        "GROUP BY date ORDER BY date").fetchall()
    return [{"date": r["date"], "trains": r["n"]} for r in rows]


def best_data_date(conn) -> Optional[Dict]:
    """车次最多的数据日期（用于前端默认日期）。"""
    rows = available_dates(conn)
    return max(rows, key=lambda x: x["trains"]) if rows else None


def prepare_date(conn, to_date: str, from_date: Optional[str] = None,
                 *, overwrite: bool = False) -> dict:
    """为目标日期准备时刻表；未指定源日期时自动选择车次最多的基线。"""
    for label, value in (("目标日期", to_date), ("源日期", from_date)):
        if value is None:
            continue
        try:
            parsed = dt.date.fromisoformat(value)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"{label}必须是 YYYY-MM-DD"}
        if parsed.isoformat() != value:
            return {"ok": False, "error": f"{label}必须是 YYYY-MM-DD"}
    existing = conn.execute("SELECT COUNT(*) c FROM schedules WHERE date=?",
                            (to_date,)).fetchone()["c"]
    if existing and not overwrite:
        return {"ok": True, "copied": False, "rows": existing, "source": None,
                "note": f"目标日期已有 {existing} 行数据，已保留原数据。"}
    if from_date is None:
        source = best_data_date(conn)
        if not source:
            return {"ok": False, "error": "没有可用的源日期，请先导入或爬取时刻表"}
        from_date = source["date"]
    return copy_date(conn, from_date, to_date, overwrite=overwrite)


def _autofill_date(conn, date: str) -> Optional[str]:
    """date 无数据时，从最近的有数据日期复制（优先车次≥1000 的日期）。返回来源日期或 None。"""
    if conn.execute("SELECT 1 FROM schedules WHERE date=? LIMIT 1", (date,)).fetchone():
        return None
    ds = available_dates(conn)
    if not ds:
        return None
    try:
        target = dt.date.fromisoformat(date)
    except ValueError:
        return None
    full = [d for d in ds if d["trains"] >= 1000]
    pool = full or ds
    pool.sort(key=lambda d: abs((dt.date.fromisoformat(d["date"]) - target).days))
    src = pool[0]["date"]
    db.ensure_date_data(conn, date, src)
    return src


def _dates_hint(conn) -> str:
    ds = available_dates(conn)
    if not ds:
        return "本地没有任何日期数据，请先 import_dataset 或 crawl-date"
    return ("已加载日期: " + "、".join(
        f"{d['date']}({d['trains']} 车次)" for d in ds)
            + "；可用 import_dataset / copy-date / crawl-date 补充其它日期")


def _diagnose_empty(conn, trips, flight_trips, frm_codes, to_codes,
                    footpaths, start_min, max_transfers, mode) -> str:
    """无方案时的原因诊断。"""
    st = {s for t in trips for s in t.stations}
    names = _names(conn)
    miss_frm = [c for c in frm_codes if c not in st]
    miss_to = [c for c in to_codes if c not in st]
    frm_all_miss = bool(miss_frm) and len(miss_frm) == len(frm_codes)
    to_all_miss = bool(miss_to) and len(miss_to) == len(to_codes)

    def label(codes):
        return "/".join(names.get(c, c) for c in codes[:4])

    if frm_all_miss and to_all_miss:
        return (f"出发地「{label(frm_codes)}」与到达地「{label(to_codes)}」"
                f"都不在该日数据覆盖范围内（该日仅覆盖 {len(st)} 个站）。{_dates_hint(conn)}")
    if frm_all_miss:
        return f"出发地「{label(frm_codes)}」不在该日数据覆盖范围内。{_dates_hint(conn)}"
    if to_all_miss:
        return f"到达地「{label(to_codes)}」不在该日数据覆盖范围内。{_dates_hint(conn)}"
    # 两地都有站，但无方案：换乘上限过低，或真不可达
    probe = raptor.plan(trips, footpaths, frm_codes, to_codes, start_min,
                        max_transfers=min(8, max_transfers + 4))
    if probe:
        need = min(len(j.legs) - 1 for j in probe)
        return (f"两地可达，但至少需要 {need} 次换乘（当前上限 {max_transfers} 次），"
                "请调大「最多换乘」")
    return (f"该日数据中「{label(frm_codes)}」到「{label(to_codes)}」不可达"
            "（可能存在中间线路数据缺口）。" + _dates_hint(conn))


def _pareto_filter(journeys):
    seen = {}
    for j in journeys:
        seen.setdefault((j.dep_min, j.arr_min, j.transfers), j)
    uniq = list(seen.values())

    def dominates(a, b) -> bool:
        if (a.dep_min, a.arr_min, a.transfers) == (b.dep_min, b.arr_min, b.transfers):
            return False
        return (a.dep_min >= b.dep_min and a.arr_min <= b.arr_min
                and a.transfers <= b.transfers)

    return [j for j in uniq if not any(dominates(o, j) for o in uniq)]


def _tour_chain_labels(conn, names: Dict[str, str], journey: Journey) -> List[str]:
    """从串城链保留交通节点，合并同城铁路小站但保留机场节点。

    游览链后续会按这些标签重新排期。机场与铁路站虽然同城，却需要
    触发机场↔车站接驳；如果把它们像两个铁路小站一样合并，原链中的
    航班段会在重排时悄悄变成铁路段，结果摘要仍可能显示「含飞机」。
    """
    nodes: List[tuple[str, str]] = []
    for leg in journey.legs:
        if not leg.train_no:
            continue
        for code in (leg.from_station, leg.to_station):
            if not nodes or nodes[-1][0] != code:
                nodes.append((code, names.get(code, code)))

    city_cache: Dict[str, Optional[str]] = {}

    def city_of(code: str) -> Optional[str]:
        if code not in city_cache:
            if code.startswith(db.AIR_PREFIX):
                row = conn.execute("SELECT city FROM airports WHERE code=?",
                                   (code[len(db.AIR_PREFIX):],)).fetchone()
            else:
                row = conn.execute("SELECT city FROM stations WHERE code=?",
                                   (code,)).fetchone()
            city_cache[code] = row["city"] if row else None
        return city_cache[code]

    merged: List[tuple[str, str]] = []
    for code, label in nodes:
        # 只折叠同城铁路站；机场节点必须保留，否则机场↔车站接驳和航班
        # 会在后续 plan_leg 阶段丢失。
        if merged:
            prev_code, prev_label = merged[-1]
            both_rail = (not code.startswith(db.AIR_PREFIX)
                         and not prev_code.startswith(db.AIR_PREFIX))
            if both_rail and city_of(code) is not None \
                    and city_of(code) == city_of(prev_code):
                continue
        merged.append((code, label))
    return [label for _, label in merged]


def _transfer_guide(conn, frm_code: str, to_code: str,
                    context_min: Optional[int] = None) -> Optional[str]:
    """两站/机场间的公交中转指引文案；无 key 或查询失败返回 None。

    context_min：换乘发生的绝对分钟（相对该日 0 点），用于深夜停运提示。
    """
    key = amap.get_key(conn)
    if not key:
        return None
    if frm_code.startswith(db.AIR_PREFIX):
        frm = conn.execute("SELECT name, city FROM airports WHERE code=?",
                           (frm_code[len(db.AIR_PREFIX):],)).fetchone()
        frm_air = True
    else:
        frm = conn.execute("SELECT name, city FROM stations WHERE code=?",
                           (frm_code,)).fetchone()
        frm_air = False
    if to_code.startswith(db.AIR_PREFIX):
        to = conn.execute("SELECT name, city FROM airports WHERE code=?",
                          (to_code[len(db.AIR_PREFIX):],)).fetchone()
        to_air = True
    else:
        to = conn.execute("SELECT name, city FROM stations WHERE code=?",
                          (to_code,)).fetchone()
        to_air = False
    if not frm or not to:
        return None
    g = amap.transit_guide(conn, key, frm["name"], frm["city"],
                           to["name"], to["city"],
                           frm_is_airport=frm_air, to_is_airport=to_air)
    text = amap.fmt_guide(g)
    if text and context_min is not None and (context_min % 1440) >= 1380:
        text += "；已近深夜，公交/地铁可能停运，建议打车"
    return text or None


def _leg_dict(conn, names, leg, date, links: bool, guide: bool, day0: int = 0) -> dict:
    is_flight = bool(leg.train_no) and leg.from_station.startswith(db.AIR_PREFIX)
    shift = day0 * 1440
    d = {
        "kind": "flight" if is_flight else ("transfer" if not leg.train_no else "rail"),
        "code": leg.train_code,
        "from": names.get(leg.from_station, leg.from_station),
        "to": names.get(leg.to_station, leg.to_station),
        "dep_min": leg.dep_min,
        "arr_min": leg.arr_min,
        "dep_text": fmt_min(leg.dep_min + shift),
        "arr_text": fmt_min(leg.arr_min + shift),
        "duration_text": fmt_dur(leg.arr_min - leg.dep_min),
    }
    if is_flight and leg.train_no:
        row = conn.execute(
            "SELECT airline FROM flights WHERE flight_no=? AND date=? LIMIT 1",
            (leg.train_no, date)).fetchone()
        if row and row["airline"]:
            d["airline"] = row["airline"]
    if leg.train_no and links and not is_flight:
        fs = urllib.parse.quote(f"{d['from']},{leg.from_station}")
        ts = urllib.parse.quote(f"{d['to']},{leg.to_station}")
        d["link"] = ("https://kyfw.12306.cn/otn/leftTicket/init"
                     f"?linktypeid=dc&fs={fs}&ts={ts}&date={date}&flag=N,N,Y")
    if not leg.train_no and guide and leg.from_station != leg.to_station:
        g = _transfer_guide(conn, leg.from_station, leg.to_station,
                            context_min=leg.dep_min)
        if g:
            d["guide"] = g
    return d


def _transfer_stats(j):
    """换乘口径拆分：乘几趟 / 站内换乘几次 / 城市转场几次（含同城接驳段）。"""
    rides = 0
    station_transfers = 0
    city_transfers = 0
    prev_vehicle = None
    for leg in j.legs:
        if leg.train_no:
            rides += 1
            if prev_vehicle is not None:
                if prev_vehicle.to_station == leg.from_station:
                    station_transfers += 1
                else:
                    city_transfers += 1
            prev_vehicle = leg
        else:
            city_transfers += 1   # 同城接驳/出发/收尾转场
    return rides, station_transfers, city_transfers


def _journey_dict(conn, names, j, date, links: bool, guide: bool, day0: int = 0) -> dict:
    legs = []
    for k, leg in enumerate(j.legs):
        d = _leg_dict(conn, names, leg, date, links, guide, day0=day0)
        if (k < len(j.legs) - 1 and leg.train_no and j.legs[k + 1].train_no):
            nxt = j.legs[k + 1]
            d["wait"] = fmt_dur(nxt.dep_min - leg.arr_min)
            if guide and nxt.from_station != leg.to_station:
                g = _transfer_guide(conn, leg.to_station, nxt.from_station,
                                    context_min=leg.arr_min)
                if g:
                    d["transfer_guide"] = g
        legs.append(d)
    rides, st_transfers, city_transfers = _transfer_stats(j)
    shift = day0 * 1440
    base = dt.date.fromisoformat(date)
    dep_date = base + dt.timedelta(days=(j.dep_min + shift) // 1440)
    arr_date = base + dt.timedelta(days=(j.arr_min + shift) // 1440)

    def hms(m: int) -> str:
        m %= 1440
        return f"{m // 60:02d}:{m % 60:02d}"

    has_price = j.price is not None
    return {
        "legs": legs,
        "n_legs": rides,
        "rides": rides,
        "station_transfers": st_transfers,
        "city_transfers": city_transfers,
        "transfers": max(rides - 1, 0),
        "has_transfer_leg": any(not l.train_no for l in j.legs),
        "price": j.price if has_price else None,
        "price_text": (f"约¥{int(j.price)}" if has_price else None),
        "dep_min": j.dep_min,
        "arr_min": j.arr_min,
        "dep_text": fmt_min(j.dep_min + shift),
        "arr_text": fmt_min(j.arr_min + shift),
        "dep_cal": f"{dep_date.isoformat()} {hms(j.dep_min + shift)}",
        "arr_cal": f"{arr_date.isoformat()} {hms(j.arr_min + shift)}",
        "duration_text": fmt_dur(j.duration),
    }


def _attach_prices(pricer: _Pricer, trips, journeys) -> None:
    """为最快路径等已算出的行程补票价（引擎最省路径已自带票价则跳过）。"""
    for j in journeys:
        if j.price is not None:
            continue
        total = 0.0
        got = True
        for leg in j.legs:
            if not leg.train_no:
                continue
            if leg.price is None:
                trip = pricer.trips_by_id.get(leg.train_no)
                if trip is None:
                    got = False
                    break
                i = next((k for k, s in enumerate(trip.stations)
                          if s == leg.from_station), None)
                jj = None
                if i is not None:
                    jj = next((k for k in range(i + 1, len(trip.stations))
                               if trip.stations[k] == leg.to_station), None)
                if i is None or jj is None:
                    got = False
                    break
                leg.price = pricer.price_leg(trip, i, jj)
            if leg.price is None:
                got = False
                break
            total += leg.price
        if got:
            j.price = round(total)


def _price_of(pricer: _Pricer, trips):
    def price_of(ti, i, j):
        p = pricer.price_trip_idx(trips, ti, i, j)
        return float(p) if p is not None else 0.0
    return price_of


def _loop_budget_suggestions(stops, stays, it, start_date, max_days,
                             plan_leg, default_dep):
    """全程超预算时：逐个试删一个中间点，返回 (删哪个点 → 新全程天数)。"""
    if not it or not it.total_days or it.total_days <= max_days:
        return []
    out = []
    n = len(stops)
    for j in range(1, n - 1):            # 只考虑可删的中间点
        if stops[j] == stops[0]:         # 不动起点/返回点
            continue
        try:
            cand_stops = stops[:j] + stops[j + 1:]
            cand_stays = stays[:j - 1] + stays[j:] if len(stays) >= j else []
            sub = loop_engine.plan_fixed(cand_stops, start_date, cand_stays,
                                         plan_leg, default_dep)
            if sub and sub.total_days:
                out.append({"remove": stops[j], "days": sub.total_days})
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda x: (x["days"],))
    return out[:4]


def _inflate_buffer(footpaths, buffer_min: int) -> Dict:
    """给所有步行/换乘边加上容差分钟数（缓解换乘紧张）。"""
    if not buffer_min:
        return footpaths
    return {s: [(t, w + buffer_min) for t, w in edges]
            for s, edges in footpaths.items()}


def _resolve_place(conn, q: str) -> List[dict]:
    """解析车站或机场（机场返回 AIR: 前缀码）。"""
    rows = resolve_stations(conn, q)
    if rows:
        return rows
    code_row = conn.execute(
        "SELECT code,name,city FROM airports WHERE active=1 AND code=? LIMIT 1",
        (q.upper(),)).fetchone()
    if code_row:
        return [{"code": db.air_code(code_row["code"]), "name": code_row["name"],
                 "city": code_row["city"]}]
    r = conn.execute("SELECT code,name,city FROM airports WHERE active=1 AND name=? LIMIT 1",
                     (q,)).fetchone()
    if r:
        return [{"code": db.air_code(r["code"]), "name": r["name"], "city": r["city"]}]
    city_rows = conn.execute(
        "SELECT code,name,city FROM airports WHERE active=1 AND city=? ORDER BY name",
        (q,)).fetchall()
    if city_rows:
        return [{"code": db.air_code(r["code"]), "name": r["name"], "city": r["city"]}
                for r in city_rows]
    return []


def _do_tour(conn, from_q, to_q, date, time, mode, links, guide, buffer_min,
             objective, slack_hours, green_hsr_fare, tour_stay, with_flights,
             names, frm_rows, to_rows, frm_codes, to_codes, max_days,
             frm_label, to_label, notes, tour_city_count=None,
             rest_days: int = 0) -> dict:
    """单程游览模式：绿皮串城（换乘不限，每站按 tour_stay 停留），可选飞机参与。

    价格闸门：纯绿皮直达价 ×3；含飞机超限→提示"建议不要选择飞机"但仍列出；
    不含飞机超限→按所选目标提示"建议参与飞机或改绿皮卧铺"。
    """
    start_min = parse_time(time)
    start_date = dt.date.fromisoformat(date)

    universe_mode = "mixed" if with_flights else "rail"
    rail_trips, flight_trips, airport_codes, frm_codes, to_codes, unotes = _universe(
        conn, date, from_q, frm_rows, to_q, to_rows, universe_mode)
    notes += unotes
    trips = rail_trips + flight_trips
    if not trips:
        return {"ok": False, "error": f"本地没有 {date} 的时刻表。{_dates_hint(conn)}"}

    footpaths = db.build_mixed_footpaths(
        conn, {s for t in rail_trips for s in t.stations}, airport_codes)
    footpaths = _inflate_buffer(footpaths, buffer_min)
    pricer = _Pricer(conn, trips, date)

    def allow_leg(trip, i, j):
        if trip.trip_id in pricer.flight_prices:
            return True                                  # 航班段（仅在含飞机时存在）
        if fare_mod.is_conventional(trip.code):
            return True
        p = pricer.price_leg(trip, i, j)
        return p is not None and float(p) <= green_hsr_fare

    # 1) 绿皮直达基线价
    direct = raptor.plan(rail_trips, footpaths, frm_codes, to_codes, start_min,
                         max_transfers=0, allow_leg=allow_leg)
    _attach_prices(pricer, trips, direct)
    baseline = min((j.price for j in direct if j.price is not None), default=None)
    if baseline is None:
        fb = cheap_engine.plan_cheapest(
            rail_trips, footpaths, frm_codes, to_codes, start_min, 4,
            start_min + 6 * 1440, _price_of(pricer, trips), allow_leg=allow_leg)
        if fb:
            baseline = fb[0].price
    if baseline is None:
        return {"ok": False, "error": "该 OD 无普速车次可达，无法计算游览模式（可改用其它方式）"}

    # 2) 串城链发现（换乘不限 → 工程上限 12 趟）
    chain_js = cheap_engine.plan_cheapest(
        trips, footpaths, frm_codes, to_codes, start_min, TOUR_RIDES_CAP - 1,
        start_min + 7 * 1440, _price_of(pricer, trips), allow_leg=allow_leg)
    if not chain_js:
        return {"ok": False, "error": "未找到可达的串城链（数据未覆盖或绿皮不可达）"}
    best = chain_js[0]
    labels = _tour_chain_labels(conn, names, best)
    if len(labels) < 2:
        return {"ok": False, "error": "串城链过短"}
    # 城市数是用户偏好：保留起终点，在候选串城链中均匀抽取中间城市。
    if tour_city_count and tour_city_count >= 2 and len(labels) > tour_city_count:
        mids = len(labels) - 2
        want = tour_city_count - 2
        picks = [] if want == 0 else [round(i * (mids - 1) / max(1, want - 1))
                                      for i in range(want)]
        labels = [labels[0]] + [labels[1 + i] for i in sorted(set(picks))] + [labels[-1]]

    # 3) 按站停留重新排期（每站 = tour_stay）
    stay_spec = {"halfday": {"mode": "halfday", "nights": 0},
                 "transit": {"mode": "transit", "nights": 0},
                 "nights": {"mode": "nights", "nights": 1}}[tour_stay]
    stay_spec = dict(stay_spec, rest_days=max(0, int(rest_days or 0)))
    stays = [stay_spec] * (len(labels) - 1)

    resolved_cache: Dict[str, List[dict]] = {}

    def resolve_label(label):
        if label not in resolved_cache:
            resolved_cache[label] = _resolve_place(conn, label)
        return resolved_cache[label]

    def plan_leg(a_label, b_label, d, dep_min):
        a_rows = resolve_label(a_label)
        b_rows = resolve_label(b_label)
        if not a_rows or not b_rows:
            return None
        a_codes = [r["code"] for r in a_rows]
        b_codes = [r["code"] for r in b_rows]
        if set(a_codes) == set(b_codes):
            return None
        dstr = d.isoformat()
        a_air = all(c.startswith(db.AIR_PREFIX) for c in a_codes)
        b_air = all(c.startswith(db.AIR_PREFIX) for c in b_codes)
        # 纯航班段（仅含飞机时）
        if a_air and b_air:
            if not with_flights:
                return None
            try:
                fliggy.fetch_and_store(conn, a_rows[0]["city"], b_rows[0]["city"], dstr)
            except Exception:  # noqa: BLE001
                pass
            ftrips = db.load_flights(
                conn, dstr,
                [db.air_code(r["code"]) for r in a_rows],
                [db.air_code(r["code"]) for r in b_rows])
            if not ftrips:
                return None
            ffoot = db.build_mixed_footpaths(
                conn, [], {s for t in ftrips for s in t.stations})
            js = raptor.plan(ftrips, ffoot, a_codes, b_codes, dep_min,
                             max_transfers=1)
            js = _pareto_filter(js)
            js.sort(key=lambda jj: (jj.arr_min, jj.transfers, jj.duration))
            if not js:
                return None
            pr2 = _Pricer(conn, ftrips, dstr)
            _attach_prices(pr2, ftrips, js)
            return js[0]
        # 同城机场↔车站：接驳伪段
        if (a_air != b_air) and a_rows[0]["city"] == b_rows[0]["city"]:
            return Journey(
                legs=[ModelLeg(train_no="", train_code="同城接驳",
                               from_station=a_codes[0], from_name="",
                               to_station=b_codes[0], to_name="",
                               dep_min=dep_min, arr_min=dep_min + 90, price=None)],
                dep_min=dep_min, arr_min=dep_min + 90, price=None)
        # 铁路段（绿皮规则）
        if not db.ensure_date_data(conn, dstr, date):
            return None
        rtrips = db.load_trips(conn, dstr)
        if not rtrips:
            return None
        fpaths = db.build_mixed_footpaths(
            conn, {s for t in rtrips for s in t.stations}, set())
        fpaths = _inflate_buffer(fpaths, buffer_min)
        pr = _Pricer(conn, rtrips, dstr)

        def allow(trip, i, j):
            if fare_mod.is_conventional(trip.code):
                return True
            p = pr.price_leg(trip, i, j)
            return p is not None and float(p) <= green_hsr_fare

        js = raptor.plan(rtrips, fpaths, a_codes, b_codes, dep_min,
                         max_transfers=2, allow_leg=allow)
        js = _pareto_filter(js)
        # 游览体验优先：趟数最少的坐法（其次最早到达）
        js.sort(key=lambda jj: (len(jj.legs), jj.arr_min, jj.duration))
        if not js:
            return None
        if objective == "cheap":
            fast = js[0]
            budget = fast.arr_min + max(0, slack_hours) * 60
            cheap = cheap_engine.plan_cheapest(
                rtrips, fpaths, a_codes, b_codes, dep_min, 2, budget,
                _price_of(pr, rtrips), allow_leg=allow)
            if cheap:
                best_cheap = min(cheap, key=lambda jj: (len(jj.legs), jj.price or 0))
                _attach_prices(pr, rtrips, cheap)
                return best_cheap
        _attach_prices(pr, rtrips, js)
        return js[0]

    it = loop_engine.plan_fixed(labels, start_date, stays, plan_leg,
                                default_dep=start_min)

    legs_out = []
    total_price = 0.0
    n_ok = 0
    for i, leg in enumerate(it.legs):
        day0 = (leg.date - start_date).days
        rec = {"day_no": day0 + 1, "date": str(leg.date),
               "frm": leg.frm, "to": leg.to,
               "stay": loop_engine.stay_label(it.stays[i]) if i < len(it.legs) - 1 else None,
               "error": leg.error, "journey": None}
        if leg.journey is not None:
            rec["journey"] = _journey_dict(conn, names, leg.journey,
                                           str(leg.date), links, guide, day0=day0)
            if leg.journey.price is not None:
                total_price += leg.journey.price
            n_ok += 1
        legs_out.append(rec)

    gate = round(baseline * 3, 0)
    warnings: List[str] = []
    if tour_city_count and len(labels) != tour_city_count:
        warnings.append(f"目标城市数 {tour_city_count}，当前可行串城链为 {len(labels)} 个城市")
    if total_price > 0:
        if with_flights and total_price > gate:
            warnings.append(
                f"总价约¥{int(total_price)}，已超过纯绿皮直达(¥{int(baseline)})的3倍"
                f"(¥{int(gate)})，建议不要选择飞机；以下仍按你的选择列出方案")
        elif not with_flights and total_price > gate:
            obj_txt = "最快到达" if objective == "fast" else "限时内最省"
            warnings.append(
                f"按【{obj_txt}】目标：全程约¥{int(total_price)} 已超纯绿皮直达3倍"
                f"(¥{int(gate)})，建议参与飞机或改绿皮卧铺")
    if n_ok < len(it.legs):
        warnings.append(f"{len(it.legs) - n_ok} 段未找到方案（可放宽--green-fare或调整停留）")
    budget = None
    if max_days and it.total_days and it.total_days > max_days:
        budget = {"max_days": max_days, "over_by_days": it.total_days - max_days}

    # `with_flights` 表示允许航班，不代表最终一定选到航班。用实际结果
    # 计算展示标记，避免用户看到「含飞机」却在明细中找不到 ✈。
    actual_with_flights = any(
        leg.get("journey") and any(x.get("kind") == "flight"
                                    for x in leg["journey"].get("legs", ()))
        for leg in legs_out
    )

    tourism_conn = tourism.connect()
    try:
        city_info = []
        for label in labels:
            place_rows = _resolve_place(conn, label)
            city = place_rows[0]["city"] if place_rows else label
            info = tourism.get(tourism_conn, city) or {
                "city": city, "recommendation_score": None, "intro": "",
                "halfday_plan": "", "tags": ""
            }
            info["label"] = label
            city_info.append(info)
    finally:
        tourism_conn.close()
    tourism_by_city = {item.get("label", item["city"]): item for item in city_info}
    for rec in legs_out:
        info = tourism_by_city.get(rec["to"])
        if info and (rec.get("stay") or "").startswith("玩半天"):
            rec["city_tourism"] = info

    return {
        "ok": True, "error": None, "mode": "tour",
        "frm_label": frm_label, "to_label": to_label, "date": date,
        "objective": objective,
        "tour": {
            "chain": labels,
            "city_info": city_info,
            "city_count": len(labels),
            "requested_city_count": tour_city_count,
            "rest_days": max(0, int(rest_days or 0)),
            "baseline": baseline,
            "gate": gate,
            "total_price": round(total_price, 0) if total_price > 0 else None,
            "with_flights": actual_with_flights,
            "flight_requested": with_flights,
            "legs": legs_out,
            "total_days": it.total_days,
            "warnings": warnings,
            "budget": budget,
        },
        "notes": notes,
    }


def do_plan(conn, from_q: str, to_q: str, date: str, time: str = "00:00",
            max_transfers: int = 3, mode: str = "mixed", top: int = 10,
            links: bool = False, guide: bool = True, buffer_min: int = 0,
            objective: str = "fast", slack_hours: int = 2,
            green_hsr_fare: float = fare_mod.GREEN_MAX_HSR_FARE,
            tour_stay: str = "halfday", with_flights: bool = False,
            max_days: Optional[int] = None, tour_city_count: Optional[int] = None,
            rest_days: int = 0) -> dict:
    """单程规划。

    objective: fast=最快到达（默认）；cheap=限时内最省（允许比最快慢 slack_hours 小时）。
    mode: rail / air / mixed / green（绿皮省钱）/ tour（单程游览：绿皮串城+可选飞机）。
    tour_stay: 游览模式每站停留（transit/halfday/nights）；with_flights: 是否允许飞机参与。
    buffer_min：换乘容差分钟，会在各站最小换乘时间基础上额外增加。
    """
    # 普通铁路规划继续优先解析车站；机场名也允许作为 air/mixed/tour 的端点。
    resolver = _resolve_place if mode in ("air", "mixed", "tour") else resolve_stations
    frm_rows = resolver(conn, from_q)
    to_rows = resolver(conn, to_q)
    if not frm_rows or not to_rows:
        miss = []
        if not frm_rows:
            miss.append(from_q)
        if not to_rows:
            miss.append(to_q)
        return {"ok": False, "error": f"未找到地点: {', '.join(miss)}"}

    frm_codes = [r["code"] for r in frm_rows]
    to_codes = [r["code"] for r in to_rows]
    if set(frm_codes) & set(to_codes):
        return {"ok": False, "error": "出发地与到达地包含相同车站"}
    frm_label = (frm_rows[0]["name"] if len(frm_rows) == 1
                 else f"{from_q}({len(frm_rows)} 站)")
    to_label = (to_rows[0]["name"] if len(to_rows) == 1
                else f"{to_q}({len(to_rows)} 站)")

    # 缺数据的日期自动从最近有数据日期复制（解锁任意日期查询，注意调图差异）
    notes: List[str] = []
    if mode != "air":
        src = _autofill_date(conn, date)
        if src:
            notes.append(f"该日期无本地数据，已自动从 {src} 复制时刻表（注意调图/隔日差异）")

    if mode == "tour":
        return _do_tour(conn, from_q, to_q, date, time, mode, links, guide,
                        buffer_min, objective, slack_hours, green_hsr_fare,
                        tour_stay, with_flights, _names(conn),
                        frm_rows, to_rows, frm_codes, to_codes,
                        max_days, frm_label, to_label, notes,
                        tour_city_count=tour_city_count, rest_days=rest_days)

    rail_trips, flight_trips, airport_codes, frm_codes, to_codes, unotes = _universe(
        conn, date, from_q, frm_rows, to_q, to_rows, mode)
    notes += unotes
    trips = rail_trips + flight_trips
    if not trips:
        if mode == "air":
            return {"ok": False, "error": f"没有 {date} 的航班数据（该航线无航班或接口失败）"}
        return {"ok": False, "error": f"本地没有 {date} 的时刻表。{_dates_hint(conn)}"}

    names = _names(conn)
    footpaths = db.build_mixed_footpaths(
        conn, {s for t in rail_trips for s in t.stations}, airport_codes)
    footpaths = _inflate_buffer(footpaths, buffer_min)
    start_min = parse_time(time)

    pricer = _Pricer(conn, trips, date)
    allow_leg = None
    if mode == "green":
        def allow_leg(trip, i, j):
            if fare_mod.is_conventional(trip.code):
                return True
            p = pricer.price_leg(trip, i, j)
            return p is not None and float(p) <= green_hsr_fare
        notes.append(f"绿皮省钱：仅普速车次 + 段价≤{green_hsr_fare}元的高铁/动车衔接")

    fast_journeys = raptor.plan(trips, footpaths, frm_codes, to_codes,
                                start_min, max_transfers=max_transfers,
                                allow_leg=allow_leg)
    fast_journeys = _pareto_filter(fast_journeys)
    fast_journeys.sort(key=lambda j: (j.arr_min, j.transfers, j.duration))

    journeys = list(fast_journeys)
    objective_note = "最快到达"
    if objective == "cheap":
        if fast_journeys:
            budget = fast_journeys[0].arr_min + max(0, slack_hours) * 60
            cheap = cheap_engine.plan_cheapest(
                trips, footpaths, frm_codes, to_codes, start_min,
                max_transfers, budget, _price_of(pricer, trips),
                allow_leg=allow_leg)
            if cheap:
                journeys = cheap
                objective_note = (f"限时内最省（比最快方案 {fmt_dur(fast_journeys[0].duration)} 晚不超过 "
                                  f"{slack_hours} 小时）")
    _attach_prices(pricer, trips, journeys)

    diagnosis = None
    if not journeys:
        diagnosis = _diagnose_empty(conn, trips, flight_trips, frm_codes, to_codes,
                                    footpaths, start_min, max_transfers, mode)

    return {
        "ok": True,
        "error": None,
        "frm_label": frm_label,
        "to_label": to_label,
        "date": date,
        "time": time,
        "max_transfers": max_transfers,
        "mode": mode,
        "objective": objective,
        "objective_note": objective_note,
        "notes": notes,
        "journeys": [_journey_dict(conn, names, j, date, links, guide)
                     for j in journeys[:top]],
        "total": len(journeys),
        "diagnosis": diagnosis,
    }


def do_loop(conn, date: str, stops: Sequence[str], stays: Optional[Sequence[int]],
            free_order: bool = False, time: str = "08:00", max_transfers: int = 2,
            mode: str = "mixed", links: bool = False, guide: bool = True,
            buffer_min: int = 0, objective: str = "fast", slack_hours: int = 2,
            max_days: Optional[int] = None,
            green_hsr_fare: float = fare_mod.GREEN_MAX_HSR_FARE,
            tour_city_count: Optional[int] = None, rest_days: int = 0) -> dict:
    """大环线规划。返回 {"ok", "error", "order", "legs", "total_days", "warnings", ...}。

    stays：每段到达点的停留方式（int：0=纯中转、N=住N晚；dict：{mode, nights}）。
    buffer_min：换乘容差分钟。objective: fast / cheap（每段限时内最省，slack_hours 小时）。
    mode: rail / air / mixed / green（绿皮省钱，每段同样适用）。
    max_days：全程天数预算；超限时给出"删掉哪些点可压缩到 N 天"的建议（游览模式用）。
    """
    start_date = dt.date.fromisoformat(date)
    stops = list(stops)
    if len(stops) < 2:
        return {"ok": False, "error": "至少需要 2 个途经点（首尾可相同形成环线）"}
    if tour_city_count and tour_city_count >= 2 and len(stops) > tour_city_count:
        mids = len(stops) - 2
        want = tour_city_count - 2
        picks = [] if want == 0 else [round(i * (mids - 1) / max(1, want - 1))
                                      for i in range(want)]
        stops = [stops[0]] + [stops[1 + i] for i in sorted(set(picks))] + [stops[-1]]

    resolved = {}
    for s in stops:
        rows = resolve_stations(conn, s)
        if not rows:
            return {"ok": False, "error": f"未找到途经点「{s}」"}
        resolved[s] = rows

    if stays is None:
        stays = [{"mode": "nights", "nights": 1}] * (len(stops) - 1)
    elif len(stays) != len(stops) - 1 and len(stays) >= len(stops) - 1:
        # 城市数偏好压缩站点后，兼容前端仍提交原始停留数组。
        stays = list(stays)[:len(stops) - 1]
    # pydantic 模型 / dict / int 统一归一化
    stays = [s.model_dump() if hasattr(s, "model_dump") else s for s in stays]
    if rest_days:
        stays = [dict(s, rest_days=max(0, int(rest_days or 0))) if isinstance(s, dict)
                 else {"mode": "nights", "nights": int(s), "rest_days": max(0, int(rest_days or 0))}
                 for s in stays]
    stays = list(stays)
    if len(stays) != len(stops) - 1:
        return {"ok": False, "error": f"停留方式应为 {len(stops) - 1} 个"
                "（每个到达点一个：transit=纯中转 / halfday=玩半天 / nights=住N晚）"}

    base_date = date
    if mode != "air" and not conn.execute(
            "SELECT 1 FROM schedules WHERE date=? LIMIT 1", (base_date,)).fetchone():
        if not _autofill_date(conn, base_date):
            return {"ok": False, "error": f"{date} 无时刻表数据。{_dates_hint(conn)}"}

    names = _names(conn)
    default_dep = parse_time(time)

    def plan_leg(frm_label, to_label, d, dep_min):
        fr, to = resolved[frm_label], resolved[to_label]
        dstr = d.isoformat()
        rail_trips, flight_trips, airport_codes, frm_codes, to_codes, _ = _universe(
            conn, dstr, frm_label, fr, to_label, to, mode, base_date=base_date)
        if set(frm_codes) == set(to_codes):
            return None
        trips = rail_trips + flight_trips
        if not trips:
            return None
        footpaths = db.build_mixed_footpaths(
            conn, {s for t in rail_trips for s in t.stations}, airport_codes)
        footpaths = _inflate_buffer(footpaths, buffer_min)
        pricer = _Pricer(conn, trips, dstr)
        allow_leg = None
        if mode == "green":
            def allow_leg(trip, i, j):
                if fare_mod.is_conventional(trip.code):
                    return True
                p = pricer.price_leg(trip, i, j)
                return p is not None and float(p) <= green_hsr_fare
        js = raptor.plan(trips, footpaths, frm_codes, to_codes, dep_min,
                         max_transfers=max_transfers, allow_leg=allow_leg)
        js = _pareto_filter(js)
        js.sort(key=lambda jj: (jj.arr_min, jj.transfers, jj.duration))
        if not js:
            return None
        if objective == "cheap":
            fast = js[0]
            budget = fast.arr_min + max(0, slack_hours) * 60
            cheap = cheap_engine.plan_cheapest(
                trips, footpaths, frm_codes, to_codes, dep_min,
                max_transfers, budget, _price_of(pricer, trips),
                allow_leg=allow_leg)
            if cheap:
                _attach_prices(pricer, trips, cheap)
                return cheap[0]   # 费用升序，首条即最省
        _attach_prices(pricer, trips, js)
        return js[0]

    warnings: List[str] = []
    free_summary: Optional[dict] = None
    if free_order:
        middles = stops[1:-1]
        if not middles:
            return {"ok": False, "error": "自由顺序需要至少一个中间点"}
        try:
            best_labels, best_cost, cache = loop_engine.solve_free_order(
                stops[0], stops[-1], middles, start_date, default_dep, plan_leg)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if best_cost >= float("inf"):
            return {"ok": False, "error": "无任何可行顺序（数据未覆盖这些站点间）"}
        input_cost = 0.0
        ok_input = True
        for i in range(len(stops) - 1):
            c = cache.get((stops[i], stops[i + 1]))
            if c is None or c >= float("inf"):
                ok_input = False
                break
            input_cost += c
        free_summary = {
            "order": best_labels,
            "input_order": stops,
            "saved_delta_min": None,
            "note": "停留方式按最终顺序的位置应用",
        }
        if ok_input:
            free_summary["saved_delta_min"] = int(best_cost - input_cost)
        it = loop_engine.plan_fixed(best_labels, start_date, stays, plan_leg, default_dep)
        it.order = best_labels
        plan_stops = best_labels
    else:
        it = loop_engine.plan_fixed(stops, start_date, stays, plan_leg, default_dep)
        plan_stops = list(stops)

    names = _names(conn)  # 航班可能刚入库
    tourism_conn = tourism.connect()
    legs_out = []
    n_ok = 0
    for i, leg in enumerate(it.legs):
        day_no = (leg.date - start_date).days + 1
        rec = {
            "day_no": day_no,
            "date": str(leg.date),
            "frm": leg.frm,
            "to": leg.to,
            "stay": loop_engine.stay_label(it.stays[i]) if i < len(it.legs) - 1 else None,
            "error": leg.error,
            "journey": None,
        }
        stay_label_text = loop_engine.stay_label(it.stays[i]) if i < len(it.legs) - 1 else ""
        if stay_label_text.startswith("玩半天"):
            place_rows = resolve_stations(conn, leg.to)
            city = place_rows[0]["city"] if place_rows else leg.to
            rec["city_tourism"] = tourism.get(tourism_conn, city)
        if leg.journey is not None:
            day0 = (leg.date - start_date).days
            rec["journey"] = _journey_dict(conn, names, leg.journey,
                                           str(leg.date), links, guide, day0=day0)
            n_ok += 1
        legs_out.append(rec)
    if n_ok < len(it.legs):
        warnings.append(f"{len(it.legs) - n_ok} 段未找到方案（该日数据未覆盖这些 OD，或换乘次数上限过低）")
        if n_ok == 0:
            warnings.append(_dates_hint(conn))

    result = {
        "ok": True,
        "error": None,
        "date": date,
        "mode": mode,
        "order": it.order,
        "legs": legs_out,
        "total_days": it.total_days,
        "city_count": len(plan_stops),
        "requested_city_count": tour_city_count,
        "rest_days": max(0, int(rest_days or 0)),
        "warnings": warnings,
    }
    if max_days and it.total_days and it.total_days > max_days:
        result["budget"] = {
            "max_days": max_days,
            "over_by_days": it.total_days - max_days,
            "suggestions": _loop_budget_suggestions(
                plan_stops, list(stays), it, start_date, max_days, plan_leg, default_dep),
        }
    if free_summary:
        result["free_summary"] = free_summary
    tourism_conn.close()
    return result


def data_status(conn) -> dict:
    """各日期数据量与来源；以及车站/机场/航班缓存规模。"""
    dates = []
    for r in conn.execute(
            "SELECT date, COUNT(DISTINCT train_no) n FROM schedules "
            "GROUP BY date ORDER BY date"):
        src = conn.execute("SELECT value FROM meta WHERE key=?",
                           (f"crawl:{r['date']}:stats",)).fetchone()
        imp = conn.execute("SELECT value FROM meta WHERE key=?",
                           (f"import:{r['date']}",)).fetchone()
        dates.append({
            "date": r["date"],
            "trains": r["n"],
            "source": (f"爬取: {src['value'][:60]}" if src else
                       (f"导入: {imp['value'][:60]}" if imp else "未知")),
        })
    airport_count = conn.execute("SELECT COUNT(*) c FROM airports").fetchone()["c"]
    airport_master_count = conn.execute(
        "SELECT COUNT(*) c FROM airports WHERE source='master'").fetchone()["c"]
    airport_cities = conn.execute(
        "SELECT COUNT(DISTINCT city) c FROM airports WHERE city<>''").fetchone()["c"]
    flight_airports = conn.execute(
        "SELECT COUNT(DISTINCT code) c FROM ("
        "SELECT dep_airport code FROM flights UNION SELECT arr_airport FROM flights)").fetchone()["c"]
    missing_flight_airports = conn.execute(
        "SELECT COUNT(*) c FROM (SELECT DISTINCT code FROM ("
        "SELECT dep_airport code FROM flights UNION SELECT arr_airport FROM flights) f "
        "WHERE NOT EXISTS (SELECT 1 FROM airports a WHERE a.code=f.code))").fetchone()["c"]
    tourism_conn = tourism.connect()
    try:
        tourism_cities = tourism_conn.execute("SELECT COUNT(*) c FROM city_tourism").fetchone()["c"]
    finally:
        tourism_conn.close()
    return {
        "dates": dates,
        "stations": conn.execute("SELECT COUNT(*) c FROM stations").fetchone()["c"],
        "airports": airport_count,
        "airport_cities": airport_cities,
        "airport_master": airport_master_count,
        # 航班时刻仍按日期/航线动态缓存；两个数字不一致时说明仍有
        # 航班记录引用未入机场表的代码。
        "flight_airports": flight_airports,
        "flight_airports_missing": missing_flight_airports,
        "tourism_cities": tourism_cities,
        "flight_routes_cached": conn.execute(
            "SELECT COUNT(*) c FROM flight_route_cache").fetchone()["c"],
        "transit_cached": conn.execute(
            "SELECT COUNT(*) c FROM transit_cache").fetchone()["c"],
        "amap_key_set": bool(amap.get_key(conn)),
    }


def copy_dates(conn, from_date: str, to_dates: List[str], *,
               overwrite: bool = False, dry_run: bool = False, via: str = "维护") -> dict:
    """把基准日复制到多个日期，默认跳过已有日期。

    这是数据维护入口的统一实现：CLI/脚本可以一次准备一周，Web 仍可调用
    单日期兼容包装。``dry_run`` 只计算结果，不写入数据库。

    复制会留下来源指纹 ``copy:<日期>:from``。这一点很重要：复制出来的数据和真爬的
    在表里长得一样，只有 meta 能区分，页面靠它把「拷贝」标灰、避免被当成真实时刻表。
    """
    try:
        src_obj = dt.date.fromisoformat(from_date)
        target_objs = [dt.date.fromisoformat(x) for x in to_dates]
    except (TypeError, ValueError):
        return {"ok": False, "error": "日期必须是 YYYY-MM-DD", "results": []}
    if src_obj.isoformat() != from_date or any(
        d.isoformat() != raw for d, raw in zip(target_objs, to_dates)
    ):
        return {"ok": False, "error": "日期必须是 YYYY-MM-DD", "results": []}
    src = src_obj.isoformat()
    targets = [d.isoformat() for d in target_objs]
    if not targets:
        return {"ok": False, "error": "至少指定一个目标日期", "results": []}
    targets = list(dict.fromkeys(targets))
    n = conn.execute("SELECT COUNT(*) c FROM schedules WHERE date=?", (src,)).fetchone()["c"]
    if n == 0:
        return {"ok": False, "error": f"源日期 {src} 无数据", "results": []}
    results = []
    for target in targets:
        if target == src:
            results.append({"date": target, "status": "skipped", "rows": n,
                            "reason": "目标日期与源日期相同"})
            continue
        existing = conn.execute("SELECT COUNT(*) c FROM schedules WHERE date=?",
                                (target,)).fetchone()["c"]
        if existing and not overwrite:
            results.append({"date": target, "status": "skipped", "rows": existing,
                            "reason": "目标日期已有数据"})
            continue
        if dry_run:
            results.append({"date": target, "status": "would_copy", "rows": n,
                            "existing": existing})
            continue
        with conn:
            if overwrite:
                conn.execute("DELETE FROM schedules WHERE date=?", (target,))
            conn.execute(
                "INSERT INTO schedules(train_no,train_code,date,seq,station_code,"
                "station_name,day,arr,dep) "
                "SELECT train_no,train_code,?,seq,station_code,station_name,day,arr,dep "
                "FROM schedules WHERE date=?", (target, src))
            # 来源指纹：页面据此把这一天标成「拷贝」，不再冒充真爬
            db.set_meta(conn, f"copy:{target}:from", src)
            db.set_meta(conn, f"crawl:{target}:stats",
                        f"copied from {src} ({n} rows, via {via})")
        results.append({"date": target, "status": "copied", "rows": n,
                        "replaced": existing if overwrite else 0})
    return {"ok": True, "source": src, "source_rows": n,
            "copied": sum(r["rows"] for r in results if r["status"] == "copied"),
            "results": results,
            "note": "复制仅复用车次停站时刻；隔日/周末开行和临时调图差异无法体现，重要出行请重新爬取。"}


def copy_date(conn, from_date: str, to_date: str, overwrite: bool = False) -> dict:
    """将一日的铁路时刻表复制到另一日。

    复制是个人工具中常用的快速准备数据动作，但目标日期已有数据时静默覆盖
    很容易误删刚刚爬取的结果。因此新调用默认拒绝覆盖；CLI 旧调用仍可显式
    传 ``overwrite=True`` 保持原先的覆盖行为。日期格式在这里统一校验，避免
    把拼写错误当成“源日期无数据”。
    """
    for label, value in (("源日期", from_date), ("目标日期", to_date)):
        try:
            parsed = dt.date.fromisoformat(value)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"{label}必须是 YYYY-MM-DD"}
        if parsed.isoformat() != value:
            return {"ok": False, "error": f"{label}必须是 YYYY-MM-DD"}

    if from_date == to_date:
        return {"ok": False, "error": "源日期和目标日期不能相同"}

    n = conn.execute("SELECT COUNT(*) c FROM schedules WHERE date=?",
                     (from_date,)).fetchone()["c"]
    if n == 0:
        return {"ok": False, "error": f"源日期 {from_date} 无数据"}

    target_row = conn.execute(
        "SELECT COUNT(*) rows, COUNT(DISTINCT train_no) trains "
        "FROM schedules WHERE date=?", (to_date,)
    ).fetchone()
    target_rows = target_row["rows"]
    target_trains = target_row["trains"]
    if target_rows and not overwrite:
        return {
            "ok": False,
            "error": f"目标日期 {to_date} 已有 {target_trains} 趟车次，未覆盖",
            "target_exists": True,
            "target_rows": target_rows,
            "target_trains": target_trains,
            "source_date": from_date,
            "to_date": to_date,
        }

    with conn:
        if overwrite:
            conn.execute("DELETE FROM schedules WHERE date=?", (to_date,))
        conn.execute(
            "INSERT INTO schedules(train_no,train_code,date,seq,station_code,"
            "station_name,day,arr,dep) "
            "SELECT train_no,train_code,?,seq,station_code,station_name,day,arr,dep "
            "FROM schedules WHERE date=?",
            (to_date, from_date))
    db.set_meta(conn, f"crawl:{to_date}:stats",
                f"copied from {from_date} ({n} rows, via web)")
    return {
        "ok": True,
        "rows": n,
        "trains": conn.execute(
            "SELECT COUNT(DISTINCT train_no) c FROM schedules WHERE date=?",
            (to_date,),
        ).fetchone()["c"],
        "source_date": from_date,
        "to_date": to_date,
        "overwritten": bool(target_rows),
        "note": "注意：隔日/周末开行、临时调图的差异无法体现，重要出行请重新爬取。",
    }
