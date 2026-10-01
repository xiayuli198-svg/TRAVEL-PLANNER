"""给一批城市建/补 POI 池（城内动线要对齐散文里的地名，池子越大对得越全）。

每个城市 4 次请求（风景名胜 / 科教文化 / 购物服务 / 体育休闲 各 1 页，25 条/页）。

用法：
    python scripts/fill_city_pools.py 北京 上海 西安          # 建池子
    python scripts/fill_city_pools.py 北京 --pages 2 --refresh
    python scripts/fill_city_pools.py --report 北京           # 只看池子有多大、能对上几天
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from travel_planner import guide  # noqa: E402


def report(cities: list[str]) -> int:
    conn = guide.connect()
    try:
        for city in cities:
            data = guide.city_days(conn, city)
            if not data.get("ok"):
                print(f"  {city}: {data.get('error')}")
                continue
            drawable = [d for d in data["days"] if d["drawable"]]
            per_day = "/".join(str(d["matched"]) for d in data["days"])
            print(f"  {city}: 池子 {data['pool']} 个点位 · {len(data['days'])} 天动线 "
                  f"（可画 {len(drawable)} 天）· 每天对上 {per_day}")
        return 0
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="给城市建 POI 池（城内动线用）")
    ap.add_argument("cities", nargs="*", help="城市名（可多个）")
    ap.add_argument("--pages", type=int, default=1, help="每个类型抓几页（默认 1）")
    ap.add_argument("--refresh", action="store_true", help="重抓（默认已有就跳过）")
    ap.add_argument("--delay", type=float, default=0.35)
    ap.add_argument("--report", action="store_true", help="只看池子与对齐情况，不联网")
    args = ap.parse_args(argv)
    if not args.cities:
        print("给几个城市名：python scripts/fill_city_pools.py 北京 上海")
        return 1
    if args.report:
        return report(args.cities)

    conn = guide.connect()
    start = time.time()
    try:
        for city in args.cities:
            out = guide.ensure_pool(conn, city, pages=args.pages, delay=args.delay,
                                    refresh=args.refresh)
            if not out.get("ok"):
                print(f"  ✗ {city}: {out.get('error')}")
                continue
            print(f"  ✓ {city}: {out.get('note')}"
                  + (f"（失败 {len(out['failed'])} 次请求）" if out.get("failed") else ""),
                  flush=True)
            time.sleep(args.delay)
    finally:
        conn.close()
    print(f"耗时 {time.time() - start:.0f}s")
    return report(args.cities)


if __name__ == "__main__":
    raise SystemExit(main())
