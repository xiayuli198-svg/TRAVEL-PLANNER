"""全国级换乘验证：对必须换乘的 OD 对输出方案，人工核对车次/时刻合理性。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from travel_planner import db  # noqa: E402
from travel_planner.engine import raptor  # noqa: E402

DEFAULT_PAIRS = [
    ("丽江", "上海"),        # 需经昆明/贵阳等枢纽换乘
    ("齐齐哈尔", "厦门"),     # 东北→东南，长距离多段
    ("大理", "青岛"),        # 西南→山东
]


def fmt_min(m: int) -> str:
    return f"第{m // 1440 + 1}天 {m % 1440 // 60:02d}:{m % 60:02d}"


def main() -> None:
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-09-09"
    conn = db.connect()
    trips = db.load_trips(conn, date)
    if not trips:
        print(f"{date} 无数据，请先 crawl-date")
        return
    names = {r["code"]: r["name"] for r in conn.execute("SELECT code,name FROM stations")}
    footpaths = db.build_footpaths(conn, {s for t in trips for s in t.stations})
    print(f"{date} 时刻表规模: {len(trips)} 车次")

    for frm_q, to_q in DEFAULT_PAIRS:
        frm = db.resolve_station(conn, frm_q)
        to = db.resolve_station(conn, to_q)
        if not frm or not to:
            print(f"!! 车站未找到: {frm_q} -> {to_q}")
            continue
        js = raptor.plan(trips, footpaths, [frm[0]["code"]], [to[0]["code"]], 360,
                         max_transfers=3)
        js.sort(key=lambda j: (j.arr_min, j.transfers, j.duration))
        print(f"\n{frm[0]['name']} -> {to[0]['name']}（最多换乘 3 次，06:00 后出发）")
        if not js:
            print("  无方案（该日数据未覆盖）")
            continue
        for j in js[:3]:
            legs_desc = " → ".join(
                f"{l.train_code} {names.get(l.from_station, l.from_station)}"
                f"{fmt_min(l.dep_min)}→{names.get(l.to_station, l.to_station)}{fmt_min(l.arr_min)}"
                for l in j.legs)
            print(f"  [{len(j.legs)}段/{j.transfers}换] {legs_desc}")


if __name__ == "__main__":
    main()
