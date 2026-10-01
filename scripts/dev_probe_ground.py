"""开发探针：路线库里有多少条目「去掉没有铁路站的景区/小镇落脚点后」还能排出行程。

背景：166 条素材里有 79 条的分段端点写的是景区/小镇（篁岭、黑马河、亚龙湾…），
规划引擎要求每一站都有铁路站，于是这些条目排不出行程。
这个探针只统计「剩下的铁路锚点有几个」——用来决定值不值得做
「铁路 + 地面接驳段」的混合行程（不去动任何数据）。
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from travel_planner import concrete, db, routes as routes_mod, trips  # noqa: E402


def main() -> int:
    conn = db.connect()
    store = trips.connect()
    known = concrete._city_index(conn)
    rows = {r["route_id"]: r for r in trips.skips(store, limit=500)}
    buckets = Counter()
    examples: dict[str, list[str]] = {}
    for item in routes_mod.query():
        if item["id"] not in rows:
            continue
        reason = rows[item["id"]]["reason"]
        if "只涉及一个城市" in reason:
            buckets["同城条目（本来就不该排城际）"] += 1
            continue
        stops = concrete.route_stops(conn, item)
        anchors = [s for s in stops if s in known]
        ground = [s for s in stops if s not in known]
        key = (f"锚点 {len(anchors)} 个" if len(anchors) < 3 else "锚点 ≥3 个")
        buckets[key] += 1
        examples.setdefault(key, []).append(
            f"{item['id']}｜锚点 {'→'.join(anchors) or '（无）'}｜地面 {len(ground)} 个：" + "、".join(ground[:3]))
    print("== 排不出的 91 条按「还剩几个铁路锚点」分组 ==")
    for key, count in buckets.most_common():
        print(f"  {count:3d}  {key}")
        for line in examples.get(key, [])[:3]:
            print("        ", line)
    print()
    print("== 全部 166 条：铁路锚点数量分布 ==")
    dist = Counter()
    for item in routes_mod.query():
        stops = concrete.route_stops(conn, item)
        dist[len([s for s in stops if s in known])] += 1
    for count, n in sorted(dist.items()):
        print(f"  锚点 {count} 个：{n} 条")
    store.close()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
