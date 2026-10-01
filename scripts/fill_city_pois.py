"""按名单补城市看点坐标（数据源：高德关键词检索，落到 data/poi.db）。

名单 = 城市目录里 239 城的约 900 个「看点」。**1 个名字 1 次请求**，
不加 types 过滤（否则「中央大街」这种被高德标成「道路名」的点永远抓不到）。
查不到就记未命中（负缓存），不猜坐标。

用法：
    python scripts/fill_city_pois.py --dry-run        # 只看要查多少个名字
    python scripts/fill_city_pois.py --limit 30       # 试跑 30 个
    python scripts/fill_city_pois.py --delay 0.35     # 全量（约 900 次请求，几分钟）
    python scripts/fill_city_pois.py --report         # 只看当前覆盖率
    python scripts/fill_city_pois.py --export out.json
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

from travel_planner import poi  # noqa: E402

#: 子代理「看点名称校对」的产出位置（--apply-fixes 不给值时用它）
FIXES_DEFAULT = ROOT / "data" / "route_content" / "_staging" / "poi_fixes.json"


def report(sample: int = 12) -> int:
    conn = poi.connect()
    try:
        cov, st = poi.coverage(conn), poi.stats(conn)
        print(f"名单 {cov['targets']} 个看点（{cov['cities']} 城）"
              f"：命中 {cov['hit']}（{cov['rate']}%）· 未命中 {cov['miss']}"
              f" · 还没查 {cov['pending']}")
        print(f"有命中的城市 {cov['cities_with_hit']}/{cov['cities']}，"
              f"全城看点都补上的 {cov['full_cities']} 个")
        last = st.get("last_harvest") or {}
        if last:
            print(f"上次补查：{last.get('total')} 个，命中 {last.get('hit')}，"
                  f"未命中 {last.get('miss')}，失败 {last.get('failed')}")
        if sample:
            rows = poi.list_pois(conn, status="ok", limit=sample)["pois"]
            if rows:
                print("命中抽样（核对坐标是否落在城里）：")
                for r in rows:
                    print(f"  {r['city']} {r['name']} → {r['poi_name']} "
                          f"{r['lat']:.4f},{r['lon']:.4f} [{r['kind']}] "
                          f"{r['distance_km']}km ★{r['rating']}")
        misses = poi.list_pois(conn, status="miss", limit=sample)["pois"]
        if misses:
            print("未命中示例：" + "、".join(f"{m['city']}/{m['name']}" for m in misses))
        breakdown = poi.miss_breakdown(conn)
        if breakdown["mainland"] or breakdown["overseas_region"]:
            print(f"未命中分布：港澳台 {breakdown['overseas_region']} 条 · "
                  f"内地 {breakdown['mainland']} 条"
                  + ("（" + "、".join(f"{k} {v}"
                                     for k, v in breakdown["by_province"].items()) + "）"
                     if breakdown["by_province"] else ""))
        return 0
    finally:
        conn.close()


def explain(spec: str) -> int:
    """排查一个名字：把每个检索口径的结果与逐条判定打出来（为什么没收到，一眼看到）。"""
    city, _, name = spec.partition("/")
    target = next((t for t in poi.targets()
                   if t["city"] == city and t["name"] == name), None)
    if not target:
        print(f"✗ 名单里没有「{spec}」（写法是 城市/看点名）")
        return 1
    key = poi.amap_key()
    if not key:
        print("✗ 没有高德 key")
        return 1
    print(f"== {city} / {name}（市中心 {target['lat']},{target['lon']}）==")
    for variant in poi.query_variants(name):
        found = False
        for scope in (city, ""):
            try:
                pois = poi.fetch_amap(scope, variant, key=key)
            except Exception as exc:                      # noqa: BLE001
                print(f"  city={scope or '(全国)'} keywords={variant}：失败 {exc}")
                continue
            print(f"  city={scope or '(全国)'} keywords={variant}：返回 {len(pois)} 条")
            for item in pois[:5]:
                verdict = poi.judge(name, target, item)
                mark = "✓" if verdict["ok"] else "✗"
                detail = verdict.get("why") or verdict.get("reason") or ""
                score = verdict.get("score")
                print(f"    {mark} {item['name']} [{item['type_full'][:26]}] "
                      f"★{item['rating']}｜得分 {score if score is not None else '-'}｜{detail[:60]}")
                found = found or verdict["ok"]
            if found:
                break
        if found:
            break
    return 0


def recheck(delay: float = 0.35, db_path: str = "") -> int:
    """核查后定点重查：把「贴着市中心、真地方在远处」的那几条用**当前规则**重查一遍。

    判据与 `show_poi_check.py --suspects` 一致；重查后如果新坐标跟维基对上了（≤20 公里）
    就说明修好了；仍然对不上就**不动库**，列出来人工看 —— 因为维基自己也会有错坐标
    （实测：柳侯祠的维基经度写成了 108.41，实际在 109.41，差一个数字一条河）。
    """
    conn = poi.connect(db_path or None)
    try:
        total = conn.execute("SELECT COUNT(*) n FROM poi_check").fetchone()["n"]
        joined = conn.execute(
            "SELECT COUNT(*) n FROM poi_check k JOIN city_poi c "
            "ON c.city=k.city AND c.name=k.name WHERE k.km > 20").fetchone()["n"]
        rows = [dict(r) for r in conn.execute(
            "SELECT k.city,k.name,k.km,k.wiki_label,k.wiki_lat,k.wiki_lon,"
            "c.lon AS our_lon,c.lat AS our_lat,c.distance_km AS city_km "
            "FROM poi_check k JOIN city_poi c ON c.city=k.city AND c.name=k.name "
            "WHERE k.km > 20 AND c.distance_km IS NOT NULL AND c.distance_km < 8 "
            "ORDER BY k.km DESC")]
        print(f"（核对表 {total} 行；差 >20 公里的 {joined} 行；"
              f"其中「贴着市中心」的 {len(rows)} 行）", flush=True)
    finally:
        conn.close()
    # 同名撞车（维基那边是**别处**的同名条目，比如「老外滩」撞上上海的「外滩」）不算问题；
    # 注意名字完全相同不算撞车，只有「一边是另一边的真子串」才是。
    def _collision(row: dict) -> bool:
        label = row["wiki_label"] or ""
        if not label or label == row["name"]:
            return False
        return label in row["name"] or row["name"] in label

    suspects = [r for r in rows if not _collision(r)]
    if not suspects:
        print("没有需要重查的（先跑 verify_poi_coords.py）")
        return 0
    print(f"定点重查 {len(suspects)} 条：", flush=True)
    for row in suspects:
        print(f"  {row['km']:7.1f}km  {row['city']}/{row['name']}"
              f"（我们距市中心 {row['city_km']}km，维基「{row['wiki_label']}」）")
    out = poi.harvest(only_names=[(r["city"], r["name"]) for r in suspects],
                      delay=delay, db_path=db_path or None)
    if not out.get("ok"):
        print("✗ " + str(out.get("error")))
        return 1
    print(f"\n重查完成：命中 {out['hit']} · 未命中 {out['miss']} · 失败 {out['failed']}")
    conn = poi.connect(db_path or None)
    try:
        fixed, review = [], []
        for row in suspects:
            cur = conn.execute(
                "SELECT lon,lat,poi_name,status,source FROM city_poi WHERE city=? AND name=?",
                (row["city"], row["name"])).fetchone()
            if not cur or cur["lon"] is None:
                review.append((row, "重查后没拿到坐标", None))
                continue
            moved = poi._haversine_km((row["our_lon"], row["our_lat"]),
                                      (cur["lon"], cur["lat"]))
            gap = poi._haversine_km((cur["lon"], cur["lat"]),
                                    (row["wiki_lon"], row["wiki_lat"]))
            if gap <= 20:
                fixed.append((row, cur, moved, gap))
            else:
                review.append((row, f"仍与维基差 {gap:.0f} 公里（新点位 {cur['poi_name']}）",
                               (cur["lon"], cur["lat"], moved)))
        print(f"\n== 修好了 {len(fixed)} 条（重查后与维基对得上）==")
        for row, cur, moved, gap in fixed:
            print(f"  {row['city']}/{row['name']}：挪了 {moved:.1f} 公里 → "
                  f"{cur['lat']:.4f},{cur['lon']:.4f}「{cur['poi_name']}」"
                  f"（距维基 {gap:.1f} 公里）")
        print(f"\n== 仍需人工看 {len(review)} 条（没动库）==")
        for row, why, _ in review:
            print(f"  {row['city']}/{row['name']}：{why}"
                  f"｜我们 {row['our_lat']:.4f},{row['our_lon']:.4f}"
                  f"｜维基 {row['wiki_lat']:.4f},{row['wiki_lon']:.4f}"
                  f"（{row['wiki_label']}）")
        return report(6)
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="按名单补城市看点坐标")
    ap.add_argument("--limit", type=int, default=0, help="只查前 N 个（试跑用）")
    ap.add_argument("--delay", type=float, default=0.35, help="每次请求间隔秒数")
    ap.add_argument("--refresh", action="store_true", help="重查已落库的名字")
    ap.add_argument("--misses-only", action="store_true",
                    help="只重查上次判为未命中的（换过规则后补一遍用）")
    ap.add_argument("--revalidate", action="store_true",
                    help="用当前规则本地重审已落库的命中行（不联网），不达标的降级为未命中")
    ap.add_argument("--dry-run", action="store_true", help="只统计要查多少个，不联网")
    ap.add_argument("--report", action="store_true", help="只看覆盖率")
    ap.add_argument("--sample", type=int, default=12, help="报告里抽几行命中/未命中")
    ap.add_argument("--db", default="", help="覆盖 poi.db 路径（测试用）")
    ap.add_argument("--export", default="", help="把结果导出成 JSON")
    ap.add_argument("--explain", default="", metavar="城市/看点",
                    help="排查一个名字为什么没收到（会联网）")
    ap.add_argument("--apply-fixes", default="__default__", metavar="文件",
                    help="把「看点名称校对」的结果并进库（不给值就用 _staging/poi_fixes.json）")
    ap.add_argument("--recheck", action="store_true",
                    help="把维基核查出来的疑点用当前规则定点重查（先跑 verify_poi_coords.py）")
    args = ap.parse_args(argv)

    if args.explain:
        return explain(args.explain)
    if args.recheck:
        return recheck(delay=args.delay, db_path=args.db)

    if args.apply_fixes != "__default__":
        path = args.apply_fixes or str(FIXES_DEFAULT)
        if not Path(path).exists():
            print(f"✗ 找不到校对文件：{path}", file=sys.stderr)
            return 1
        conn = poi.connect(args.db or None)
        try:
            out = poi.apply_fixes(conn, path)
        finally:
            conn.close()
        print(f"并入校对结果 {out['total']} 条：写入坐标 {out['applied']}"
              f" · 仍无坐标 {out['no_coord']} · 跳过 {out['skipped']}")
        for item in out["no_coord_items"]:
            print(f"  · 无坐标：{item['city']}/{item['name']}"
                  + (f"（建议关键词「{item['query']}」）" if item["query"] else ""))
        return report(args.sample)

    if args.report:
        return report(args.sample)

    db_path = args.db or None
    if args.revalidate:
        conn = poi.connect(db_path)
        try:
            out = poi.revalidate(conn)
        finally:
            conn.close()
        print(f"重审 {out['checked']} 行，降级 {out['demoted']} 行")
        for item in out["samples"]:
            print(f"  ✗ {item['city']}/{item['name']} → {item['poi_name']}｜{item['reason'][:60]}")
        return report(args.sample)

    conn = poi.connect(db_path)
    try:
        targets = poi.targets()
        if args.misses_only:
            miss_keys = {(r["city"], r["name"]) for r in conn.execute(
                "SELECT city,name FROM city_poi WHERE status='miss'").fetchall()}
            todo = [t for t in targets if (t["city"], t["name"]) in miss_keys]
        else:
            known = poi.done_names(conn, refresh=args.refresh)
            todo = [t for t in targets if (t["city"], t["name"]) not in known]
        if args.limit:
            todo = todo[: args.limit]
        print(f"名单 {len(targets)} 个看点；本次要查 {len(todo)} 个"
              + ("（只补未命中）" if args.misses_only else ""))
        print(f"预计 {len(todo)} 次请求（{args.delay}s 间隔 → 约 "
              f"{len(todo) * (args.delay + 0.3) / 60:.1f} 分钟）")
        if args.dry_run:
            print("（--dry-run：不联网）")
            return 0
    finally:
        conn.close()

    start = time.time()
    done = {"n": 0}

    def progress(phase: str, index: int, total: int, extra: dict) -> None:
        if index % 25 == 0 or index == total:
            print(f"  {index}/{total} 命中 {extra.get('ok', 0)}"
                  f" · 未命中 {extra.get('miss', 0)} · 失败 {extra.get('failed', 0)}",
                  flush=True)

    out = poi.harvest(limit=args.limit, delay=args.delay, refresh=args.refresh,
                      only_miss=args.misses_only, db_path=db_path, progress_cb=progress)
    if not out.get("ok"):
        print("✗ " + str(out.get("error")), file=sys.stderr)
        return 1
    print(f"✓ 查了 {out['total']} 个：命中 {out['hit']} · 未命中 {out['miss']} "
          f"· 失败 {out['failed']}（耗时 {time.time() - start:.0f}s）")
    if out.get("misses"):
        print("  未命中示例：" + "、".join(f"{m['city']}/{m['name']}"
                                        for m in out["misses"][:10]))
    if out.get("failures"):
        print("  失败示例：" + "；".join(f"{f['city']}/{f['name']} {f['error']}"
                                       for f in out["failures"][:5]), file=sys.stderr)
    if args.export:
        conn = poi.connect(db_path)
        try:
            Path(args.export).write_text(poi.export_json(conn), encoding="utf-8")
            print(f"✓ 已导出 {args.export}")
        finally:
            conn.close()
    return report(args.sample)


if __name__ == "__main__":
    raise SystemExit(main())
