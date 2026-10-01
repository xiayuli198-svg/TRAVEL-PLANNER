"""数据检查脚本：天偏移分布、跨天车次样例、入库统计。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "timetable.db"


def main() -> None:
    date = sys.argv[1] if len(sys.argv) > 1 else None
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    if date is None:
        date = conn.execute("SELECT DISTINCT date FROM schedules ORDER BY date DESC LIMIT 1").fetchone()[0]
    print(f"检查日期: {date}")
    print("--- 天偏移分布(day) ---")
    for r in conn.execute(
            "SELECT day, COUNT(DISTINCT train_no) n FROM schedules WHERE date=? GROUP BY day", (date,)):
        print(f"  day={r['day']}: {r['n']} 车次")
    print("--- 跨天车次样例 ---")
    rows = conn.execute(
        "SELECT train_no, train_code FROM schedules WHERE date=? AND day<>0 "
        "GROUP BY train_no LIMIT 5", (date,)).fetchall()
    for r in rows:
        stops = conn.execute(
            "SELECT seq, station_name, day, arr, dep FROM schedules "
            "WHERE train_no=? AND date=? ORDER BY seq", (r["train_no"], date)).fetchall()
        parts = [f"{s['station_name']}(d{s['day']} {s['arr']}/{s['dep']})" for s in stops]
        print(f"  {r['train_code']}({r['train_no'][-8:]}): {' '.join(parts)}")
    print("--- 入库车次数 ---")
    print(conn.execute(
        "SELECT COUNT(DISTINCT train_no) FROM schedules WHERE date=?", (date,)).fetchone()[0])


if __name__ == "__main__":
    main()
