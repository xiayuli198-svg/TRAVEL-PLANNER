"""定向爬取：只爬「这次要去的地方」相关的 OD，不再每次都扫全国 6006 个枢纽对。

为什么需要它：``pick_hub_stations`` 固定取 78 个枢纽，两两组合就是 6006 个 OD 对，
再逐趟抓整列停站；实际耗时受车次数、网络与服务端限流影响 —— 而其中绝大多数航段跟你这次的行程毫无关系。
时刻表本来就有「隔日/调图有偏差、只作参考」的定位，所以**够用的子图**比**全国全集**更划算：

* 目标城市之间的直达 OD（最要紧）
* 目标城市 ↔ 全国枢纽（覆盖「先到枢纽再转」）
* 目标城市内部各站的互连（同一城市多站中转时会用到）

配合既有的断点续爬（``crawl_state_<日期>.txt``）与 ``ensure_date_data`` 兜底：
定向爬没覆盖到的日期/区间，查询时仍会自动从基准日拷一份，不会出现「查不出结果」。

经验记录：每次成功规划会把用到的城市写进 ``search_log``，``suggest_targets()`` 据此
把「你常去/刚去过的地方」优先排进爬取队列 —— 这就是「主动学习」的第一步：
不训练模型，只把注意力放在你真正会走的方向上。
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: 开发/测试时把 DSH_STRICT_LOG 设为 1，历史记录写失败就直接抛错（别静默吞）
_DEBUG = os.environ.get("DSH_STRICT_LOG") == "1"

#: 单个城市参与组合的主要车站上限（把 30 个站的北京压到 3 个，OD 才不会爆）
MAX_STATIONS_PER_CITY = 3
#: 定向爬取的 OD 对上限；逐对请求受全局节流和风控冷却控制，不估算固定耗时
DEFAULT_MAX_PAIRS = 320
#: 搜索记录保留多久（按此推荐爬取目标）
RECENT_DAYS = 90


def _ensure_search_log(conn: sqlite3.Connection) -> None:
    """确保 search_log 存在且结构正确。

    两个坑都踩过：① 列名不能用裸 ``to``（SQLite 关键字，near "to": syntax error）；
    ② ``CREATE TABLE IF NOT EXISTS`` 不会修旧结构，早期建错的表要补列。
    ``with conn`` 也不能省：连接默认没开 autocommit，DDL 不提交就随连接一起丢。
    """
    with conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS search_log("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, frm TEXT, dest TEXT, travel_date TEXT,"
            "mode TEXT, created_at TEXT)"
        )
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(search_log)")}
        for name in ("frm", "dest", "travel_date", "mode", "created_at"):
            if name not in cols:
                conn.execute(f"ALTER TABLE search_log ADD COLUMN {name} TEXT")


def remember_search(conn: sqlite3.Connection, frm: str, to: str,
                    date: str = "", mode: str = "") -> bool:
    """记一次查询用到的城市。返回是否写入成功（调用方不必理会失败）。"""
    if not (frm or to):
        return False
    try:
        _ensure_search_log(conn)
        with conn:
            conn.execute(
                "INSERT INTO search_log(frm,dest,travel_date,mode,created_at) VALUES(?,?,?,?,?)",
                (frm or "", to or "", date or "", mode or "",
                 dt.datetime.now().isoformat(timespec="seconds")))
        return True
    except sqlite3.Error as exc:            # 记录历史失败不该影响规划本身
        if _DEBUG:
            raise
        return False


def recent_targets(conn: sqlite3.Connection, limit: int = 6) -> List[str]:
    """最近查询过的城市（去重，按最近一次使用排序）。"""
    try:
        _ensure_search_log(conn)
        since = (dt.date.today() - dt.timedelta(days=RECENT_DAYS)).isoformat()
        rows = conn.execute(
            "SELECT city, MAX(created_at) last FROM ("
            "  SELECT frm AS city, created_at FROM search_log WHERE created_at>=? "
            "  UNION ALL SELECT dest AS city, created_at FROM search_log WHERE created_at>=?) "
            "GROUP BY city HAVING city<>'' ORDER BY last DESC LIMIT ?",
            (since, since, limit)).fetchall()
    except sqlite3.Error:
        return []
    out: List[str] = []
    for r in rows:
        city = (r["city"] or "").strip()
        if city and city not in out:
            out.append(city)
    return out


def suggest_targets(conn: sqlite3.Connection, plan_targets: Sequence[str] = (),
                    limit: int = 8) -> Dict:
    """这次该爬哪些城市：计划里的目标 + 最近查过的 + 基准日的数据热点（枢纽）。"""
    from .ingest.crawl import pick_hub_stations

    chosen: List[str] = []
    for name in list(plan_targets) + recent_targets(conn, limit=6):
        name = (name or "").strip()
        if name and name not in chosen:
            chosen.append(name)
    hubs = [r["city"] for r in pick_hub_stations(conn)]
    return {
        "targets": chosen[:limit],
        "from_plan": [c for c in chosen if c in set(plan_targets)],
        "from_history": [c for c in chosen if c not in set(plan_targets)],
        "hubs": hubs,
    }


#: 进程内缓存：sqlite3.Connection 不允许挂自定义属性（AttributeError 被吞掉过），
#: 所以用模块级字典，键是 (连接 id, 维度)，并限制条数避免长跑进程里越积越多。
_CACHE_LIMIT = 32
_BUSIEST_CACHE: Dict[Tuple[int, str], str] = {}
_COUNTS_CACHE: Dict[Tuple[int, str], Dict[str, int]] = {}


def _cache_put(store: dict, key: tuple, value) -> None:
    if len(store) >= _CACHE_LIMIT:
        store.clear()
    store[key] = value


def _busiest_date(conn: sqlite3.Connection) -> str:
    """基准日（车次最多的那天）。结果按连接缓存：这条聚合在 30 万行停站表上要 1 秒左右，
    一次「查缺口」里会被反复调用（真踩过：调用方总共等了 7-12 秒）。"""
    key = (id(conn), "busiest")
    if key in _BUSIEST_CACHE:
        return _BUSIEST_CACHE[key]
    try:
        row = conn.execute(
            "SELECT date FROM schedules GROUP BY date "
            "ORDER BY COUNT(DISTINCT train_no) DESC LIMIT 1").fetchone()
        value = row["date"] if row else ""
    except sqlite3.Error:
        value = ""
    _cache_put(_BUSIEST_CACHE, key, value)
    return value


def _train_counts(conn: sqlite3.Connection, day: str) -> Dict[str, int]:
    """基准日「车站 → 车次数」一次算好（进程内缓存，按连接+日期）。

    原本每个城市做一次 JOIN，5 个城市就是 5 遍几十万行扫描，一次「查缺口」要 9 秒；
    现在整表分组一次，后续全是字典查。
    """
    key = (id(conn), day)
    if key in _COUNTS_CACHE:
        return _COUNTS_CACHE[key]
    out: Dict[str, int] = {}
    if day:
        try:
            for r in conn.execute(
                    "SELECT station_code code, COUNT(DISTINCT train_no) n FROM schedules "
                    "WHERE date=? GROUP BY station_code", (day,)):
                out[r["code"]] = r["n"]
        except sqlite3.Error:
            out = {}
    _cache_put(_COUNTS_CACHE, key, out)
    return out


def stations_of(conn: sqlite3.Connection, city: str,
                limit: int = MAX_STATIONS_PER_CITY,
                date: str = "") -> List[sqlite3.Row]:
    """一个城市参与组合的主要车站。

    排序有讲究：**主站优先**（名字就叫「北京站」/「北京」的那个），其次才是车次多的。
    只按车次数排会把北京站挤掉、留下郊区的车场，旅客根本不会从那里出发。
    """
    day = date or _busiest_date(conn)
    counts = _train_counts(conn, day)
    rows = conn.execute(
        "SELECT code, name, city, ordinal FROM stations WHERE city=?", (city,)).fetchall()
    def rank(row):
        main = 0 if row["name"] in (city, city + "站") else 1
        return (main, -counts.get(row["code"], 0), row["ordinal"] or 0)
    return sorted(rows, key=rank)[:max(1, int(limit))]


def city_of_code(conn: sqlite3.Connection, code: str) -> str:
    row = conn.execute("SELECT city FROM stations WHERE code=?", (code.upper(),)).fetchone()
    return row["city"] if row else ""


def focus_pairs(conn: sqlite3.Connection, cities: Sequence[str], *,
                hubs: Optional[Sequence[str]] = None,
                max_pairs: int = DEFAULT_MAX_PAIRS,
                per_city: int = MAX_STATIONS_PER_CITY) -> Dict:
    """生成定向 OD 列表（有序对）。

    优先级：① 目标城市两两直达 → ② 目标城市与全国枢纽 → ③ 目标城市内部各站互连。
    超出 ``max_pairs`` 的部分会被截掉，并在结果里说明被截了多少。
    """
    from .ingest.crawl import pick_hub_stations

    clean: List[str] = []
    for c in cities:
        c = (c or "").strip()
        if c and c not in clean:
            clean.append(c)
    if not clean:
        return {"pairs": [], "targets": [], "cities_used": [], "truncated": 0,
                "error": "没有可用的目标城市"}

    picked: Dict[str, List[sqlite3.Row]] = {}
    used_cities: List[str] = []
    for city in clean:
        rows = stations_of(conn, city, per_city)
        if rows:
            picked[city] = rows
            used_cities.append(city)
    if not picked:
        return {"pairs": [], "targets": clean, "cities_used": [], "truncated": 0,
                "error": f"车站表里找不到这些城市：{clean}"}

    if hubs is None:
        hubs = [r["code"] for r in pick_hub_stations(conn)]
    hubs = [h for h in hubs]

    pairs: List[Tuple[str, str]] = []
    seen = set()

    def add(a: str, b: str) -> None:
        if a and b and a != b and (a, b) not in seen:
            seen.add((a, b))
            pairs.append((a, b))

    # ① 目标城市之间：两两直达
    cities_in = list(picked)
    for i, a in enumerate(cities_in):
        for b in cities_in[i + 1:]:
            for ra in picked[a]:
                for rb in picked[b]:
                    add(ra["code"], rb["code"])
                    add(rb["code"], ra["code"])

    # ② 目标城市 ↔ 全国枢纽（转车用）
    hub_set = set(hubs)
    for city in cities_in:
        for r in picked[city]:
            if r["code"] not in hub_set:
                for h in hubs:
                    add(r["code"], h)
                    add(h, r["code"])

    # ③ 目标城市内部：同城各站互连（同城中转、站间换乘）
    for city in cities_in:
        rows = picked[city]
        for i, ra in enumerate(rows):
            for rb in rows[i + 1:]:
                add(ra["code"], rb["code"])
                add(rb["code"], ra["code"])

    truncated = max(0, len(pairs) - max_pairs)
    return {
        "pairs": pairs[:max_pairs],
        "targets": clean,
        "cities_used": used_cities,
        "stations": {c: [r["code"] for r in rows] for c, rows in picked.items()},
        "total_before_cap": len(pairs),
        "truncated": truncated,
        "error": "",
    }


def describe_focus(info: Dict) -> str:
    """给人看的一句话说明（CLI 提示 / 接口返回）。"""
    if info.get("error"):
        return info["error"]
    used = "、".join(info.get("cities_used") or [])
    text = (f"定向爬取：{used}（{len(info['pairs'])} 个 OD 对"
            f"，全量是 6006 对）")
    if info.get("truncated"):
        text += f"；另有 {info['truncated']} 对被上限截掉"
    return text


def focused_pairs_for_plan(conn: sqlite3.Connection, targets: Sequence[str], *,
                           max_pairs: int = DEFAULT_MAX_PAIRS) -> Dict:
    """给「出发地 → 目的地（可多站）」规划用：合并目标城市 + 历史目标。"""
    sug = suggest_targets(conn, targets)
    cities = list(targets) + [c for c in sug["from_history"] if c not in set(targets)]
    info = focus_pairs(conn, cities, hubs=sug["hubs"], max_pairs=max_pairs)
    info["suggest"] = sug
    return info
