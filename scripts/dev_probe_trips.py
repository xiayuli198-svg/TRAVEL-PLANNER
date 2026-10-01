"""开发探针：看看「把路线库排成行程」要花多久，以及该用哪一天的数据。

用法：
    python scripts/dev_probe_trips.py [条数]

输出：每个可用日期的数据来源等级 + 抽几条路线跑 plan_trip 的耗时，
用来估算「整库 100+ 条批量存档」要跑几分钟（决定要不要做成后台任务）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from travel_planner import concrete, db, maintenance, routes as routes_mod  # noqa: E402


def main(argv=None) -> int:
    limit = int(argv[1]) if argv and len(argv) > 1 else 3
    conn = db.connect()
    print("== 可用日期的数据来源 ==")
    for row in maintenance.date_rows(conn):
        print(f"  {row.get('date')}  {row.get('tier') or row.get('label') or ''}"
              f"  trains={row.get('trains')}")
    items = routes_mod.query()
    print(f"\n== 路线库 {len(items)} 条，抽 {limit} 条计时 ==")
    total = 0.0
    ok = 0
    for item in items[:limit]:
        start = time.time()
        try:
            out = concrete.plan_trip(conn, item, "2026-06-17")
        except Exception as exc:                       # noqa: BLE001
            print(f"  {item['id']}: 抛异常 {type(exc).__name__}: {exc}")
            continue
        cost = time.time() - start
        total += cost
        ok += 1 if out.get("ok") else 0
        summary = out.get("summary") or {}
        print(f"  {item['id']}: ok={out.get('ok')} "
              f"days={summary.get('days')} rides={summary.get('rides')} "
              f"{cost:.2f}s  {out.get('error') or ''}")
    if ok:
        avg = total / ok
        print(f"\n平均 {avg:.2f}s/条 → 整库 {len(items)} 条约 {avg * len(items) / 60:.1f} 分钟")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
