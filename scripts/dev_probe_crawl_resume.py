"""手工走一遍「中断 → 接着爬」：用假 client，不联网，不碰真实数据库。

    python -X utf8 scripts/dev_probe_crawl_resume.py

为什么留着这个脚本：`tests/test_crawl_resume.py` 是回归网，这个脚本是**给人看的**演示 ——
把「爬到一半断电」「换个爬取范围继续」「旧断点怎么处理」三种情况逐条打印出来，
一眼能看出续爬到底跳过了什么、补了什么。两边的断言是同一套语义。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from travel_planner import crawl_progress, db          # noqa: E402
from travel_planner.ingest import crawl                # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DATE = "2099-01-01"
CODES = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]
PAIRS = [(a, b) for a in CODES for b in CODES if a != b]

calls = {"left": [], "info": []}
fail_on: set[str] = set()


def fake_left(client, date, frm, to):
    key = f"{frm}:{to}"
    calls["left"].append(key)
    if key in fail_on:
        raise ConnectionError(f"模拟断电：{key}")
    n = len(calls["left"])
    code = f"K{n:03d}"
    return [{"train_no": f"{code}00", "train_code": code, "from_code": frm,
             "to_code": to, "from": frm, "to": to}]


def fake_info(client, train_no, date):
    calls["info"].append(train_no)
    return [{"seq": "01", "station_code": "AAA", "station_name": "站AAA",
             "arr": "08:00", "dep": "08:05", "day": "0"},
            {"seq": "02", "station_code": "BBB", "station_name": "站BBB",
             "arr": "10:00", "dep": "10:05", "day": "0"}]


crawl.query_left_ticket = fake_left
crawl.query_train_info = fake_info
crawl._resolve_and_anchor = lambda conn, stops, frm, **kw: stops
crawl.time.sleep = lambda *_: None
crawl.Client12306.warmup = lambda self: None


def run(conn, ods, *, resume=True, cancel_after=None, label=""):
    def cancel():
        return cancel_after is not None and len(calls["left"]) >= cancel_after

    calls["left"].clear()
    calls["info"].clear()
    stats = crawl.crawl_date(conn, DATE, ods=ods, delay=0, resume=resume,
                             progress_every=1, cancel_cb=cancel)
    print(f"  [{label}] 查了 {len(calls['left'])} 对 OD："
          f"{'、'.join(calls['left'][:6])}{'…' if len(calls['left']) > 6 else ''}"
          f"｜跳过已完成 {stats['skipped_pairs']} 对｜入库 {stats['ingested']} 趟")
    return stats, list(calls["left"])


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="crawl-resume-demo-"))
    real_root = db.PROJECT_ROOT
    db.PROJECT_ROOT = tmp                      # 断点面包屑也写进临时目录
    problems: list[str] = []
    try:
        conn = db.connect(str(tmp / "demo.db"))
        for i, code in enumerate(CODES):
            conn.execute("INSERT OR REPLACE INTO stations(code,name,city,ordinal) "
                         "VALUES(?,?,?,?)", (code, f"站{code}", f"城{code}", i))
        conn.commit()

        print("① 爬到第 3 对就中断（相当于关窗口 / 断电）")
        _, first = run(conn, PAIRS[:8], cancel_after=3, label="第一次")
        s = crawl_progress.summary(conn, DATE)
        print(f"  账本：{s['done']}/{s['planned']} 完成，剩余 {s['remaining']}，状态 {s['state']}")
        print(f"  {crawl_progress.note_for(s)}")

        print("\n② 再点一次「接着爬」")
        _, second = run(conn, PAIRS[:8], label="续爬")
        if set(first) & set(second):
            problems.append(f"续爬重复查了：{set(first) & set(second)}")
        if len(second) != len(PAIRS[:8]) - 3:
            problems.append(f"续爬应只补 5 对，实际 {len(second)} 对")

        print("\n③ 换成另一份 OD 列表继续（旧实现会在这里切空、报「入库 0 趟」）")
        other = PAIRS[:3] + [("HHH", "AAA"), ("HHH", "BBB")]
        _, third = run(conn, other, label="换列表")
        if third != ["HHH:AAA", "HHH:BBB"]:
            problems.append(f"换列表续爬应只补 HHH:AAA / HHH:BBB，实际 {third}")

        print("\n④ 断电时正在做的那一对：续爬会重试它")
        fail_on.add("GGG:AAA")
        run(conn, PAIRS[:8] + [("GGG", "AAA")], label="遇到网络错")
        fail_on.clear()
        _, retried = run(conn, PAIRS[:8] + [("GGG", "AAA")], label="再续爬")
        if "GGG:AAA" not in retried:
            problems.append("失败的那一对没有在续爬时重做")

        print("\n⑤ 已经入库的车次不会重复抓")
        calls["info"].clear()
        run(conn, PAIRS[:3], resume=False, label="不续爬重跑")
        print(f"  重新跑一遍但一条停站都没再抓：{'是' if not calls['info'] else '否'}")
        if calls["info"]:
            problems.append(f"已入库车次被重复抓：{calls['info'][:3]}")

        print("\n⑥ 旧格式断点（只有序号）会被如实提示，不假装能精确续爬")
        legacy_date = "2099-02-02"
        (tmp / "data").mkdir(parents=True, exist_ok=True)
        (tmp / "data" / f"crawl_state_{legacy_date}.txt").write_text(
            "phase pairs\nindex 596\n", encoding="utf-8")
        info = crawl_progress.legacy_info(legacy_date)
        print("  " + (info["text"] if info else "(没识别到)"))

        print("\n" + ("✓ 六种情况都符合预期" if not problems
                      else "✗ 有问题：\n  - " + "\n  - ".join(problems)))
        return 1 if problems else 0
    finally:
        db.PROJECT_ROOT = real_root
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
