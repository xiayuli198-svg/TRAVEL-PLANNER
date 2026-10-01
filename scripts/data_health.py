"""一键数据体检：汇总各数据表规模/覆盖/问题，并给出修复命令提示。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from travel_planner.ingest.amap import get_key  # noqa: E402

DB = Path(__file__).resolve().parent.parent / "data" / "timetable.db"


def main() -> None:
    # Windows PowerShell 旧版常以 GBK 输出，诊断中的中文标记（如 ⚠）
    # 会触发 UnicodeEncodeError；尽量切到 UTF-8，失败时仍保持默认流。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    clean_junk = "--clean-junk" in sys.argv
    keep_dates = {"2026-09-09"}   # 保留实时爬取的京沪样本（演示/调试）

    if clean_junk:
        junk = [r["date"] for r in conn.execute(
            "SELECT date FROM schedules GROUP BY date "
            "HAVING COUNT(DISTINCT train_no) < 1000")]
        to_del = [d for d in junk if d not in keep_dates]
        with conn:
            for d in to_del:
                conn.execute("DELETE FROM schedules WHERE date=?", (d,))
                conn.execute("DELETE FROM meta WHERE key=?", (f"crawl:{d}:stats",))
        print(f"已清理 {len(to_del)} 个车次不足 1000 的日期: {', '.join(to_del)}")

    print("=== 数据体检 ===")
    print(f"车站: {conn.execute('SELECT COUNT(*) c FROM stations').fetchone()['c']}")
    print(f"机场: {conn.execute('SELECT COUNT(*) c FROM airports').fetchone()['c']}")
    print(f"高德 key: {'已配置' if get_key(conn) else '未配置'}")

    print("\n-- 铁路时刻表（按日期） --")
    rows = conn.execute(
        "SELECT date, COUNT(DISTINCT train_no) n FROM schedules "
        "GROUP BY date ORDER BY date").fetchall()
    small = [r for r in rows if r["n"] < 1000]
    for r in rows:
        src = conn.execute("SELECT value FROM meta WHERE key=?",
                           (f"crawl:{r['date']}:stats",)).fetchone()
        imp = conn.execute("SELECT value FROM meta WHERE key=?",
                           (f"import:{r['date']}",)).fetchone()
        origin = "导入" if imp else ("爬取/复制" if src else "?")
        flag = "  ⚠ 车次少" if r["n"] < 1000 else ""
        print(f"  {r['date']}  {r['n']:>5} 车次 [{origin}]{flag}")
    if small:
        print(f"\n  提示：{len(small)} 个日期车次 <1000（多为局部线路或调试复制），"
              "确认无用可清理：")
        for r in small[:5]:
            print(f"    DELETE FROM schedules WHERE date='{r['date']}';")

    print("\n-- 航班 --")
    print(f"  航班行数: {conn.execute('SELECT COUNT(*) c FROM flights').fetchone()['c']}")
    print(f"  缓存航线数: {conn.execute('SELECT COUNT(*) c FROM flight_route_cache').fetchone()['c']}")
    print(f"  缺票价航班: {conn.execute('SELECT COUNT(DISTINCT flight_no) c FROM flights WHERE price IS NULL').fetchone()['c']}（可删对应 flight_route_cache 重取）")
    airport_count = conn.execute("SELECT COUNT(*) c FROM airports").fetchone()["c"]
    master_count = conn.execute("SELECT COUNT(*) c FROM airports WHERE source='master'").fetchone()["c"]
    airport_cities = conn.execute("SELECT COUNT(DISTINCT city) c FROM airports WHERE city<>''").fetchone()["c"]
    flight_airports = conn.execute("SELECT COUNT(DISTINCT code) c FROM (SELECT dep_airport code FROM flights UNION SELECT arr_airport FROM flights)").fetchone()["c"]
    missing_airports = conn.execute("SELECT COUNT(*) c FROM (SELECT DISTINCT code FROM (SELECT dep_airport code FROM flights UNION SELECT arr_airport FROM flights) f WHERE NOT EXISTS (SELECT 1 FROM airports a WHERE a.code=f.code))").fetchone()["c"]
    print(f"  机场目录: {airport_count} 座（静态主数据 {master_count}）/ {airport_cities} 个城市；航班实际引用 {flight_airports} 座")
    if missing_airports:
        print(f"  ⚠ 有 {missing_airports} 个航班机场代码未在 airports 目录中")
    if master_count:
        print("  注：机场目录已包含静态主数据；航班时刻仍由按日期/航线的动态查询提供。")
    else:
        print("  注：尚未导入静态机场主数据，可运行 python scripts/import_airports.py。")

    print("\n-- 坐标（票价估算精度） --")
    total = conn.execute("SELECT COUNT(*) c FROM geo_cache").fetchone()["c"]
    ok = conn.execute("SELECT COUNT(*) c FROM geo_cache WHERE failed=0").fetchone()["c"]
    print(f"  已缓存 {total}（成功 {ok}，失败 {total - ok}）")
    if total - ok:
        print("  提示：失败项可重试: python scripts/prewarm_coords.py")

    print("\n-- 中转指引缓存 --")
    print(f"  transit_cache: {conn.execute('SELECT COUNT(*) c FROM transit_cache').fetchone()['c']} 条")

    print("\n-- 空间 --")
    size_mb = DB.stat().st_size / 1024 / 1024
    print(f"  DB 体积: {size_mb:.1f} MB")


if __name__ == "__main__":
    main()
