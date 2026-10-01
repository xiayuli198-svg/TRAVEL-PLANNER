"""导入静态机场主数据。

用法：
    python scripts/import_airports.py
    python scripts/import_airports.py --csv path/to/airports_master.csv --replace
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from travel_planner import db  # noqa: E402


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description="导入静态机场主数据")
    ap.add_argument("--csv", dest="csv_path", type=Path,
                    default=root / "data" / "airports_master.csv")
    ap.add_argument("--db", dest="db_path", type=Path, default=None)
    ap.add_argument("--replace", action="store_true",
                    help="删除旧的静态主数据后再导入；不影响动态机场")
    args = ap.parse_args()
    with args.csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    conn = db.connect(args.db_path)
    count = db.import_airports_master(conn, rows, replace=args.replace)
    db.set_meta(conn, "airports:master_source", str(args.csv_path.name))
    db.set_meta(conn, "airports:master_count", str(count))
    total = conn.execute("SELECT COUNT(*) FROM airports").fetchone()[0]
    print(f"已导入静态机场 {count} 条；机场目录共 {total} 座。")


if __name__ == "__main__":
    main()
