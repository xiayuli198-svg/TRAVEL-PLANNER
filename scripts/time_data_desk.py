"""给数据台的几个接口测个时：慢在哪一层（连库 / 聚合查询 / 体检项）。

用法：python -X utf8 scripts/time_data_desk.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from travel_planner import db, maintenance       # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def timed(label: str, fn):
    t = time.perf_counter()
    out = fn()
    print(f"  {label}: {time.perf_counter() - t:.2f}s" + (f" → {out}" if out is not None else ""))
    return out


def main() -> int:
    print("连库与各步耗时：")
    conn = timed("db.connect()", db.connect)
    timed("COUNT(schedules)", lambda: conn.execute("SELECT COUNT(*) c FROM schedules").fetchone()["c"])
    timed("COUNT(DISTINCT date)", lambda: conn.execute(
        "SELECT COUNT(DISTINCT date) c FROM schedules").fetchone()["c"])
    timed("date_rows()", lambda: len(maintenance.date_rows(conn)))
    items = timed("health()", lambda: len(maintenance.health(conn).get("items", [])))
    timed("overview()", lambda: len(maintenance.overview(conn).get("dates", [])))
    timed("page_count()", lambda: conn.execute("PRAGMA page_count").fetchone()[0])
    timed("freelist", lambda: conn.execute("PRAGMA freelist_count").fetchone()[0])
    conn.close()
    print(f"（体检项 {items} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
