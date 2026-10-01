"""检查某日上海始发车次（验证方向覆盖）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

DB = Path(__file__).resolve().parent.parent / "data" / "timetable.db"


def main() -> None:
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-09-09"
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    print(f"--- {date} 上海始发车次 ---")
    for r in conn.execute(
            "SELECT train_code, station_name, dep FROM schedules "
            "WHERE date=? AND seq=1 AND station_name LIKE '上海%' ORDER BY dep LIMIT 40",
            (date,)):
        print(f"  {r['train_code']:8s} {r['station_name']} {r['dep']}")


if __name__ == "__main__":
    main()
