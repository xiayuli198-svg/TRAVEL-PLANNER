"""开发小工具：看一眼 poi_check 表（坐标核对结果），按需要过滤。

用法：
    python scripts/show_poi_check.py                  # 汇总 + 按城市
    python scripts/show_poi_check.py 上海 东营         # 只看这些城
    python scripts/show_poi_check.py --verdict wrong   # 只看某一类判定
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


#: 范围型地名：维基给的常是几何中心，我们给的是入口/镇子 —— 差几十公里不算错
AREA_WORDS = ("沙漠", "草原", "湖", "措", "海", "山", "峰", "岭", "谷", "峡", "淀", "岛",
              "湿地", "公园", "景区", "风景区", "保护区", "冰川", "森林", "梯田", "滩",
              "湾", "群岛", "河", "江", "瀑布", "温泉", "天池", "盐湖", "雅丹", "地貌")
#: 点状地名：差几公里就说明有一边错了
POINT_WORDS = ("镇", "村", "祠", "楼", "馆", "宫", "寺", "塔", "桥", "街", "巷", "门",
               "古镇", "古城", "故居", "陵", "墓", "发射中心", "滑雪场", "车站", "机场",
               "广场", "纪念碑", "博物馆", "博物院", "遗址")


def triage(conn) -> int:
    """把可疑/错误分成三类，判断哪些真该改。"""
    rows = [dict(r) for r in conn.execute(
        "SELECT k.*, c.distance_km AS city_km, c.source AS our_source "
        "FROM poi_check k JOIN city_poi c ON c.city=k.city AND c.name=k.name "
        "WHERE k.verdict IN ('suspect','wrong') ORDER BY k.km DESC")]
    collision, area, real = [], [], []
    for row in rows:
        name = row["name"]
        label = row["wiki_label"] or ""
        # 同名撞车：维基条目名和我们的名字不是一回事（「老外滩」撞上「外滩」）
        norm_ours, norm_wiki = name, label
        if norm_wiki and norm_wiki not in norm_ours and norm_ours not in norm_wiki:
            collision.append(row)
        elif norm_ours != norm_wiki and (norm_wiki in norm_ours or norm_ours in norm_wiki):
            # 一边包含另一边（老外滩→外滩、台北故宫博物院→故宫博物院）：按撞车处理
            if len(norm_wiki) < len(norm_ours) and norm_wiki in norm_ours:
                collision.append(row)
            elif any(w in name for w in AREA_WORDS):
                area.append(row)
            else:
                real.append(row)
        elif any(w in name for w in AREA_WORDS):
            area.append(row)
        elif any(w in name for w in POINT_WORDS):
            real.append(row)
        else:
            area.append(row)

    def dump(title: str, items: list, note: str) -> None:
        print(f"\n== {title}（{len(items)} 条）== {note}")
        for row in items[:40]:
            print(f"  {row['km']:8.1f}km  {row['city']}/{row['name']}"
                  f"  → 维基「{row['wiki_label']}」"
                  f"  · 我们距市中心 {row['city_km'] if row['city_km'] is not None else '?'}km")

    dump("同名撞车（维基匹配到别处的同名条目，我们多半没错）", collision,
         "需要逐条看，不要改")
    dump("范围型（沙漠/湖/山…维基给的是几何中心）", area, "差几十公里属正常，不要改")
    dump("疑似真错（点状地名却离维基很远）", real, "建议按维基/公开资料修正")
    return 0


def suspects(conn, *, near_city_km: float = 8.0, far_wiki_km: float = 20.0) -> int:
    """真正的疑似错误：**我们的点紧贴市中心，而维基标的那个地方在几十公里外**。

    这是自动挑点最典型的翻车方式 —— 高德按城市搜的时候返回了城里同名的东西
    （湖景房、广场、道路），我们照单收了。反过来，市中心本身就有景点（嘉兴南湖）
    会被维基的**同名别处条目**撞车，用「维基标题带括号消歧义」把它排除掉。
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT k.city,k.name,k.km,k.wiki_label,k.wiki_lat,k.wiki_lon,"
        "c.lon AS our_lon,c.lat AS our_lat,c.distance_km AS city_km,c.source AS our_source "
        "FROM poi_check k JOIN city_poi c ON c.city=k.city AND c.name=k.name "
        "WHERE k.km > ? AND c.distance_km IS NOT NULL AND c.distance_km < ? "
        "ORDER BY k.km DESC", (far_wiki_km, near_city_km))]
    real = [r for r in rows if "(" not in (r["wiki_label"] or "")
            and "（" not in (r["wiki_label"] or "")]
    print(f"「贴着市中心、真地方在远处」共 {len(real)} 条（另有 {len(rows) - len(real)} 条是维基同名撞车）：")
    for row in real:
        print(f"  {row['km']:7.1f}km  {row['city']}/{row['name']}"
              f"  我们 {row['our_lat']:.4f},{row['our_lon']:.4f}（距市中心 {row['city_km']}km）"
              f"  → 维基「{row['wiki_label']}」{row['wiki_lat']:.4f},{row['wiki_lon']:.4f}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="查看坐标核对结果")
    ap.add_argument("cities", nargs="*", default=[])
    ap.add_argument("--verdict", default="")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--triage", action="store_true",
                    help="把可疑/错误的挑出来分三类（同名撞车 / 范围型 / 疑似真错）")
    ap.add_argument("--suspects", action="store_true",
                    help="只看「贴着市中心、真地方在远处」的那种真错")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(ROOT / "data" / "poi.db")
    conn.row_factory = sqlite3.Row
    if args.suspects:
        return suspects(conn)
    if args.triage:
        return triage(conn)
    where, params = [], []
    if args.cities:
        where.append("city IN (%s)" % ",".join("?" * len(args.cities)))
        params += list(args.cities)
    if args.verdict:
        where.append("verdict = ?")
        params.append(args.verdict)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(f"SELECT * FROM poi_check {clause} ORDER BY city, name LIMIT ?",
                        (*params, args.limit)).fetchall()
    for row in rows:
        km = f"{row['km']:.1f}km" if row["km"] is not None else "   -  "
        print(f"{row['city']:6s} {row['name'][:18]:18s} {row['verdict']:7s} {km:>8s} "
              f"{(row['wiki_label'] or '')[:20]:20s} {row['note'][:44]}")
    counts: dict = {}
    for row in conn.execute("SELECT verdict, COUNT(*) n FROM poi_check GROUP BY verdict"):
        counts[row["verdict"]] = row["n"]
    print("\n总计：" + " · ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
