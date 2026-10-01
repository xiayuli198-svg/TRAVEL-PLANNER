"""导入静态机场主数据到 airport_master。

用法：
    python scripts/import_airports_master.py
    python scripts/import_airports_master.py data/airports_master.csv --replace --sync-airports

主数据与动态 airports 表分开保存；默认不会覆盖动态机场名称，--sync-airports
只为动态表补齐尚不存在的机场。
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from travel_planner import db  # noqa: E402

FIELDS = (
    "code", "icao", "name", "name_en", "city", "province", "country",
    "lat", "lon", "active", "kind",
)


def read_rows(path: Path) -> list[dict[str, object]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    out: list[dict[str, object]] = []
    seen: set[str] = set()
    for no, raw in enumerate(rows, 2):
        row = {k: (raw.get(k) or "").strip() for k in FIELDS}
        code = row["code"].upper()
        if len(code) != 3 or not code.isascii() or not code.isalpha():
            raise ValueError(f"第 {no} 行 IATA code 无效: {code!r}")
        if code in seen:
            raise ValueError(f"第 {no} 行 IATA code 重复: {code}")
        seen.add(code)
        if not row["name"] or not row["city"]:
            raise ValueError(f"第 {no} 行缺少 name/city: {code}")
        try:
            lat = float(row["lat"]) if row["lat"] else None
            lon = float(row["lon"]) if row["lon"] else None
            active = int(row["active"] or 1)
        except ValueError as exc:
            raise ValueError(f"第 {no} 行坐标或 active 无效: {code}") from exc
        if active not in (0, 1):
            raise ValueError(f"第 {no} 行 active 必须是 0/1: {code}")
        row.update(code=code, lat=lat, lon=lon, active=active)
        out.append(row)
    if not out:
        raise ValueError(f"机场主数据为空: {path}")
    return out


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS airport_master (
          code TEXT PRIMARY KEY,
          icao TEXT,
          name TEXT NOT NULL,
          name_en TEXT,
          city TEXT NOT NULL,
          province TEXT,
          country TEXT,
          lat REAL,
          lon REAL,
          active INTEGER NOT NULL DEFAULT 1,
          kind TEXT,
          source TEXT,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_airport_master_city ON airport_master(city)")


def import_rows(conn: sqlite3.Connection, rows: list[dict[str, object]], replace: bool,
                sync_airports: bool) -> None:
    ensure_table(conn)
    if replace:
        conn.execute("DELETE FROM airport_master")
    conn.executemany("""
        INSERT INTO airport_master
          (code,icao,name,name_en,city,province,country,lat,lon,active,kind,source,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,'static-csv',CURRENT_TIMESTAMP)
        ON CONFLICT(code) DO UPDATE SET
          icao=excluded.icao, name=excluded.name, name_en=excluded.name_en,
          city=excluded.city, province=excluded.province, country=excluded.country,
          lat=excluded.lat, lon=excluded.lon, active=excluded.active,
          kind=excluded.kind, source=excluded.source, updated_at=CURRENT_TIMESTAMP
    """, [tuple(row[k] for k in FIELDS) for row in rows])
    if sync_airports:
        # 同步到规划实际读取的 airports 表，并标记为 master，补齐动态缓存。
        db.import_airports_master(conn, rows, replace=False)
    db.set_meta(conn, "airport_master:source", "data/airports_master.csv")
    db.set_meta(conn, "airport_master:count", str(len(rows)))
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser(description="导入静态机场主数据")
    ap.add_argument("csv", nargs="?", type=Path, default=ROOT / "data" / "airports_master.csv")
    ap.add_argument("--db", type=Path, default=None, help="SQLite 路径")
    ap.add_argument("--replace", action="store_true", help="导入前清空 airport_master")
    ap.add_argument("--sync-airports", action="store_true", help="为动态 airports 表补齐缺失记录")
    args = ap.parse_args()
    path = args.csv if args.csv.is_absolute() else ROOT / args.csv
    rows = read_rows(path)
    conn = db.connect(args.db)
    try:
        import_rows(conn, rows, args.replace, args.sync_airports)
    finally:
        conn.close()
    print(f"已导入 {len(rows)} 条机场主数据 -> airport_master")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
