"""导入 HerbertHe/cr-12306-train-info 发布的全国车次详情数据集。

用法: python scripts/import_dataset.py [json路径] [日期YYYY-MM-DD]
默认: data/import/train_detail_20260617.json, 日期 2026-06-17

注意：数据集停站的天偏移(arrive_day_diff)相对各车次始发日，导入时假设
车次从数据日期当天始发；对「前一日始发、当日途经」的过夜直通车会整体
偏移一天（少数车次），正式出行请以 crawl-date 爬取的当天数据为准。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from travel_planner import db  # noqa: E402


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path("data/import/train_detail_20260617.json")
    date = sys.argv[2] if len(sys.argv) > 2 else "2026-06-17"
    if not path.exists():
        print(f"找不到 {path}，请先从 HerbertHe/cr-12306-train-info Releases 下载")
        return

    conn = db.connect()
    name2code = {r["name"]: r["code"]
                 for r in conn.execute("SELECT name, code FROM stations")}

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    rows = []
    n_trains = 0
    n_dropped_stops = 0
    for t in data:
        tn = t.get("train_no")
        tc = t.get("station_train_codes") or t.get("station_train_code") or ""
        stops = t.get("data") or []
        if not tn or not stops:
            continue
        out = []
        for i, s in enumerate(stops, start=1):
            name = (s.get("station_name") or "").strip()
            code = name2code.get(name)
            if not code:
                n_dropped_stops += 1
                continue
            arr = s.get("arrive_time") or "--"
            dep = s.get("start_time") or "--"
            if arr == "----":
                arr = "--"
            if dep == "----":
                dep = "--"
            try:
                day = int(s.get("arrive_day_diff") or 0)
            except ValueError:
                day = 0
            out.append((tn, tc, date, i, code, name, day, arr, dep))
        if not out:
            continue
        rows.extend(out)
        n_trains += 1

    with conn:
        conn.execute("DELETE FROM schedules WHERE date=?", (date,))
        conn.executemany(
            "INSERT INTO schedules(train_no,train_code,date,seq,station_code,"
            "station_name,day,arr,dep) VALUES(?,?,?,?,?,?,?,?,?)", rows)
    db.set_meta(conn, f"import:{date}",
                f"{path.name}: {n_trains} 车次 {len(rows)} 停站，"
                f"丢弃未匹配站名 {n_dropped_stops}")
    print(f"导入完成: {n_trains} 车次 / {len(rows)} 停站，"
          f"丢弃未匹配站名 {n_dropped_stops}")


if __name__ == "__main__":
    main()
