"""省钱/快 vs 价格展示验证脚本：北京→上海 混合模式 + 昆明→上海 快/省对比。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from travel_planner import db, service  # noqa: E402


def show_plan(conn, label, **kw):
    res = service.do_plan(conn, kw["frm"], kw["to"], kw["date"], time=kw.get("time", "08:00"),
                          mode=kw.get("mode", "rail"), objective=kw.get("objective", "fast"),
                          slack_hours=kw.get("slack", 2), top=kw.get("top", 3),
                          guide=False, links=False)
    print(f"\n=== {label} ===")
    if not res["ok"]:
        print(" 失败:", res["error"])
        return
    for n in res.get("notes", []):
        print(" note:", n)
    for j in res["journeys"]:
        parts = [f"{l['kind'][0].upper()} {l['code']} {l['from']}→{l['to']}" for l in j["legs"]]
        print(f"  [{j['price_text']}] 到 {j['arr_cal']} {j['duration_text']} | {' + '.join(parts)}")


def main() -> None:
    conn = db.connect()
    # 强制重取北京-上海 2026-09-09 航班（旧缓存无票价）
    conn.execute("DELETE FROM flight_route_cache WHERE origin='北京' "
                 "AND destination='上海' AND date='2026-09-09'")
    conn.commit()
    show_plan(conn, "北京→上海 mixed 最快", frm="北京", to="上海",
              date="2026-09-09", time="07:00", mode="mixed")
    show_plan(conn, "昆明→上海 rail 最快", frm="昆明", to="上海",
              date="2026-06-17", mode="rail")
    show_plan(conn, "昆明→上海 rail 限时最省(≤4h)", frm="昆明", to="上海",
              date="2026-06-17", mode="rail", objective="cheap", slack=4)


if __name__ == "__main__":
    main()
