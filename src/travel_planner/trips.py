"""我的行程：把参考路线排成真实行程并**存档**。

和数据台里「排成行程」的区别：那个是看过就没了；这里把结果存进独立的
``data/trips.db``（``TRAVEL_PLANNER_TRIPS_DB`` 可覆盖），所以：

* 换一天、换一条路线重排，旧的那份还在，能对比；
* 排不出来的条目（同城选站、没有铁路站的小镇）也**记账**，不是静默丢掉 ——
  数据台能看到「哪些条目排不出城际行程、为什么」，这本身也是路线库的质量体检。

行程仍然是「本地时刻表算出来的参考」：每条都带 ``data_source`` 与 ``note``，
页面照原样显示，不假装是实时车次。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[2]

SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_trips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dedupe_key TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL DEFAULT '',
  route_id TEXT NOT NULL DEFAULT '',
  route_name TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'oneway',
  date TEXT NOT NULL DEFAULT '',
  stops TEXT NOT NULL DEFAULT '[]',
  day_count INTEGER NOT NULL DEFAULT 0,
  ride_count INTEGER NOT NULL DEFAULT 0,
  fare REAL,
  stay_mode TEXT NOT NULL DEFAULT 'auto',
  objective TEXT NOT NULL DEFAULT 'fast',
  mode TEXT NOT NULL DEFAULT 'mixed',
  data_source TEXT NOT NULL DEFAULT '',
  data_label TEXT NOT NULL DEFAULT '',
  ground_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ok',
  note TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_saved_trips_route ON saved_trips(route_id);
CREATE INDEX IF NOT EXISTS idx_saved_trips_date ON saved_trips(date);

-- 排不出城际行程的条目也要留痕（同城选站类、没有铁路站的小镇、缺数据的方向）
CREATE TABLE IF NOT EXISTS trip_skips (
  route_id TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT '',
  fail_kind TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  checked_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS trip_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
_FINAL = (STATE_DONE, STATE_FAILED, STATE_CANCELLED)


def default_path() -> Path:
    value = os.environ.get("TRAVEL_PLANNER_TRIPS_DB")
    return Path(value) if value else ROOT / "data" / "trips.db"


def connect(path: Optional[str | Path] = None) -> sqlite3.Connection:
    p = Path(path) if path else default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """老库存档补列（加了地面接驳统计后，早期建的库要能平滑升上来）。"""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(saved_trips)")}
    for name, decl in (("ground_count", "INTEGER NOT NULL DEFAULT 0"),):
        if name not in existing:
            conn.execute(f"ALTER TABLE saved_trips ADD COLUMN {name} {decl}")
    existing = {row[1] for row in conn.execute("PRAGMA table_info(trip_skips)")}
    for name, decl in (("fail_kind", "TEXT NOT NULL DEFAULT ''"),):
        if name not in existing:
            conn.execute(f"ALTER TABLE trip_skips ADD COLUMN {name} {decl}")


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _key(route_id: str, date: str, stay_mode: str, objective: str, mode: str) -> str:
    """同一条路线 + 同一天 + 同一套偏好 = 同一份存档（换偏好就是另一份）。"""
    return "|".join([route_id or "manual", date or "", stay_mode or "auto",
                     objective or "fast", mode or "mixed"])


def _json_load(text: str, fallback):
    try:
        return json.loads(text or "")
    except (TypeError, ValueError):
        return fallback


# ---------------------------------------------------------------- 存档
def save(conn: sqlite3.Connection, plan: Dict, *, date: str = "", name: str = "",
         note: str = "", replace: bool = False) -> Dict:
    """把一份 ``concrete.plan_trip`` 的结果存下来（默认同键覆盖，留最新一份）。"""
    if not plan or not plan.get("ok"):
        return {"ok": False, "error": "只存排成功了的行程"}
    route = plan.get("route") or {}
    route_id = str(route.get("id") or "")
    when = str(plan.get("date") or date or "")
    stay_mode = str(plan.get("stay_mode") or "auto")
    objective = str(plan.get("objective") or "fast")
    mode = str(plan.get("mode") or "mixed")
    key = _key(route_id, when, stay_mode, objective, mode)
    summary = plan.get("summary") or {}
    source = plan.get("data_source") or {}
    stops = [str(s) for s in (plan.get("stops") or [])]
    ground_count = int(plan.get("ground_count") or summary.get("ground") or 0)
    title = name or f"{route.get('name') or '行程'}（{when}）"
    fields = {
        "dedupe_key": key,
        "name": title,
        "route_id": route_id,
        "route_name": str(route.get("name") or ""),
        "kind": str(route.get("kind") or "oneway"),
        "date": when,
        "stops": json.dumps(stops, ensure_ascii=False),
        "day_count": int(summary.get("days") or len(plan.get("days") or [])),
        "ride_count": int(summary.get("rides") or 0),
        "fare": (float(summary["total_fare"]) if summary.get("total_fare") is not None else None),
        "stay_mode": stay_mode,
        "objective": objective,
        "mode": mode,
        "data_source": str(source.get("source") or ""),
        "data_label": str(source.get("label") or ""),
        "ground_count": ground_count,
        "status": "partial" if ground_count else "ok",
        "note": note or str(plan.get("note") or ""),
        "payload": json.dumps(plan, ensure_ascii=False),
        "updated_at": _now(),
    }
    existing = conn.execute("SELECT id FROM saved_trips WHERE dedupe_key=?", (key,)).fetchone()
    with conn:
        if existing:
            sets = ",".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE saved_trips SET {sets} WHERE id=?",
                         (*fields.values(), existing["id"]))
            trip_id = int(existing["id"])
            created = "updated"
        else:
            fields["created_at"] = _now()
            cols = ",".join(fields)
            conn.execute(f"INSERT INTO saved_trips({cols}) VALUES({','.join('?' * len(fields))})",
                         tuple(fields.values()))
            trip_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            created = "created"
    return {"ok": True, "id": trip_id, "created": created, "name": title,
            "route_id": route_id, "date": when, "days": fields["day_count"],
            "rides": fields["ride_count"], "fare": fields["fare"]}


def save_route(tt_conn, store, route: Dict, date: str, *, replace: bool = False,
               objective: str = "fast", mode: str = "mixed", max_transfers: int = 2,
               buffer_min: int = 30, stay_mode: Optional[str] = None,
               name: str = "", note: str = "") -> Dict:
    """排一条路线并存档。排不出来就记进 ``trip_skips``，返回原因。

    两个连接分工明确：``tt_conn`` 是只读的时刻表（算行程用），
    ``store`` 是 ``trips.db``（存档与留痕用）—— 行程数据不该混进时刻表库。
    """
    from . import concrete

    plan = concrete.plan_trip(tt_conn, route, date, objective=objective, mode=mode,
                              max_transfers=max_transfers, buffer_min=buffer_min,
                              stay_mode=stay_mode or None)
    if not plan.get("ok"):
        reason = str(plan.get("error") or "排不出行程")
        if plan.get("unknown_stops"):
            reason = f"{reason}｜找不到车站：{'、'.join(plan['unknown_stops'])}"
        remember_skip(store, str(route.get("id") or ""), str(route.get("name") or ""), reason,
                      kind=str(route.get("kind") or ""), fail_kind=_fail_kind(plan, reason))
        return {"ok": False, "route_id": route.get("id"), "error": reason,
                "fail_kind": _fail_kind(plan, reason)}
    if not replace:
        key = _key(str((plan.get("route") or {}).get("id") or ""), date,
                   str(plan.get("stay_mode") or "auto"), objective, mode)
        if store.execute("SELECT 1 FROM saved_trips WHERE dedupe_key=?", (key,)).fetchone():
            return {"ok": True, "skipped": True, "reason": "同一天同偏好的存档已存在",
                    "route_id": route.get("id")}
    out = save(store, plan, date=date, name=name, note=note)
    if out.get("ok"):
        clear_skip(store, str(route.get("id") or ""))
    return out


def _fail_kind(plan: Dict, reason: str) -> str:
    """排不出的原因归类（数据台按这个分组显示，而不是让 47 条堆成一坨）。"""
    kind = str(plan.get("kind") or "")
    if kind:
        return kind                     # 引擎已经说清了：base = 基地式周边游
    if "一个城市" in reason:
        return "single_city"            # 同城条目，本来就没有城际段
    if "找不到车站" in reason:
        return "no_station"
    if "没有把这些点连起来" in reason:
        return "no_data"                # 这一天/这个方向缺车次，可以去定向爬
    return "other"


def remember_skip(conn, route_id: str, name: str, reason: str, *, kind: str = "",
                  fail_kind: str = "") -> None:
    with conn:
        conn.execute(
            "INSERT INTO trip_skips(route_id,name,kind,fail_kind,reason,checked_at) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(route_id) DO UPDATE SET name=excluded.name, kind=excluded.kind, "
            "fail_kind=excluded.fail_kind, reason=excluded.reason, checked_at=excluded.checked_at",
            (route_id, name, kind, fail_kind, reason, _now()))


def clear_skip(conn, route_id: str) -> None:
    if not route_id:
        return
    with conn:
        conn.execute("DELETE FROM trip_skips WHERE route_id=?", (route_id,))


# ---------------------------------------------------------------- 读取
def _row_to_summary(row: sqlite3.Row) -> Dict:
    data = dict(row)
    data.pop("payload", None)
    data["stops"] = _json_load(data.get("stops") or "[]", [])
    data["fare"] = (round(float(data["fare"])) if data.get("fare") is not None else None)
    data["kind_label"] = "环线" if data.get("kind") == "loop" else "单向"
    data["ground_count"] = int(data.get("ground_count") or 0)
    # 「含地面接驳」的行程只核实了铁路段，页面要一眼看出来
    data["status_label"] = ("含地面接驳" if data["ground_count"] else "铁路段已核实")
    return data


def list_saved(conn, *, q: str = "", kind: str = "", date: str = "",
               limit: int = 0, skip: int = 0, order: str = "created") -> Dict:
    where, params = [], []
    if q:
        where.append("(name LIKE ? OR route_name LIKE ? OR stops LIKE ?)")
        params += [f"%{q}%"] * 3
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if date:
        where.append("date = ?")
        params.append(date)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = int(conn.execute(f"SELECT COUNT(*) n FROM saved_trips {clause}", params).fetchone()["n"])
    order_by = {"created": "id DESC", "date": "date ASC, id DESC",
                "days": "day_count DESC, id DESC", "fare": "fare DESC"}.get(order, "id DESC")
    start = max(0, int(skip or 0))
    size = max(0, int(limit or 0))
    sql = f"SELECT * FROM saved_trips {clause} ORDER BY {order_by}"
    if size:
        sql += " LIMIT ? OFFSET ?"
        params = params + [size, start]
    rows = [_row_to_summary(r) for r in conn.execute(sql, params).fetchall()]
    return {"ok": True, "total": total, "skip": start, "limit": size,
            "has_more": bool(size) and start + size < total, "trips": rows}


def get(conn, trip_id: int) -> Optional[Dict]:
    row = conn.execute("SELECT * FROM saved_trips WHERE id=?", (int(trip_id),)).fetchone()
    if not row:
        return None
    data = _row_to_summary(row)
    data["plan"] = _json_load(row["payload"] or "{}", {})
    return data


def remove(conn, trip_id: int) -> Dict:
    with conn:
        cur = conn.execute("DELETE FROM saved_trips WHERE id=?", (int(trip_id),))
    return {"ok": bool(cur.rowcount), "deleted": int(cur.rowcount or 0)}


def rename(conn, trip_id: int, name: str) -> Dict:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "名字不能为空"}
    with conn:
        cur = conn.execute("UPDATE saved_trips SET name=?, updated_at=? WHERE id=?",
                           (name, _now(), int(trip_id)))
    return {"ok": bool(cur.rowcount), "id": int(trip_id), "name": name}


def skips(conn, limit: int = 300) -> List[Dict]:
    rows = conn.execute(
        "SELECT route_id,name,kind,fail_kind,reason,checked_at FROM trip_skips "
        "ORDER BY checked_at DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["fail_label"] = SKIP_LABELS.get(item.get("fail_kind") or "", "排不出")
        out.append(item)
    return out


#: 排不出的原因 → 人话（数据台按这个分组）
SKIP_LABELS = {
    "base": "基地式周边游（只有 1 个铁路锚点）",
    "single_city": "同城条目（本来就没有城际段）",
    "no_station": "落脚点没有铁路站",
    "no_data": "缺这一天/这个方向的车次",
    "other": "其他原因",
}


def stats(conn) -> Dict:
    row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(day_count),0) days, COALESCE(SUM(ride_count),0) rides, "
        "COALESCE(SUM(ground_count),0) ground, COALESCE(SUM(fare),0) fare, MAX(created_at) latest "
        "FROM saved_trips").fetchone()
    loops = conn.execute("SELECT COUNT(*) n FROM saved_trips WHERE kind='loop'").fetchone()["n"]
    partial = conn.execute(
        "SELECT COUNT(*) n FROM saved_trips WHERE ground_count > 0").fetchone()["n"]
    dates = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM saved_trips ORDER BY date").fetchall()]
    built = conn.execute("SELECT value FROM trip_meta WHERE key='last_build'").fetchone()
    return {
        "trips": int(row["n"] or 0), "loops": int(loops or 0),
        "oneway": int(row["n"] or 0) - int(loops or 0),
        "days": int(row["days"] or 0), "rides": int(row["rides"] or 0),
        "ground": int(row["ground"] or 0), "partial": int(partial or 0),
        "fare": round(float(row["fare"] or 0)) or None,
        "latest": row["latest"] or "", "dates": dates,
        "skips": len(skips(conn, limit=1000)),
        "last_build": _json_load((built or {})["value"] if built else "", {}),
    }


def coverage(conn) -> Dict:
    """路线库的存档覆盖率：哪些还没排、哪些排不出来（按原因分组）。"""
    from . import routes as routes_mod

    items = routes_mod.query()
    saved_ids = {r["route_id"] for r in conn.execute(
        "SELECT DISTINCT route_id FROM saved_trips WHERE route_id<>''").fetchall()}
    skip_rows = {r["route_id"]: r["reason"] for r in conn.execute(
        "SELECT route_id, reason FROM trip_skips").fetchall()}
    pending = [{"id": r["id"], "name": r.get("name"), "kind": r.get("kind")}
               for r in items if r["id"] not in saved_ids and r["id"] not in skip_rows]
    all_skips = skips(conn, limit=1000)
    groups: Dict[str, int] = {}
    for item in all_skips:
        label = item.get("fail_label") or "排不出"
        groups[label] = groups.get(label, 0) + 1
    return {
        "routes": len(items), "saved": len([r for r in items if r["id"] in saved_ids]),
        "skipped": len([r for r in items if r["id"] in skip_rows]),
        "pending": len(pending),
        "pending_items": pending[:60],
        "skip_groups": [{"label": k, "count": v}
                        for k, v in sorted(groups.items(), key=lambda x: -x[1])],
        "skips": all_skips[:60],
    }


def export_json(conn) -> str:
    items = []
    for row in conn.execute("SELECT * FROM saved_trips ORDER BY id").fetchall():
        item = _row_to_summary(row)
        item["plan"] = _json_load(row["payload"] or "{}", {})
        items.append(item)
    return json.dumps({"exported": _now(), "trips": items}, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- 批量建档
class TripBuildJob:
    """后台把整库（或指定几条）路线排成行程并存档。

    和爬取任务分开：这里的读操作都在本地时刻表上，写只落在 ``trips.db``，
    所以可以和爬取并行跑，也不会碰 12306。同样支持进度、日志与中途取消。
    """

    def __init__(self, date: str, *, stay_mode: str = "", objective: str = "fast",
                 mode: str = "mixed", replace: bool = False, ids: Optional[Sequence[str]] = None,
                 db_path: Optional[str] = None, trip_db: Optional[str] = None,
                 limit: int = 0):
        self.date = date
        self.stay_mode = stay_mode
        self.objective = objective
        self.mode = mode
        self.replace = replace
        self.ids = list(ids) if ids else None
        self.db_path = db_path
        self.trip_db = trip_db
        self.limit = int(limit or 0)
        self.state = STATE_RUNNING
        self.phase = "准备"
        self.done = 0
        self.total = 0
        self.saved = 0
        self.skipped = 0
        self.failed = 0
        self.error = ""
        self.started = dt.datetime.now()
        self.finished: Optional[dt.datetime] = None
        self.logs: List[str] = []
        self.failures: List[Dict] = []
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- 供任务线程回调 ----
    def _log(self, message: str) -> None:
        self.logs.append(f"{dt.datetime.now().strftime('%H:%M:%S')} {message}")
        if len(self.logs) > 400:
            del self.logs[:100]

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def start(self) -> "TripBuildJob":
        self._thread = threading.Thread(target=self._run, name=f"trips-{self.date}", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        from . import db, routes as routes_mod

        conn = None
        trips = None
        final_state = STATE_DONE
        try:
            conn = db.connect(self.db_path)
            trips = connect(self.trip_db)
            items = routes_mod.query()
            if self.ids:
                wanted = set(self.ids)
                items = [r for r in items if r["id"] in wanted]
            if self.limit:
                items = items[: self.limit]
            self.total = len(items)
            self.phase = "排行程"
            self._log(f"开始：{self.total} 条路线 → {self.date}"
                      f"（停留 {self.stay_mode or '自动'}，偏好 {self.objective}/{self.mode}）")
            for item in items:
                if self._cancel.is_set():
                    final_state = STATE_CANCELLED
                    self._log("已取消（已存档的会保留）")
                    break
                out = save_route(conn, trips, item, self.date, replace=self.replace,
                                 objective=self.objective, mode=self.mode,
                                 stay_mode=self.stay_mode or None)
                if out.get("ok") and out.get("skipped"):
                    self.skipped += 1
                elif out.get("ok"):
                    self.saved += 1
                    self._log(f"✓ {item['id']} → {out.get('days')} 天 / {out.get('rides')} 段")
                else:
                    self.failed += 1
                    self.failures.append({"id": item["id"], "name": item.get("name"),
                                          "reason": out.get("error")})
                    self._log(f"✗ {item['id']}：{out.get('error')}")
                self.done += 1
            if final_state == STATE_DONE:
                self._log(f"完成：新增 {self.saved} 份，跳过 {self.skipped} 份，"
                          f"排不出 {self.failed} 条")
        except Exception as exc:                       # noqa: BLE001
            final_state = STATE_FAILED
            self.error = f"{type(exc).__name__}: {exc}"
            self._log("失败：" + self.error)
        finally:
            summary = {"date": self.date, "saved": self.saved, "skipped": self.skipped,
                       "failed": self.failed, "total": self.total, "state": final_state,
                       "finished": _now()}
            if trips is not None:
                try:
                    with trips:
                        trips.execute(
                            "INSERT INTO trip_meta(key,value) VALUES('last_build',?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (json.dumps(summary, ensure_ascii=False),))
                    trips.close()
                except Exception:                      # noqa: BLE001
                    pass
            if conn is not None:
                try:
                    conn.close()
                except Exception:                      # noqa: BLE001
                    pass
            # 终态放在**关完连接之后**：外面 wait_for(job) 的语义是
            # 「任务已经收工，不会再碰时刻表/存档文件」，否则测试里删临时目录会撞上占用。
            self.state = final_state
            self.finished = dt.datetime.now()

    def cancel(self) -> None:
        if self.state == STATE_RUNNING:
            self._cancel.set()
            self._log("收到取消请求，等当前这条排完…")

    def snapshot(self, *, include_logs: int = 40) -> Dict:
        end = self.finished or dt.datetime.now()
        elapsed = (end - self.started).total_seconds()
        pct = round(self.done / self.total * 100) if self.total else None
        eta = None
        if self.state == STATE_RUNNING and self.done and self.total:
            eta = round(elapsed / self.done * (self.total - self.done))
        return {
            "date": self.date, "state": self.state, "phase": self.phase,
            "done": self.done, "total": self.total, "percent": pct,
            "saved": self.saved, "skipped": self.skipped, "failed": self.failed,
            "elapsed": round(elapsed), "eta": eta, "error": self.error,
            "failures": self.failures[-40:],
            "logs": self.logs[-include_logs:],
            "finished": self.finished.isoformat(timespec="seconds") if self.finished else "",
        }


class BuildRegistry:
    """进程内任务表：批量排行程同一时刻只跑一个（避免重复写 trips.db）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Optional[TripBuildJob] = None

    def start(self, date: str, **kwargs) -> Dict:
        with self._lock:
            cur = self._current
            if cur and cur.state == STATE_RUNNING:
                return {"ok": False, "error": "已有一个批量建档任务在跑，先等它结束或取消",
                        "job": cur.snapshot()}
            job = TripBuildJob(date, **kwargs)
            self._current = job
        job.start()
        return {"ok": True, "job": job.snapshot()}

    def current(self) -> Optional[TripBuildJob]:
        return self._current

    def status(self) -> Dict:
        cur = self._current
        return {"ok": True, "job": cur.snapshot() if cur else None}

    def cancel(self) -> Dict:
        cur = self._current
        if not cur:
            return {"ok": False, "error": "没有批量建档任务"}
        cur.cancel()
        return {"ok": True, "job": cur.snapshot()}


#: 全局注册表（web.py 与脚本共用）
BUILD = BuildRegistry()


def wait_for(job: TripBuildJob, timeout: float = 10.0) -> bool:
    """测试用：等任务收尾。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.state in _FINAL:
            return True
        time.sleep(0.05)
    return job.state in _FINAL
