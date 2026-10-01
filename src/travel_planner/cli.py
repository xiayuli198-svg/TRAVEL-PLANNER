"""命令行入口：init / sync-stations / stations / crawl-date / plan / loop / web 数据操作。

规划逻辑位于 service 层（与 Web API 共用），本文件只负责参数解析与打印。
"""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import crawl_progress, db, od_focus, service
from .ingest import crawl, stations as stations_ingest
from .ingest.client12306 import Client12306, DEFAULT_DELAY


def cmd_init(args) -> int:
    conn = db.connect(args.db)
    print(f"数据库已就绪: {db.default_db_path()}")
    conn.close()
    return 0


def cmd_sync_stations(args) -> int:
    conn = db.connect(args.db)
    client = Client12306()
    rows = stations_ingest.fetch_station_rows(client)
    db.upsert_stations(conn, rows)
    db.set_meta(conn, "stations:count", str(len(rows)))
    print(f"车站已同步: {len(rows)} 个")
    conn.close()
    return 0


def cmd_stations(args) -> int:
    conn = db.connect(args.db)
    rows = db.resolve_station(conn, args.q)
    if not rows:
        print("未找到匹配车站（请先运行 sync-stations）")
    for r in rows:
        print(f"  {r['code']}\t{r['name']}\t{r['city']}")
    conn.close()
    return 0 if rows else 1


def cmd_crawl_date(args) -> int:
    conn = db.connect(args.db)
    ods = None
    if args.od:
        ods = []
        for spec in args.od:
            a, _, b = spec.partition(":")
            if not a or not b:
                print(f"--od 格式应为 FROM:TO，收到 {spec!r}")
                return 1
            ods.append((a, b))
    elif getattr(args, "focus", None):
        # 定向爬取：只爬目标城市相关的 OD（默认 ~320 对，全量是 6006 对）
        info = od_focus.focused_pairs_for_plan(conn, args.focus,
                                               max_pairs=args.focus_pairs)
        print(od_focus.describe_focus(info))
        if getattr(args, "dry_run", False):
            for a, b in info["pairs"][:20]:
                print(f"  {a} -> {b}")
            if len(info["pairs"]) > 20:
                print(f"  ... 共 {len(info['pairs'])} 对")
            conn.close()
            return 0
        ods = info["pairs"]
        if not ods:
            print(info.get("error") or "没有生成任何 OD 对")
            conn.close()
            return 1
    if args.resume:
        before = crawl_progress.summary(conn, args.date)
        if before.get("planned"):
            print(f"续爬：上次 {before['done']}/{before['planned']} 对已完成，"
                  f"这次只补剩下的 {before['remaining']} 对"
                  + (f"（{before['failed']} 对失败会重试）" if before.get("failed") else ""))
    stats = crawl.crawl_date(conn, args.date, ods=ods,
                             pairs_limit=args.pairs_limit, delay=args.delay,
                             resume=args.resume)
    print("部分完成，可续爬:" if stats.get("state") == "partial" else "爬取结束:", stats)
    after = crawl_progress.summary(conn, args.date)
    print(crawl_progress.note_for(after) or "进度：无记录")
    conn.close()
    return 0


def cmd_crawl_progress(args) -> int:
    """看某日爬取进度（断电/关窗口之后照样查得到：账本在数据库里）。"""
    conn = db.connect(args.db)
    data = crawl_progress.summary(conn, args.date)
    legacy = crawl_progress.legacy_info(args.date)
    if not data["planned"] and not legacy:
        print(f"{args.date} 没有爬取记录")
    else:
        if data["planned"]:
            print(f"{args.date}：{data['done']}/{data['planned']} 对已完成，剩余 {data['remaining']}"
                  f"，失败 {data['failed']}，进行中 {data['running']} · 状态 {data['state'] or '未开始'}")
            print(f"  已发现车次 {data['discovered']} · 已入库 {data['ingested']}"
                  f" · 最后更新 {data['updated_at'] or '-'}")
            print(f"  {crawl_progress.note_for(data)}")
            if args.rows:
                for r in crawl_progress.progress_rows(conn, args.date, limit=args.rows):
                    print(f"  {r['od']:>10} {r['status']:<8} trains={r['trains']} "
                          f"tries={r['tries']} {r['updated_at']}")
        if legacy:
            print("  " + legacy["text"])
    conn.close()
    return 0


def cmd_copy_date(args) -> int:
    conn = db.connect(args.db)
    try:
        res = service.copy_dates(conn, args.from_date, args.to_date,
                                 overwrite=args.overwrite,
                                 dry_run=args.dry_run)
        if not res["ok"]:
            print(res["error"])
            return 1
        for item in res["results"]:
            label = {"copied": "已复制", "skipped": "已跳过",
                     "would_copy": "将复制"}.get(item["status"], item["status"])
            reason = f"（{item['reason']}）" if item.get("reason") else ""
            print(f"{item['date']}：{label} {item.get('rows', 0)} 行{reason}")
        print(res["note"])
        return 0
    finally:
        conn.close()


def cmd_set_amap_key(args) -> int:
    conn = db.connect(args.db)
    with conn:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('amap_key',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (args.key,))
    print("高德 key 已保存（存于 meta 表；也可用环境变量 AMAP_KEY 提供）")
    conn.close()
    return 0


def _print_journey_block(jd: dict, indent: str = "  ") -> None:
    stats = f"{jd['rides']} 趟 · 站内换乘 {jd['station_transfers']} 次 · 转场 {jd['city_transfers']} 次"
    if jd.get("price_text"):
        stats += f" · {jd['price_text']}"
    print(f"{indent}{stats}  "
          f"出发 {jd.get('dep_cal', jd['dep_text'])}  到达 {jd.get('arr_cal', jd['arr_text'])}  "
          f"全程 {jd['duration_text']}")
    for k, d in enumerate(jd["legs"], start=1):
        mark = "✈" if d["kind"] == "flight" else ""
        if d["kind"] != "transfer":
            airline = f" {d['airline']}" if d.get("airline") else ""
            print(f"{indent}  {k}. {mark}{d['code']:6s}{airline} {d['from']} {d['dep_text']}  ->  "
                  f"{d['to']} {d['arr_text']}")
            if d.get("wait"):
                nxt_name = jd["legs"][k]["from"] if k < len(jd["legs"]) else ""
                print(f"{indent}     └─ {nxt_name} 换乘等待 {d['wait']}")
                if d.get("transfer_guide"):
                    print(f"{indent}     🚇 中转: {d['transfer_guide']}")
        else:
            if d.get("guide"):
                print(f"{indent}  {k}. 🚇 中转: {d['guide']}")
            else:
                print(f"{indent}  {k}. 同城接驳（约 {d['duration_text']}）  "
                      f"{d['from']} {d['dep_text']}  ->  {d['to']} {d['arr_text']}")
        if d.get("link"):
            print(f"{indent}     购票: {d['link']}")


def cmd_plan(args) -> int:
    conn = db.connect(args.db)
    if conn.execute("SELECT COUNT(*) c FROM stations").fetchone()["c"] == 0:
        print("车站表为空，请先运行: python -m travel_planner.cli sync-stations")
        return 1

    res = service.do_plan(conn, args.from_, args.to, args.date,
                          time=args.time, max_transfers=args.max_transfers,
                          mode=args.mode, top=args.top,
                          links=args.links, guide=not args.no_guide,
                          buffer_min=args.buffer,
                          objective=args.objective, slack_hours=args.slack,
                          green_hsr_fare=args.green_fare,
                          tour_stay=args.tour_stay, with_flights=args.with_flights,
                          max_days=args.max_days or None,
                          tour_city_count=args.tour_city_count,
                          rest_days=args.rest_days)
    if not res["ok"]:
        print(res["error"])
        conn.close()
        return 1
    for n in res["notes"]:
        print(f"[{args.mode}] {n}")

    if res.get("tour"):
        t = res["tour"]
        print(f"\n{res['frm_label']} -> {res['to_label']}  单程游览（{args.date} {args.time} 后出发）")
        print(f"串城链: {' → '.join(t['chain'])}")
        print(f"绿皮直达基线 ¥{int(t['baseline'])} · 3倍闸门 ¥{int(t['gate'])}"
              + (f" · 本次全程 {t['total_price'] and '约¥' + str(int(t['total_price']))}")
              + (" · 含飞机" if t["with_flights"] else " · 纯绿皮"))
        print(f"每站停留: {args.tour_stay}，换乘不限（工程上限 {service.TOUR_RIDES_CAP} 趟）")
        for w in t["warnings"]:
            print(f"⚠ {w}")
        for rec in t["legs"]:
            tail = f"    停留: {rec['stay']}" if rec["stay"] is not None else ""
            print(f"\nD{rec['day_no']}  {rec['date']}  {rec['frm']} → {rec['to']}{tail}")
            if rec["journey"] is None:
                print(f"  !! {rec['error']}")
                continue
            if rec.get("city_tourism"):
                info = rec["city_tourism"]
                score = info.get("recommendation_score")
                print(f"  城市推荐: {score}/10" if score is not None else "  城市推荐: 暂无评分")
                if info.get("intro"):
                    print(f"  {info['intro']}")
                if info.get("halfday_plan"):
                    print(f"  半日建议: {info['halfday_plan']}")
            _print_journey_block(rec["journey"], indent="  ")
        if t["total_days"]:
            print(f"\n共 {t['total_days']} 天")
        conn.close()
        return 0

    print(f"\n{res['frm_label']} -> {res['to_label']}  {args.date}  "
          f"{args.time} 后出发  最多换乘 {args.max_transfers} 次"
          + (f"  目标: {res['objective_note']}" if res.get("objective_note") else ""))
    if not res["journeys"]:
        print("未找到可行方案：" + (res.get("diagnosis") or "该日数据未覆盖此 OD，或换乘次数上限过低"))
        conn.close()
        return 1
    for i, jd in enumerate(res["journeys"], start=1):
        print(f"\n方案 {i}")
        _print_journey_block(jd, indent="  ")
    conn.close()
    return 0


def cmd_loop(args) -> int:
    conn = db.connect(args.db)
    if conn.execute("SELECT COUNT(*) c FROM stations").fetchone()["c"] == 0:
        print("车站表为空，请先运行: python -m travel_planner.cli sync-stations")
        return 1

    stays = None
    if args.stays:
        try:
            stays = [int(x) for x in args.stays.split(",")]
        except ValueError:
            print("--stays 格式错误（逗号分隔整数）")
            return 1

    res = service.do_loop(conn, args.date, args.stops, stays,
                          free_order=args.free_order, time=args.time,
                          max_transfers=args.max_transfers, mode=args.mode,
                          links=args.links, guide=not args.no_guide,
                          buffer_min=args.buffer,
                          objective=args.objective, slack_hours=args.slack,
                          max_days=args.max_days or None,
                          green_hsr_fare=args.green_fare,
                          tour_city_count=args.tour_city_count,
                          rest_days=args.rest_days)
    if not res["ok"]:
        print(res["error"])
        conn.close()
        return 1

    if res.get("free_summary"):
        fs = res["free_summary"]
        print(f"自由顺序寻优（基准日 {res['date']}，{args.time} 后出发）：")
        print(f"  最优顺序: {' → '.join(fs['order'])}")
        if fs.get("saved_delta_min") is not None:
            if fs["saved_delta_min"] < 0:
                print(f"  相比输入顺序（{' → '.join(fs['input_order'])}）"
                      f"节省 {service.fmt_dur(-fs['saved_delta_min'])}")
            else:
                print("  输入顺序同样最优")
        print(f"  注意：{fs['note']}")
    if args.objective == "cheap":
        print(f"每段目标：限时内最省（允许比每段最快方案晚 {args.slack} 小时）")

    print(f"\n行程总览  {res['date']} 出发"
          + (f"（自由顺序寻优：{' → '.join(res['order'])}）" if res["order"] else ""))
    for rec in res["legs"]:
        tail = f"    停留: {rec['stay']}" if rec["stay"] is not None else ""
        print(f"\nD{rec['day_no']}  {rec['date']}  {rec['frm']} → {rec['to']}{tail}")
        if rec["journey"] is None:
            print(f"  !! {rec['error']}")
            continue
        if rec.get("city_tourism"):
            info = rec["city_tourism"]
            score = info.get("recommendation_score")
            print(f"  城市推荐: {score}/10" if score is not None else "  城市推荐: 暂无评分")
            if info.get("intro"):
                print(f"  {info['intro']}")
            if info.get("halfday_plan"):
                print(f"  半日建议: {info['halfday_plan']}")
        _print_journey_block(rec["journey"], indent="  ")
    if res["total_days"]:
        print(f"\n共 {res['total_days']} 天")
    if res.get("budget"):
        b = res["budget"]
        print(f"⚠ 全程 {res['total_days']} 天，超出预算（≤{b['max_days']} 天）{b['over_by_days']} 天")
        for s in b["suggestions"]:
            print(f"  删掉「{s['remove']}」→ 全程 {s['days']} 天")
    for w in res["warnings"]:
        print(f"\n警告：{w}")
    conn.close()
    return 0


def _print_city_line(item: dict, indent: str = "  ") -> None:
    rail = "" if item.get("rail", True) else "（无铁路，需飞机/包车）"
    alias = item.get("plan_name")
    alias_txt = f"  规划点: {alias}" if alias and alias != item["name"] else ""
    print(f"{indent}{item['name']}（{item.get('province') or item.get('region')}）"
          f"  推荐 {item.get('score')}/10 · 建议 {item.get('hours')} 小时"
          f"/{item.get('days')} 天{rail}{alias_txt}")
    if item.get("intro"):
        print(f"{indent}  {item['intro']}")
    if item.get("halfday"):
        print(f"{indent}  半日: {item['halfday']}")
    if item.get("oneday"):
        print(f"{indent}  一日: {item['oneday']}")
    names = "、".join(h["name"] for h in item.get("highlights") or [])
    if names:
        print(f"{indent}  看点: {names}")
    if item.get("note"):
        print(f"{indent}  提示: {item['note']}")


def cmd_roam(args) -> int:
    from . import catalog, drive as drive_mod, roam as roam_mod

    if args.regions or args.region:
        if args.regions and not args.region:
            print("大区（地图漫游的入口，每个大区下是可选城市）：")
            for region in catalog.regions():
                cities = catalog.cities(region["id"])
                print(f"  {region['name']}（{len(cities)} 城） {region.get('subtitle') or ''}")
                print(f"    {region.get('blurb') or ''}")
                print(f"    代表城市: {'、'.join(c['name'] for c in cities[:8])}")
            print("\n用 --region 名称 看某区全部城市，例如："
                  "python -m travel_planner.cli roam --region 江南")
            return 0
        items = catalog.cities(args.region)
        if not items:
            print(f"未找到大区或城市：{args.region}")
            return 1
        region = catalog.region_by(args.region)
        if region:
            print(f"{region['name']}（{len(items)} 城） {region.get('subtitle') or ''}")
            print(f"  {region.get('blurb') or ''}\n")
        for item in items:
            _print_city_line(item)
        return 0

    if args.loops or args.loop:
        if args.loops and not args.loop:
            print("经典环线（在地图上点一下就飞过去，可直接转成行程）：")
            for item in catalog.loops():
                cross = "跨大区" if item.get("cross_region") else ""
                ref = "参考线路" if item.get("plan_mode") == "reference" else ""
                tag = " · ".join(x for x in (cross, ref) if x)
                print(f"  {item['name']}  {' → '.join(item['names'])}")
                print(f"    {item.get('days')} 天 · {item.get('season') or ''}"
                      f"{' · ' + tag if tag else ''}")
                print(f"    {item.get('blurb') or ''}")
            print("\n用 --loop 名称 看详情，例如：python -m travel_planner.cli roam --loop 河西走廊环线")
            return 0
        item = catalog.loop(args.loop)
        if not item:
            print(f"未找到环线：{args.loop}")
            return 1
        plan_mode = ("参考线路（无 12306 车次，仅作路线参考）"
                     if item.get("plan_mode") == "reference" else "可直接排乘车表")
        print(f"{item['name']}  {' → '.join(item['names'])}")
        print(f"  建议 {item.get('days')} 天 · 最佳季节 {item.get('season') or '全年'} · {plan_mode}")
        print(f"  {item.get('blurb') or ''}")
        for tip in item.get("tips") or []:
            print(f"  · {tip}")
        print()
        for name in item["names"]:
            city = catalog.city(name)
            if city:
                _print_city_line(city)
        if item.get("missing"):
            print(f"  ! 目录中缺少：{'、'.join(item['missing'])}")
        return 0

    if not args.date or not args.cities:
        print("用法：roam DATE 城市1 城市2 [城市3 …]（或用 --regions / --region / --loops / --loop 浏览）")
        return 1

    conn = db.connect(args.db)
    if conn.execute("SELECT COUNT(*) c FROM stations").fetchone()["c"] == 0:
        print("车站表为空，请先运行: python -m travel_planner.cli sync-stations")
        conn.close()
        return 1

    cities = list(args.cities)
    if args.loop and not cities:
        preset = catalog.loop(args.loop)
        if preset:
            cities = list(preset["names"])

    if args.drive:
        dres = drive_mod.plan_drive(conn, args.date, cities, closed=not args.open,
                                    time=args.time, with_rental=args.rental or args.drive_rental,
                                    car_class=args.car, rest_days=args.rest_days)
        if not dres.get("ok"):
            print(dres.get("error") or "自驾规划失败")
            conn.close()
            return 1
        s = dres["summary"]
        print(f"\n自驾行程  {' → '.join(dres['plan_order'])}")
        print(f"  {s['city_count']} 城 · {s['total_km']} 公里 · 驾驶 {s['drive_hours']} 小时 · "
              f"{s['total_days']} 天 · 过路费约 ¥{s['tolls']} · 油费约 ¥{s['fuel']}"
              + ("（含估算路段）" if s.get("estimated") else "（高德真实里程）"))
        preset = drive_mod.loop_drive(args.loop) if args.loop else {}
        if preset:
            print(f"  环线自驾：约 {preset.get('km')} 公里 / 建议 {preset.get('days')} 天 · "
                  f"路线 {' → '.join(preset.get('roads') or [])}")
            if preset.get("road_note"):
                print(f"  {preset['road_note']}")
            for w in preset.get("warnings") or []:
                print(f"  ⚠ {w}")
        for leg in dres["legs"]:
            if leg.get("error"):
                print(f"\nD{leg['day_no']}  {leg['date']}  {leg['frm']} → {leg['to']}  !! {leg['error']}")
                continue
            d = leg["drive"]
            print(f"\nD{leg['day_no']}  {leg['date']}  {leg['frm']} → {leg['to']}  停留: {leg.get('stay') or '—'}")
            print(f"  🚗 {d['distance_km']} 公里 · 驾驶 {d['hours_text']}"
                  + (f"（含休息 {d['break_minutes']} 分钟）" if d["break_minutes"] else "")
                  + f" · {d['dep_text']} → {d['arr_text']} · 过路费约 ¥{d['tolls']:.0f} · 油费约 ¥{d['fuel']}")
            if d.get("note"):
                print(f"  ({d['note']})")
        for w in dres.get("warnings") or []:
            print(f"\n⚠ {w}")
        if dres.get("rental"):
            q = dres["rental"]
            print(f"\n租车估算（{q['pickup']} 取车 · {q['car_class']} · {q['tier']} 档 {q['tier_label']}）")
            print(f"  日租 ¥{q['daily_low']}-{q['daily_high']} × {q['days']} 天"
                  + (f" · 异地还车费约 ¥{q['one_way_fee']}（{q['one_way_km']} 公里）" if q["one_way"] else "")
                  + f" · 保险 ¥{q['insurance_per_day'][0]}-{q['insurance_per_day'][1]}/天")
            print(f"  油费约 ¥{q['fuel']} · 过路费约 ¥{q['tolls']}"
                  f" → 合计约 ¥{q['total_low']}-{q['total_high']}")
            print(f"  {q['disclaimer']}")
            for p in q["platforms"]:
                print(f"  · {p['name']}：{p['url']}（{p['note']}）")
        conn.close()
        return 0

    res = roam_mod.plan(conn, args.date, cities,
                        closed=not args.open,
                        free_order=args.free_order,
                        time=args.time, mode=args.mode,
                        max_transfers=args.max_transfers,
                        links=args.links, guide=not args.no_guide,
                        buffer_min=args.buffer,
                        objective=args.objective, slack_hours=args.slack,
                        max_days=args.max_days or None,
                        rest_days=args.rest_days)
    if not res.get("ok"):
        print(res.get("error") or "规划失败")
        conn.close()
        return 1

    summary = res["summary"]
    print(f"\n城市漫游  {' → '.join(res['plan_order'])}")
    print(f"  {summary['city_count']} 城 · 跨 {'/'.join(res['regions']) or '—'} · "
          f"全程 {summary['total_days']} 天 · 建议游览合计 {summary['play_hours']} 小时 · "
          f"在途 {summary['travel_hours']} 小时"
          + (f" · {summary['price_text']}" if summary.get("price_text") else ""))
    for name, card in res["guides"].items():
        print()
        _print_city_line(card, indent="  ")
    print("\n乘车表：")
    for rec in res["legs"]:
        tail = f"    停留: {rec['stay']}" if rec["stay"] is not None else ""
        print(f"\nD{rec['day_no']}  {rec['date']}  {rec['frm']} → {rec['to']}{tail}")
        if rec.get("journey") is None:
            print(f"  !! {rec['error']}")
            continue
        _print_journey_block(rec["journey"], indent="  ")
    for w in res.get("warnings") or []:
        print(f"\n警告：{w}")
    near = (res.get("suggestions") or {}).get("near_route") or []
    if near:
        print("\n顺路可加：" + "；".join(
            f"{s['name']}（{s['score']}，距路线 {s['km']} 公里）" for s in near))
    print(f"\n共 {summary['total_days']} 天")
    conn.close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="travel-planner", description="国内大交通换乘规划（铁路/航班/空铁联运 + 大环线）")
    p.add_argument("--db", help="SQLite 路径（默认 data/timetable.db）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化数据库")

    sub.add_parser("sync-stations", help="从 12306 同步全国车站表")

    ak = sub.add_parser("set-amap-key", help="保存高德 Web服务 API key（用于公交中转指引）")
    ak.add_argument("key")

    sp = sub.add_parser("stations", help="按站名/三字码查车站")
    sp.add_argument("q")

    cc = sub.add_parser("copy-date", help="把某日时刻表复制到一个或多个日期（默认保护已有数据）")
    cc.add_argument("from_date", help="源日期 YYYY-MM-DD")
    cc.add_argument("to_date", nargs="+", help="目标日期 YYYY-MM-DD，可重复指定")
    cc.add_argument("--overwrite", action="store_true",
                    help="目标日期已有数据时覆盖（默认保护现有数据）")
    cc.add_argument("--dry-run", action="store_true", help="只预览复制/跳过结果，不写入数据库")

    cp = sub.add_parser("crawl-date", help="爬取某日全国枢纽间车次时刻表（耗时取决于限速与车次数；-f 可定向）")
    cp.add_argument("date", help="YYYY-MM-DD")
    cp.add_argument("--od", action="append", metavar="FROM:TO",
                    help="只爬指定 OD 对（三字码，可重复；调试/补数据用）")
    cp.add_argument("-f", "--focus", nargs="+", metavar="城市",
                    help="定向爬取：只爬这些城市相关的 OD（默认 320 对，全量 6006 对）")
    cp.add_argument("--focus-pairs", type=int, default=320,
                    help="定向爬取的 OD 对数上限（默认 320）")
    cp.add_argument("--pairs-limit", type=int, default=0, help="只爬前 N 个枢纽对（调试用）")
    cp.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="最小请求间隔秒数（默认 1，限速时自动增加）")
    cp.add_argument("--resume", action="store_true",
                    help="断点续爬：按 OD 成员跳过已完成的（换过 --od/-f 范围也不会错位）")
    cp.add_argument("--dry-run", action="store_true", help="只看会爬哪些 OD，不实际爬取")
    cp.set_defaults(func=cmd_crawl_date)

    cpp = sub.add_parser("crawl-progress", help="看某日爬取进度（断电/关窗口后照样查得到）")
    cpp.add_argument("date", help="YYYY-MM-DD")
    cpp.add_argument("--rows", type=int, default=0, help="额外打印最近 N 条逐对明细")
    cpp.set_defaults(func=cmd_crawl_progress)

    pp = sub.add_parser("plan", help="规划换乘方案")
    pp.add_argument("from_", metavar="FROM", help="出发地（城市名/站名/三字码）")
    pp.add_argument("to", metavar="TO", help="到达地")
    pp.add_argument("date", help="YYYY-MM-DD")
    pp.add_argument("--time", default="00:00", help="最早出发时间 HH:MM")
    pp.add_argument("--max-transfers", type=int, default=3, help="最大换乘次数")
    pp.add_argument("--top", type=int, default=10, help="最多显示方案数")
    pp.add_argument("--mode", choices=["rail", "air", "mixed", "green", "tour"], default="mixed",
                    help="rail=纯铁路 air=纯飞机 mixed=空铁联运 green=绿皮省钱 tour=单程游览（默认 mixed）")
    pp.add_argument("--green-fare", type=float, default=50,
                    help="绿皮/游览模式允许的高铁/动车单段最高票价（元，默认 50）")
    pp.add_argument("--tour-stay", choices=["transit", "halfday", "nights"], default="halfday",
                    help="游览模式每站停留：transit=纯中转 halfday=玩半天(默认) nights=住1晚")
    pp.add_argument("--with-flights", action="store_true",
                    help="游览模式允许飞机参与（超3倍绿皮直达价会提示但不拦截）")
    pp.add_argument("--tour-city-count", type=int, default=None,
                    help="游览模式希望经过的城市数（含起点和终点）")
    pp.add_argument("--rest-days", type=int, default=0,
                    help="游览模式每个到达城市额外休整天数")
    pp.add_argument("--max-days", type=int, default=0,
                    help="游览模式全程最多天数")
    pp.add_argument("--no-guide", action="store_true", help="不查询公交中转指引")
    pp.add_argument("--buffer", type=int, default=0,
                    help="换乘容差分钟（在最小换乘时间上额外增加，缓解紧张换乘）")
    pp.add_argument("--objective", choices=["fast", "cheap"], default="fast",
                    help="fast=最快到达（默认） cheap=限时内最省")
    pp.add_argument("--slack", type=int, default=2,
                    help="限时内最省允许比最快方案晚的小时数（默认 2）")
    pp.add_argument("--links", action="store_true", help="输出每段的 12306 购票深链")

    lp = sub.add_parser("loop", help="大环线多站点规划（固定顺序 / --free-order 寻优）")
    lp.add_argument("date", help="起始日期 YYYY-MM-DD")
    lp.add_argument("stops", nargs="+", help="途经点序列（城市名或站名，首尾可相同形成环线）")
    lp.add_argument("--stays", help="每个到达点后的停留，逗号分隔：0=纯中转，N=住N晚（如 2,2,0；默认 1）")
    lp.add_argument("--max-days", type=int, default=0,
                    help="全程天数预算（游览模式）；超限时给出删站建议")
    lp.add_argument("--tour-city-count", type=int, default=None,
                    help="游览模式希望保留的城市数（含起点和终点）")
    lp.add_argument("--rest-days", type=int, default=0,
                    help="游览模式每个到达城市额外休整天数")
    lp.add_argument("--free-order", action="store_true", help="对中间点做顺序寻优（TSP）")
    lp.add_argument("--time", default="08:00", help="每段默认最早出发时间 HH:MM")
    lp.add_argument("--max-transfers", type=int, default=2)
    lp.add_argument("--mode", choices=["rail", "air", "mixed", "green"], default="mixed",
                    help="rail=纯铁路 air=纯飞机 mixed=空铁联运 green=绿皮省钱（默认 mixed）")
    lp.add_argument("--green-fare", type=float, default=50,
                    help="绿皮模式允许的高铁/动车单段最高票价（元，默认 50）")
    lp.add_argument("--no-guide", action="store_true", help="不查询公交中转指引")
    lp.add_argument("--buffer", type=int, default=0,
                    help="换乘容差分钟（在最小换乘时间上额外增加）")
    lp.add_argument("--objective", choices=["fast", "cheap"], default="fast",
                    help="每段目标：fast=最快（默认） cheap=限时内最省")
    lp.add_argument("--slack", type=int, default=2,
                    help="限时内最省允许比最快方案晚的小时数（默认 2）")
    lp.add_argument("--links", action="store_true", help="输出每段的 12306 购票深链")

    rp = sub.add_parser("roam", help="城市漫游：按大区浏览城市 → 排成环线乘车表（地图选城的命令行版）")
    rp.add_argument("date", nargs="?", help="起始日期 YYYY-MM-DD（只浏览时可省略）")
    rp.add_argument("cities", nargs="*", help="城市序列（城市目录里的名字，2-12 个）")
    rp.add_argument("--regions", action="store_true", help="列出大区与其代表城市")
    rp.add_argument("--region", help="列出某个大区的全部城市（含半日/一日建议）")
    rp.add_argument("--loops", action="store_true", help="列出经典环线")
    rp.add_argument("--loop", help="查看某条经典环线详情，或直接用它规划（配合 date）")
    rp.add_argument("--drive", action="store_true",
                    help="自驾模式：走高德驾车路径算真实里程/耗时/过路费（需 amap_key）")
    rp.add_argument("--rental", action="store_true", help="自驾模式顺便给租车费用估算")
    rp.add_argument("--drive-rental", action="store_true", help="同 --rental（旧写法）")
    rp.add_argument("--car", default="SUV", help="车型：经济型 / SUV / 商务（默认 SUV）")
    rp.add_argument("--open", action="store_true", help="不回到起点（默认闭环）")
    rp.add_argument("--free-order", action="store_true", help="中间点顺序寻优（TSP）")
    rp.add_argument("--time", default="08:00", help="每段默认最早出发时间 HH:MM")
    rp.add_argument("--max-transfers", type=int, default=2)
    rp.add_argument("--mode", choices=["rail", "air", "mixed", "green"], default="mixed",
                    help="rail=纯铁路 air=纯飞机 mixed=空铁联运 green=绿皮省钱（默认 mixed）")
    rp.add_argument("--no-guide", action="store_true", help="不查询公交中转指引")
    rp.add_argument("--buffer", type=int, default=30,
                    help="换乘容差分钟（默认 30，城市间同站换乘更稳）")
    rp.add_argument("--objective", choices=["fast", "cheap"], default="fast",
                    help="每段目标：fast=最快（默认） cheap=限时内最省")
    rp.add_argument("--slack", type=int, default=2,
                    help="限时内最省允许比每段最快方案晚的小时数（默认 2）")
    rp.add_argument("--max-days", type=int, default=0, help="全程天数预算，超限给删站建议")
    rp.add_argument("--rest-days", type=int, default=0, help="每个到达城市额外休整天数")
    rp.add_argument("--links", action="store_true", help="输出每段的 12306 购票深链")

    args = p.parse_args(argv)
    handlers = {
        "init": cmd_init,
        "sync-stations": cmd_sync_stations,
        "set-amap-key": cmd_set_amap_key,
        "stations": cmd_stations,
        "copy-date": cmd_copy_date,
        "crawl-date": cmd_crawl_date,
        "crawl-progress": cmd_crawl_progress,
        "plan": cmd_plan,
        "loop": cmd_loop,
        "roam": cmd_roam,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
