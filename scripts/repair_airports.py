"""Repair airport city labels inferred from cached flight route endpoints."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from travel_planner import db  # noqa: E402
from travel_planner.ingest.fliggy import repair_airport_cities  # noqa: E402


def main() -> None:
    conn = db.connect()
    try:
        changed = repair_airport_cities(conn)
        print(f"已修复/补齐机场城市 {changed} 条")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
