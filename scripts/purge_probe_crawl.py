"""清掉探测时误启的一次爬取留下的痕迹（2099-01-01 那个假日期）。

背景：浏览器探针里点了「接着爬」，而真实前端走的是模块内的 postJSON，探针的
`window.TP.postJSON` 打桩拦不住 → 真的向后端发了一次「全国爬取 2099-01-01」。
虽然马上取消了，但账本 / 时刻表 / 断点文件里会留下这个假日期，必须清干净，
否则数据台会显示一条莫名其妙的进度。

用法：
    python scripts/purge_probe_crawl.py --date 2099-01-01 --dry-run
    python scripts/purge_probe_crawl.py --date 2099-01-01
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from travel_planner import crawl_progress, db      # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="清理探测留下的爬取痕迹")
    ap.add_argument("--date", required=True, help="要清理的日期（如 2099-01-01）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    date = args.date

    conn = db.connect()
    try:
        crawl_progress.ensure_schema(conn)
        pairs = conn.execute("SELECT COUNT(*) c FROM crawl_pairs WHERE date=?", (date,)).fetchone()["c"]
        runs = conn.execute("SELECT COUNT(*) c FROM crawl_runs WHERE date=?", (date,)).fetchone()["c"]
        rows = conn.execute("SELECT COUNT(*) c FROM schedules WHERE date=?", (date,)).fetchone()["c"]
        trains = conn.execute("SELECT COUNT(DISTINCT train_no) c FROM schedules WHERE date=?",
                              (date,)).fetchone()["c"]
        meta = conn.execute("SELECT COUNT(*) c FROM meta WHERE key LIKE ?", (f"crawl:{date}%",)).fetchone()["c"]
        files = [p for p in (ROOT / "data").glob(f"*_{date}.*")]
        print(f"{date}：账本 {pairs} 对 / 任务 {runs} 条 · 时刻表 {trains} 趟（{rows} 行）· "
              f"meta {meta} 条 · 文件 {[p.name for p in files]}")
        if args.dry_run:
            print("（dry-run：没删）")
            return 0
        with conn:
            conn.execute("DELETE FROM crawl_pairs WHERE date=?", (date,))
            conn.execute("DELETE FROM crawl_runs WHERE date=?", (date,))
            conn.execute("DELETE FROM schedules WHERE date=?", (date,))
            conn.execute("DELETE FROM meta WHERE key LIKE ?", (f"crawl:{date}%",))
        for p in files:
            try:
                p.unlink()
                print("已删除文件:", p.name)
            except OSError as exc:
                print(f"⚠ 删不掉 {p.name}: {exc}", file=sys.stderr)
        print(f"✓ 已清理 {date}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
