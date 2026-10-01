"""数据台：把「时刻表够不够、是哪来的、要不要补」变成可计算的计划 + 一键执行。

设计前提（与使用者的约定一致）：
* **检查是自动的**：``overview()`` / ``plan()`` 只读数据库，随时可以跑，不改任何数据。
* **写库要人点**：``apply_plan()`` / ``delete_date()`` 才是真正写库的动作，且都支持 dry-run。
* **真爬要人点**：本模块不自动爬 12306；爬取走 ``jobs`` 里的后台任务，由页面按钮启动。

数据来源分档（``classify_date``），因为在同一张表里它们长得一模一样，只有 meta 能区分：

============  ==========================================================
import        导入的公开数据集（HerbertHe 全国车次详情），最接近真实时刻表
crawl         真的爬过 12306
copy          从别的日期复制来的 —— **隔日/周末开行和临时调图体现不出来**
sparse        车次明显偏少（多是调试留下的局部线路）
missing       完全没有数据
============  ==========================================================

日期表的每个日期只属于一档；页面据此上色，避免把拷贝当真实时刻表用。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Dict, Iterable, List, Optional, Sequence

#: 低于这个车次数就认为「不像全国数据」
SPARSE_TRAINS = 1000
#: 12306 一般只放未来 15 天，超过这个窗口的日期爬不到
CRAWL_WINDOW_DAYS = 15

SOURCE_LABEL = {
    "import": "导入数据集",
    "crawl": "真爬",
    "focus": "定向爬取",
    "copy": "拷贝",
    "sparse": "车次偏少",
    "missing": "无数据",
}
#: 页面上的徽章色：绿=可信、蓝=真爬、灰=拷贝、橙=可疑、红=缺
SOURCE_TONE = {
    "import": "good",
    "crawl": "info",
    "focus": "info",
    "copy": "muted",
    "sparse": "warn",
    "missing": "bad",
}


def _meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _parse_date(value: str) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _focus_info(src: str) -> tuple:
    """从 crawl:<日期>:stats 里看这次爬取是不是定向的，返回 (是否定向, OD 对数)。"""
    pairs = None
    for token in (src or "").split():
        if token.startswith("pairs="):
            try:
                pairs = int(token.split("=", 1)[1])
            except ValueError:
                pairs = None
    if pairs is None:
        return False, None
    return pairs < 1000, pairs


def classify_date(conn: sqlite3.Connection, date: str, trains: int) -> Dict:
    """单个日期的来源判定（只读）。"""
    imp = _meta(conn, f"import:{date}")
    src = _meta(conn, f"crawl:{date}:stats") or ""
    copied_from = _meta(conn, f"copy:{date}:from")
    if copied_from is None and src.startswith("copied from "):
        # 老数据的兼容路径：早期复制把来源写进了 crawl:*:stats
        copied_from = src.split("copied from ", 1)[1].split(" ", 1)[0] or ""
    focused, pairs = _focus_info(src)
    if trains <= 0:
        source = "missing"
    elif copied_from:
        source = "copy"
    elif focused and trains < SPARSE_TRAINS:
        # 定向爬取本来就不全：这是有意为之，不该报成「调试遗留」
        source = "focus"
    elif trains < SPARSE_TRAINS:
        # 车次太少又说不清来源：多半是调试留下的局部线路，提醒重导/重爬
        source = "sparse"
    elif imp:
        source = "import"
    elif src:
        source = "crawl"
    else:
        source = "copy"          # 没有来源记录的旧数据，最保守的假设
    return {
        "date": date,
        "trains": trains,
        "source": source,
        "label": SOURCE_LABEL[source],
        "tone": SOURCE_TONE[source],
        "copied_from": copied_from or "",
        "pairs": pairs,
        "detail": (imp or src or copied_from or "")[:80],
    }


def date_rows(conn: sqlite3.Connection) -> List[Dict]:
    """所有有数据的日期 + 来源判定。"""
    rows = conn.execute(
        "SELECT date, COUNT(DISTINCT train_no) n FROM schedules GROUP BY date ORDER BY date"
    ).fetchall()
    return [classify_date(conn, r["date"], r["n"]) for r in rows]


def _truthful_dates(rows: Sequence[Dict]) -> List[Dict]:
    """可信日期：导入、真爬、定向爬取，且车次数正常 —— 复制出来的不算基准。"""
    return [r for r in rows if r["source"] in ("import", "crawl", "focus")]


def pick_base(rows: Sequence[Dict], near: Optional[dt.date] = None) -> Optional[Dict]:
    """挑基准日：优先可信日期里车次最多的；同样多就挑离 near 最近的。"""
    pool = _truthful_dates(rows) or [r for r in rows if r["source"] != "missing"]
    if not pool:
        return None
    best = max(r["trains"] for r in pool)
    full = [r for r in pool if r["trains"] >= max(SPARSE_TRAINS, best * 0.5)]
    if not full:
        full = pool
    if near is None:
        return max(full, key=lambda r: (r["trains"], r["date"]))
    return min(full, key=lambda r: (abs((_parse_date(r["date"]) or near) - near), -r["trains"]))


def route_for_date(conn: sqlite3.Connection, date: str) -> List[str]:
    """查这个日期时实际会走哪条取数路径（供页面说明「我查的这趟是哪来的」）。"""
    row = conn.execute("SELECT COUNT(DISTINCT train_no) n FROM schedules WHERE date=?",
                       (date,)).fetchone()
    info = classify_date(conn, date, row["n"] if row else 0)
    if info["source"] == "missing":
        return ["自动复制：查这个日期时会从基准日拷一份（不是真时刻表）"]
    if info["source"] == "copy":
        if info["copied_from"]:
            return [f"拷贝数据：来自 {info['copied_from']}（隔日开行/调图差异看不出来）"]
        return ["拷贝数据：来源没记录（早期批量复制留下的），要最新时刻请真爬这一天"]
    if info["source"] == "sparse":
        return [f"车次偏少（只有 {info['trains']} 趟）：多半是局部线路或调试数据，"
                "建议重新导入或真爬"]
    if info["source"] == "focus":
        pairs = f"{info['pairs']} 个 OD 对" if info.get("pairs") else "部分 OD"
        return [(f"定向爬取（{pairs}，{info['trains']} 趟）：只覆盖了目标城市相关的方向，"
                 "其余 OD 会自动从基准日拷贝补上")]
    return [f"{info['label']}：可以直接用于规划"]


def _count(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> int:
    """安全取一个 COUNT：缺表/缺列都当 0，不让体检整块挂掉。"""
    try:
        return conn.execute(sql, params).fetchone()["c"]
    except sqlite3.OperationalError:
        return 0


def health(conn: sqlite3.Connection) -> Dict:
    """体检项：只报告「具体哪一天/哪条」，并给出可一键执行的修法。"""
    rows = date_rows(conn)
    sparse = [r for r in rows if r["source"] == "sparse"]
    focus_days = [r for r in rows if r["source"] == "focus"]
    copies = [r for r in rows if r["source"] == "copy"]
    truthful = _truthful_dates(rows)
    # 航班：机场代码对得上、但 flights 里 price 为空 —— 删掉对应缓存行即可重取
    # （flight_route_cache 的 origin/destination 存的是城市名，flights 里是机场三字码）
    unpriced = _count(conn, "SELECT COUNT(DISTINCT flight_no) c FROM flights WHERE price IS NULL")
    unpriced_routes = _count(conn,
        "SELECT COUNT(*) c FROM flight_route_cache rc WHERE EXISTS ("
        "  SELECT 1 FROM flights f"
        "   JOIN airports a1 ON a1.code=f.dep_airport"
        "   JOIN airports a2 ON a2.code=f.arr_airport"
        "  WHERE f.price IS NULL AND a1.city=rc.origin AND a2.city=rc.destination)")
    coord_total = _table_count(conn, "geo_cache")
    coord_fail = _table_count(conn, "geo_cache", "failed<>0")
    return {
        "dates": len(rows),
        "truthful": len(truthful),
        "copies": len(copies),
        "focus_days": [r["date"] for r in focus_days],
        "sparse": [r["date"] for r in sparse],
        "flight_unpriced": unpriced,
        "flight_unpriced_routes": unpriced_routes,
        "coord_total": coord_total,
        "coord_failed": coord_fail,
        "items": [
            {
                "id": "copy_ratio",
                "tone": "warn" if copies and not truthful else ("warn" if len(copies) > len(truthful) * 3 else "ok"),
                "text": f"{len(rows)} 天数据里只有 {len(truthful)} 天是导入/真爬，其余 {len(copies)} 天是拷贝",
                "fix": "copies",
                "hint": "拷贝数据看不出隔日开行与调图差异，重要出行日建议真爬",
            },
            {
                "id": "sparse_days",
                "tone": "warn" if sparse else "ok",
                "text": (f"{len(sparse)} 天车次偏少（{', '.join(r['date'] for r in sparse[:4])}）"
                         if sparse else "没有车次异常的日期"),
                "fix": "delete_sparse" if sparse else "",
                "hint": "这些日期多是调试复制留下的局部线路，删掉后查询会自动用基准日补上",
            },
            {
                "id": "focus_days",
                "tone": "info" if focus_days else "ok",
                "text": (f"{len(focus_days)} 天是定向爬取（{', '.join(r['date'] for r in focus_days[:4])}）"
                         if focus_days else "没有定向爬取的日期"),
                "fix": "",
                "hint": "定向只覆盖目标城市方向，其余 OD 查询时会自动从基准日拷贝补上",
            },
            {
                "id": "flight_price",
                "tone": "warn" if unpriced else "ok",
                "text": (f"{unpriced} 个航班缺票价（涉及 {unpriced_routes} 条航线缓存）"
                         if unpriced else "航班缓存都有票价"),
                "fix": "refetch_flights" if unpriced_routes else "",
                "hint": "删掉这些航线的缓存后重新查询即可补齐",
            },
            {
                "id": "coords",
                "tone": "warn" if coord_fail else "ok",
                "text": f"{coord_fail} 条坐标失败（影响里程/票价估算）" if coord_fail else "车站坐标齐备",
                "fix": "prewarm_coords" if coord_fail else "",
                "hint": "跑步进命令重试：python scripts/prewarm_coords.py",
            },
        ],
    }


def _table_count(conn: sqlite3.Connection, table: str, where: str = "") -> int:
    """安全取行数：老库/测试库可能还没有某张表，不该让整个体检报错。"""
    try:
        sql = f"SELECT COUNT(*) c FROM {table}" + (f" WHERE {where}" if where else "")
        return conn.execute(sql).fetchone()["c"]
    except sqlite3.OperationalError:
        return 0


def overview(conn: sqlite3.Connection, today: Optional[str] = None) -> Dict:
    """数据台首屏数据：规模 + 日期来源 + 体检项 + 今天/基准日。"""
    today_d = _parse_date(today) if today else dt.date.today()
    rows = date_rows(conn)
    base = pick_base(rows, today_d)
    return {
        "today": today_d.isoformat(),
        "base": base,
        "stations": _table_count(conn, "stations"),
        "airports": _table_count(conn, "airports"),
        "tourism_cities": _table_count(conn, "city_tourism"),
        "crawl_window_days": CRAWL_WINDOW_DAYS,
        "sparse_below": SPARSE_TRAINS,
        "dates": rows,
        "health": health(conn),
    }


def plan(conn: sqlite3.Connection, targets: Sequence[str], *,
         base: Optional[str] = None, crawl: Sequence[str] = (),
         today: Optional[str] = None) -> Dict:
    """规划「哪些日期要补、从哪天拷、哪几天建议真爬」——只读，不写库。"""
    today_d = _parse_date(today) if today else dt.date.today()
    rows = date_rows(conn)
    by_date = {r["date"]: r for r in rows}
    base_row = by_date.get(base) if base else None
    if base_row is None or base_row["trains"] <= 0:
        base_row = pick_base(rows, today_d)
    crawl_set = {c for c in crawl if _parse_date(c)}
    steps: List[Dict] = []
    for raw in targets:
        day = _parse_date(raw)
        if day is None:
            steps.append({"date": raw, "action": "invalid", "reason": "日期格式应为 YYYY-MM-DD"})
            continue
        date = day.isoformat()
        info = by_date.get(date)
        trains = info["trains"] if info else 0
        want_crawl = date in crawl_set
        out_of_window = want_crawl and (day - today_d).days > CRAWL_WINDOW_DAYS
        if out_of_window:
            steps.append({"date": date, "action": "skip", "trains": trains,
                          "reason": f"超出 {CRAWL_WINDOW_DAYS} 天预售窗口，12306 也查不到，只能拷贝",
                          "then": "copy" if base_row else "blocked"})
            want_crawl = False
        if want_crawl:
            steps.append({"date": date, "action": "crawl", "trains": trains,
                          "reason": "真爬 12306" + ("（会覆盖现有数据）" if trains else "")})
            continue
        if out_of_window:
            continue          # 已经给出 skip 步骤，不再重复安排拷贝
        if date == (base_row or {}).get("date"):
            steps.append({"date": date, "action": "keep", "trains": trains,
                          "reason": "这就是基准日"})
            continue
        if not base_row:
            steps.append({"date": date, "action": "blocked",
                          "reason": "没有可用的基准日：先导入数据集或爬一天"})
            continue
        if trains >= SPARSE_TRAINS and info and info["source"] in ("import", "crawl"):
            steps.append({"date": date, "action": "keep", "trains": trains,
                          "reason": f"{info['label']}数据，无需补"})
            continue
        if trains >= SPARSE_TRAINS and info and info["source"] == "copy":
            steps.append({"date": date, "action": "keep", "trains": trains,
                          "reason": f"已有拷贝（来自 {info['copied_from'] or '未知'}），要最新时刻请改成真爬",
                          "could_crawl": True})
            continue
        steps.append({
            "date": date,
            "action": "copy" if not trains else "overwrite",
            "trains": trains,
            "from": base_row["date"],
            "reason": (f"从基准日 {base_row['date']} 拷一份"
                       if not trains else
                       f"现有 {trains} 趟偏少，用基准日 {base_row['date']} 覆盖"),
        })
    return {
        "base": base_row,
        "today": today_d.isoformat(),
        "crawl_window_days": CRAWL_WINDOW_DAYS,
        "steps": steps,
        "summary": {
            "crawl": sum(1 for s in steps if s["action"] == "crawl"),
            "copy": sum(1 for s in steps if s["action"] in ("copy", "overwrite")),
            "keep": sum(1 for s in steps if s["action"] == "keep"),
            "skip": sum(1 for s in steps if s["action"] in ("skip", "invalid", "blocked")),
        },
        "note": "本计划只读；点「执行计划」才会写库。真爬是按天单独启动的后台任务，需再确认一次。",
    }


def missing_between(conn: sqlite3.Connection, start: str, end: str) -> List[str]:
    """区间内还没有数据的日期（含两端）。"""
    a, b = _parse_date(start), _parse_date(end)
    if not a or not b or b < a:
        return []
    have = {r["date"] for r in date_rows(conn) if r["trains"] >= SPARSE_TRAINS}
    days, cur = [], a
    while cur <= b:
        if cur.isoformat() not in have:
            days.append(cur.isoformat())
        cur += dt.timedelta(days=1)
    return days


def apply_plan(conn: sqlite3.Connection, steps: Iterable[Dict], *,
               dry_run: bool = False, overwrite: bool = False) -> Dict:
    """执行计划里的 copy/overwrite 步骤（crawl 步骤不在这里跑）。"""
    from . import service

    done, skipped = [], []
    for step in steps:
        action = step.get("action")
        if action in ("crawl", "keep", "skip", "invalid", "blocked"):
            skipped.append({"date": step.get("date"), "action": action,
                            "reason": step.get("reason", "")})
            continue
        if action not in ("copy", "overwrite"):
            skipped.append({"date": step.get("date"), "action": action,
                            "reason": "未知动作"})
            continue
        src = step.get("from")
        if not src:
            skipped.append({"date": step["date"], "action": action, "reason": "缺来源日期"})
            continue
        res = service.copy_dates(conn, src, [step["date"]],
                                 overwrite=(overwrite or action == "overwrite"),
                                 dry_run=dry_run, via="数据台一键维护")
        if not res.get("ok"):
            skipped.append({"date": step["date"], "action": action,
                            "reason": res.get("error", "复制失败")})
            continue
        item = dict(res["results"][0] if res.get("results") else {})
        # 标清来源：覆盖目标日原有的「爬取」指纹，别再让它冒充真爬
        if item.get("status") == "copied":
            item["source"] = "copy"
            item["copied_from"] = src
        done.append(item)
    return {"ok": True, "dry_run": bool(dry_run), "done": done, "skipped": skipped,
            "note": "复制只复用车次与停站时刻；隔日/周末开行与临时调图差异无法体现。"}


def delete_date(conn: sqlite3.Connection, date: str, *, dry_run: bool = False) -> Dict:
    """删掉某个日期的时刻表（用于清理调试留下的零碎数据）。"""
    if not _parse_date(date):
        return {"ok": False, "error": "日期格式应为 YYYY-MM-DD"}
    n = conn.execute("SELECT COUNT(*) c FROM schedules WHERE date=?", (date,)).fetchone()["c"]
    if dry_run:
        return {"ok": True, "dry_run": True, "date": date, "rows": n}
    with conn:
        conn.execute("DELETE FROM schedules WHERE date=?", (date,))
        for suffix in ("stats", "pages", "fail"):
            conn.execute("DELETE FROM meta WHERE key=?", (f"crawl:{date}:{suffix}",))
        conn.execute("DELETE FROM meta WHERE key=?", (f"copy:{date}:from",))
    return {"ok": True, "date": date, "deleted": n,
            "note": "已删除；之后查这个日期会自动从基准日拷一份。"}


def clear_flight_cache(conn: sqlite3.Connection, *, dry_run: bool = False) -> Dict:
    """清掉「有航班但没价钱」的航线缓存，让下次查询重新取。"""
    rows = conn.execute(
        "SELECT DISTINCT rc.origin o, rc.destination d FROM flight_route_cache rc WHERE EXISTS ("
        "  SELECT 1 FROM flights f"
        "   JOIN airports a1 ON a1.code=f.dep_airport"
        "   JOIN airports a2 ON a2.code=f.arr_airport"
        "  WHERE f.price IS NULL AND a1.city=rc.origin AND a2.city=rc.destination)"
    ).fetchall()
    pairs = sorted((r["o"], r["d"]) for r in rows)
    if dry_run:
        return {"ok": True, "dry_run": True, "routes": [f"{a}→{b}" for a, b in pairs],
                "count": len(pairs)}
    with conn:
        for a, b in pairs:
            conn.execute("DELETE FROM flight_route_cache WHERE origin=? AND destination=?", (a, b))
    return {"ok": True, "routes": [f"{a}→{b}" for a, b in pairs], "count": len(pairs),
            "note": "下次查询这些航线会重新取价。"}
