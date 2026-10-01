"""按日期列出数据量与来源（验证自动复制/导入/爬取各自生效）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

DB = Path(__file__).resolve().parent.parent / "data" / "timetable.db"


def main() -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    print("日期        车次数  来源")
    for r in conn.execute(
            "SELECT date, COUNT(DISTINCT train_no) n FROM schedules "
            "GROUP BY date ORDER BY date"):
        src = conn.execute("SELECT value FROM meta WHERE key=?",
                           (f"crawl:{r['date']}:stats",)).fetchone()
        imp = conn.execute("SELECT value FROM meta WHERE key=?",
                           (f"import:{r['date']}",)).fetchone()
        origin = (f"crawl: {src['value'][:60]}" if src else
                  (f"import: {imp['value'][:60]}" if imp else "无记录"))
        print(f"{r['date']}  {r['n']:>5}  {origin}")


if __name__ == "__main__":
    main()
