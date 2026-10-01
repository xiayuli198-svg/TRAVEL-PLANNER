"""RAPTOR 引擎性能基准：把现有车次复制放大到全国规模（~1.5 万车次）测查询耗时。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from travel_planner import db  # noqa: E402
from travel_planner.engine import raptor  # noqa: E402


def main() -> None:
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-09-09"
    scale = int(sys.argv[2]) if len(sys.argv) > 2 else 15000
    conn = db.connect()
    trips = db.load_trips(conn, date)
    if not trips:
        print(f"{date} 无数据，请先 crawl-date")
        return
    print(f"原始车次: {len(trips)}，放大到 {scale} 车次")

    big = []
    for i in range(scale):
        big.append(trips[i % len(trips)])
    stations = {s for t in big for s in t.stations}
    footpaths = db.build_footpaths(conn, stations)

    ods = [(t.stations[0], t.stations[-1]) for t in big[:3]]
    for frm, to in ods:
        t0 = time.perf_counter()
        js = raptor.plan(big, footpaths, [frm], [to], 360, max_transfers=3)
        dt_ms = (time.perf_counter() - t0) * 1000
        best = js[0] if js else None
        print(f"  {frm}->{to}: {len(js)} 个方案, "
              f"{dt_ms:.0f} ms" +
              (f", 最优 {best.legs[0].train_code} {best.arr_min}min" if best else ""))


if __name__ == "__main__":
    main()
