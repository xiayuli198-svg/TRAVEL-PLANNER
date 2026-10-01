"""个人使用的时刻表维护助手。

常用操作：
  python scripts/maintain_data.py status
  python scripts/maintain_data.py prepare 2026-09-20
  python scripts/maintain_data.py copy 2026-09-20 2026-09-21 2026-09-22

复制默认跳过已有日期；确认数据确实需要替换时加 ``--overwrite``。
``--dry-run`` 可先查看会复制/跳过哪些日期。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from travel_planner import db, service  # noqa: E402


def _source_for(conn, target: str) -> str | None:
    try:
        day = dt.date.fromisoformat(target)
    except ValueError:
        return None
    dates = service.available_dates(conn)
    if not dates:
        return None
    # 优先全国性完整日期（通常是导入数据集），然后按日历距离选择。
    full = [x for x in dates if x["trains"] >= 1000] or dates
    return min(full, key=lambda x: abs(dt.date.fromisoformat(x["date"]) - day))["date"]


def _print_result(result: dict) -> int:
    if not result.get("ok"):
        print(result.get("error", "操作失败"), file=sys.stderr)
        return 1
    if result.get("source"):
        print(f"基准日期：{result['source']}（{result.get('source_rows', 0)} 行）")
    for item in result.get("results", []):
        status = {"copied": "已复制", "skipped": "已跳过", "would_copy": "将复制"}.get(
            item["status"], item["status"])
        reason = f"（{item['reason']}）" if item.get("reason") else ""
        print(f"  {item['date']}：{status} {item.get('rows', 0)} 行{reason}")
    if result.get("note"):
        print(result["note"])
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="travel-planner 数据维护助手")
    parser.add_argument("--db", help="SQLite 路径（默认 data/timetable.db）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="查看日期覆盖和数据库状态")
    prep = sub.add_parser("prepare", help="为目标日期自动选最近完整数据复制")
    prep.add_argument("target", nargs="+", help="目标日期，可一次指定多个")
    prep.add_argument("--overwrite", action="store_true", help="覆盖目标已有时刻表")
    prep.add_argument("--dry-run", action="store_true", help="只预览，不写入数据库")
    cp = sub.add_parser("copy", help="从 SOURCE 批量复制到一个或多个目标日期")
    cp.add_argument("source", help="源日期 YYYY-MM-DD")
    cp.add_argument("targets", nargs="+", help="目标日期 YYYY-MM-DD")
    cp.add_argument("--overwrite", action="store_true", help="覆盖目标已有时刻表")
    cp.add_argument("--dry-run", action="store_true", help="只预览，不写入数据库")
    args = parser.parse_args(argv)

    conn = db.connect(args.db)
    try:
        if args.command == "status":
            status = service.data_status(conn)
            status["dates"] = service.available_dates(conn)
            print(f"车站 {status['stations']} 个 · 机场 {status['airports']} 个（静态主数据 {status.get('airport_master', 0)}）")
            print(f"航班缓存 {status['flight_routes_cached']} 条 · 旅游资料 {status['tourism_cities']} 个城市")
            print("\n时刻表日期：")
            for row in status["dates"]:
                print(f"  {row['date']}  {row['trains']:>5} 趟")
            if status["dates"]:
                best = max(status["dates"], key=lambda x: x["trains"])
                print(f"\n推荐基线：{best['date']}（{best['trains']} 趟）")
            return 0
        if args.command == "prepare":
            source = _source_for(conn, args.target[0])
            if not source:
                print("没有可用的源日期，请先 import_dataset 或 crawl-date", file=sys.stderr)
                return 1
            if len(args.target) > 1:
                print(f"自动选择源日期：{source}")
            result = service.copy_dates(conn, source, args.target,
                                        overwrite=args.overwrite, dry_run=args.dry_run)
            return _print_result(result)
        result = service.copy_dates(conn, args.source, args.targets,
                                    overwrite=args.overwrite, dry_run=args.dry_run)
        return _print_result(result)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
