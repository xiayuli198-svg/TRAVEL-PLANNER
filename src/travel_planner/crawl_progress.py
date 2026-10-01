"""爬取进度台账：让「断电/关窗口/换一次定向范围」之后都能接着爬。

## 为什么不是原来那个 `crawl_state_<date>.txt`

原来的断点只记「第几个 OD 做完了」（`index 596`），恢复时 `pairs[596:]` —— 这有三个硬伤：

1. **换一次 OD 列表就错位**：全国爬取（6006 对）爬到第 596 个中断，之后用「定向爬取」（320 对）
   勾了续爬，`pairs[596:]` 直接是空列表，跑完报「入库 0 趟」，看着就像没续爬；
   反过来换更长的列表还会重复爬。
2. **断电那一刻正在做的 OD 无人认领**：序号只记「已完成」，正在做的那一对只能靠重跑撞回来。
3. **进程重启后页面看不到任何痕迹**：`/api/crawl/status` 只有内存里的任务，用户不知道有没有残留进度、
   剩多少、能不能接着爬 —— 于是只能从头再点一次。

## 现在用的账本（SQLite，逐 OD 落盘）

- `crawl_pairs(date, od, status, trains, tries, updated_at)`：**按 OD 成员记账**，不按序号。
  开爬前先把这一对写成 `running`，做完改成 `done`/`failed` 并立刻 commit —— 断电时那一条留在
  `running`，续爬时会**重试**它（这正是我们想要的语义：没确认完成的就该重来）。
- `crawl_runs(date, planned, started_at, updated_at, finished_at, state, discovered, ingested, note)`：
  这一天的任务总账，页面据此显示「已完成 596/6006，剩余 5410」。
- `discovered_<date>.jsonl` 继续留着当「发现的车次」账本（原地追加），已入库的车次靠 schedules 表去重。

因为记账按 OD，所以：全国爬到一半换定向、定向跑到一半换城市、同一日期爬第二次，都不会重复也不会漏。
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl_pairs (
  date       TEXT NOT NULL,
  od         TEXT NOT NULL,
  status     TEXT NOT NULL,
  trains     INTEGER NOT NULL DEFAULT 0,
  tries      INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (date, od)
);
CREATE TABLE IF NOT EXISTS crawl_runs (
  date        TEXT PRIMARY KEY,
  planned     INTEGER NOT NULL DEFAULT 0,
  started_at  TEXT NOT NULL DEFAULT '',
  updated_at  TEXT NOT NULL DEFAULT '',
  finished_at TEXT NOT NULL DEFAULT '',
  state       TEXT NOT NULL DEFAULT '',
  discovered  INTEGER NOT NULL DEFAULT 0,
  ingested    INTEGER NOT NULL DEFAULT 0,
  note        TEXT NOT NULL DEFAULT ''
);
"""


def od_key(frm: str, to: str) -> str:
    return f"{str(frm).upper()}:{str(to).upper()}"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def start_run(conn: sqlite3.Connection, date: str, planned: int, *,
              note: str = "", reset: bool = False,
              pairs: Optional[Sequence[Tuple[str, str]]] = None) -> None:
    """开一次任务。``reset=True``（不续爬）时把这一天的旧账清掉，重新开始。"""
    ensure_schema(conn)
    if reset:
        conn.execute("DELETE FROM crawl_pairs WHERE date=?", (date,))
    with conn:
        if pairs is not None:
            # 复用现有 meta 表保存本轮范围，换定向城市后统计不能混入范围外的 OD。
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                         (f"crawl:{date}:ods", json.dumps([od_key(a, b) for a, b in pairs])))
        if reset:
            conn.execute(
                "INSERT INTO crawl_runs(date,planned,started_at,updated_at,state,note) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(date) DO UPDATE SET planned=excluded.planned, "
                "started_at=excluded.started_at, updated_at=excluded.updated_at, "
                "state=excluded.state, note=excluded.note, finished_at='', "
                "discovered=0, ingested=0",
                (date, int(planned), _now(), _now(), "running", note))
        else:
            conn.execute(
                "INSERT INTO crawl_runs(date,planned,started_at,updated_at,state,note) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(date) DO UPDATE SET planned=excluded.planned, "
                "started_at=excluded.started_at, updated_at=excluded.updated_at, "
                "state=excluded.state, note=excluded.note",
                (date, int(planned), _now(), _now(), "running", note))


def update_run(conn: sqlite3.Connection, date: str, **fields) -> None:
    """更新总账（state / discovered / ingested / note / finished）。"""
    if not fields:
        return
    ensure_schema(conn)
    allowed = {"planned", "state", "discovered", "ingested", "note", "finished_at"}
    cols = [k for k in fields if k in allowed]
    if not cols:
        return
    fields = {k: fields[k] for k in cols}
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k}=?" for k in fields)
    with conn:
        conn.execute(f"UPDATE crawl_runs SET {sets} WHERE date=?",
                     (*fields.values(), date))


def mark_running(conn: sqlite3.Connection, date: str, frm: str, to: str) -> None:
    """开爬某一对之前先记账：断电时它会留在 running，续爬时被重试。"""
    ensure_schema(conn)
    key = od_key(frm, to)
    with conn:
        conn.execute(
            "INSERT INTO crawl_pairs(date,od,status,trains,tries,updated_at) "
            "VALUES(?,?,?,0,1,?) "
            "ON CONFLICT(date,od) DO UPDATE SET status=excluded.status, "
            "tries=crawl_pairs.tries+1, updated_at=excluded.updated_at",
            (date, key, STATUS_RUNNING, _now()))


def mark_done(conn: sqlite3.Connection, date: str, frm: str, to: str, trains: int = 0) -> None:
    ensure_schema(conn)
    with conn:
        conn.execute(
            "INSERT INTO crawl_pairs(date,od,status,trains,tries,updated_at) "
            "VALUES(?,?,?,?,1,?) "
            "ON CONFLICT(date,od) DO UPDATE SET status=?, trains=?, updated_at=?",
            (date, od_key(frm, to), STATUS_DONE, int(trains), _now(),
             STATUS_DONE, int(trains), _now()))


def mark_failed(conn: sqlite3.Connection, date: str, frm: str, to: str) -> None:
    """本轮未恢复的失败留账，下次续爬会重试。"""
    ensure_schema(conn)
    with conn:
        conn.execute(
            "INSERT INTO crawl_pairs(date,od,status,trains,tries,updated_at) "
            "VALUES(?,?,?,0,1,?) "
            "ON CONFLICT(date,od) DO UPDATE SET status=?, updated_at=?",
            (date, od_key(frm, to), STATUS_FAILED, _now(), STATUS_FAILED, _now()))


def done_ods(conn: sqlite3.Connection, date: str) -> Set[str]:
    """已确认完成的 OD（续爬时跳过它们）。"""
    ensure_schema(conn)
    rows = conn.execute("SELECT od FROM crawl_pairs WHERE date=? AND status=?",
                        (date, STATUS_DONE)).fetchall()
    return {r["od"] for r in rows}


def filter_pending(conn: sqlite3.Connection, date: str,
                   pairs: Sequence[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """按成员过滤出还没完成的 OD。这是修掉「换列表就错位」的关键一步。"""
    done = done_ods(conn, date)
    return [(a, b) for a, b in pairs if od_key(a, b) not in done]


def summary(conn: sqlite3.Connection, date: str) -> Dict:
    """给页面看的进度：已完成 / 失败 / 进行中 / 计划总数 / 剩余。"""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT status, COUNT(*) c FROM crawl_pairs WHERE date=? GROUP BY status",
        (date,)).fetchall()
    counts = {r["status"]: r["c"] for r in rows}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
        scope_row = conn.execute("SELECT value FROM meta WHERE key=?", (f"crawl:{date}:ods",)).fetchone()
        if scope_row:
            try:
                scope = json.loads(scope_row["value"])
            except (ValueError, TypeError):
                scope = None
            if isinstance(scope, list) and all(isinstance(k, str) for k in scope):
                keys = set(scope)
                counts = Counter(r["status"] for r in conn.execute(
                    "SELECT od,status FROM crawl_pairs WHERE date=?", (date,)) if r["od"] in keys)
    run = conn.execute("SELECT * FROM crawl_runs WHERE date=?", (date,)).fetchone()
    planned = int(run["planned"]) if run else 0
    done = counts.get(STATUS_DONE, 0)
    failed = counts.get(STATUS_FAILED, 0)
    running = counts.get(STATUS_RUNNING, 0)
    # running 的那一条是「断电时正在做、还没确认完成」的，续爬会重试它，所以算进剩余
    remaining = max(planned - done, 0) if planned else 0
    return {
        "date": date,
        "planned": planned,
        "done": done,
        "failed": failed,
        "running": running,
        "remaining": remaining,
        "percent": round(done / planned * 100) if planned else None,
        "state": (run["state"] if run else ""),
        "note": (run["note"] if run else ""),
        "discovered": int(run["discovered"]) if run else 0,
        "ingested": int(run["ingested"]) if run else 0,
        "started_at": (run["started_at"] if run else ""),
        "updated_at": (run["updated_at"] if run else ""),
        "finished_at": (run["finished_at"] if run else ""),
        # 有账可续：有完成记录但还没跑完，或者上次是被中断/取消的
        "resumable": bool(planned or done or failed or running) and (
            run is None or run["state"] != "done" or remaining > 0 or failed > 0),
    }


def progress_rows(conn: sqlite3.Connection, date: str, limit: int = 200) -> List[Dict]:
    """逐 OD 明细（页面排查用：哪些失败、哪些还没做）。"""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT od,status,trains,tries,updated_at FROM crawl_pairs WHERE date=? "
        "ORDER BY updated_at DESC LIMIT ?", (date, limit)).fetchall()
    return [dict(r) for r in rows]


def legacy_state(conn: sqlite3.Connection, date: str, pairs: Sequence[Tuple[str, str]],
                 index: int, *, exclude: Optional[Set[str]] = None) -> int:
    """把旧格式断点（按序号）迁移成 OD 成员账。

    仅在全国爬取时成立：那份 OD 列表是按城市名排序后嵌套生成的，**确定性**，
    所以「前 index 对已完成」可以直接翻译成成员记账。定向爬取的列表每次都会重算，
    翻译出来就是错的 —— 那种情况返回 0（不迁移，老老实实重爬一遍，但已入库的车次不会重复抓）。
    """
    ensure_schema(conn)
    if index <= 0 or not pairs:
        return 0
    migrated = 0
    with conn:
        for i, (a, b) in enumerate(pairs[:index], start=1):
            if exclude and od_key(a, b) in exclude:
                continue
            if i > index:
                break
            conn.execute(
                "INSERT OR IGNORE INTO crawl_pairs(date,od,status,trains,tries,updated_at) "
                "VALUES(?,?,?,0,0,?)",
                (date, od_key(a, b), STATUS_DONE, _now()))
            migrated += 1
    return migrated


def legacy_info(date: str, root: Optional[object] = None) -> Optional[Dict]:
    """查旧格式断点（`crawl_state_<date>.txt` + `discovered_<date>.jsonl`）的残留。

    旧断点只记「第几对 OD」，换过爬取范围就会错位，所以**不能**当续爬依据；
    但它是「这一天爬过、没爬完」的证据，页面该把它如实说出来，而不是装作什么都没有
    （用户的原话就是「中断了没有接着爬的功能吧」—— 连痕迹都看不到，当然只能重爬）。
    """
    from . import db as _db
    base = Path(root) if root is not None else _db.PROJECT_ROOT
    data_dir = base / "data"
    state = data_dir / f"crawl_state_{date}.txt"
    discovered = data_dir / f"discovered_{date}.jsonl"
    if not state.exists() and not discovered.exists():
        return None
    phase, index = "", 0
    if state.exists():
        try:
            for line in state.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("phase "):
                    phase = line.split()[1]
                elif line.startswith("index "):
                    try:
                        index = int(line.split()[1])
                    except (ValueError, IndexError):
                        index = 0
        except OSError:
            pass
    found = 0
    if discovered.exists():
        try:
            with discovered.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.strip():
                        found += 1
        except OSError:
            found = 0
    return {
        "phase": phase or "pairs",
        "index": index,
        "discovered_lines": found,
        "files": [p.name for p in (state, discovered) if p.exists()],
        "text": (f"发现旧格式断点：{phase or 'pairs'} 阶段第 {index} 对，"
                 f"已发现 {found} 趟车。旧断点只记序号，换过爬取范围就会错位，"
                 f"所以下次爬取会按 OD 重新核对一遍（已入库的车次不会重复抓）。"),
    }


def recent_runs(conn: sqlite3.Connection, limit: int = 8) -> List[Dict]:
    """最近几次爬取（页面显示「哪天爬到哪、能不能续」）。"""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM crawl_runs ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        item = summary(conn, r["date"])
        item["label"] = f"{r['date']} · {item['done']}/{item['planned'] or '?'}"
        out.append(item)
    return out


def iter_ods(pairs: Iterable[Tuple[str, str]]) -> Iterable[Tuple[str, str]]:
    for a, b in pairs:
        yield (str(a).upper(), str(b).upper())


def note_for(summary_data: Dict) -> str:
    """一句话说明现在的进度状态（页面横幅与日志共用）。"""
    s = summary_data
    if not s.get("planned"):
        return ""
    tail = f"（剩余 {s['remaining']}）" if s.get("remaining") else ""
    if s.get("state") == "done" and not s.get("remaining") and not s.get("failed"):
        return f"上次已完成 {s['done']}/{s['planned']} 对 OD{tail}"
    if s.get("state") == "partial":
        return f"上次部分完成 {s['done']}/{s['planned']} 对 OD{tail}，续爬会补失败 OD 和未入库车次"
    if s.get("state") == "done" and (s.get("remaining") or s.get("failed")):
        return f"本轮完成 {s['done']}/{s['planned']} 对 OD{tail}，还有失败项可续爬"
    if s.get("planned") or s.get("done") or s.get("running") or s.get("failed"):
        when = s.get("updated_at") or s.get("started_at") or ""
        return (f"上次中断在 {s['done']}/{s['planned']} 对 OD{tail}"
                + (f"，停在 {when}" if when else ""))
    return ""
