"""geo_cache 预取进度检查。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "timetable.db"


def main() -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    total = conn.execute("SELECT COUNT(*) c FROM geo_cache").fetchone()["c"]
    ok = conn.execute("SELECT COUNT(*) c FROM geo_cache WHERE failed=0").fetchone()["c"]
    recent = conn.execute(
        "SELECT COUNT(*) c FROM geo_cache "
        "WHERE fetched_at >= datetime('now', '-3 minutes')").fetchone()["c"]
    print(f"geo_cache 共 {total} 条：成功 {ok}，失败 {total - ok}，最近3分钟写入 {recent}")


if __name__ == "__main__":
    main()
