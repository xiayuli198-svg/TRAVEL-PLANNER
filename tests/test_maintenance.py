"""数据台维护（maintenance / jobs）单测：来源判定、计划、一键执行、后台任务。

重点保护三件事：
1. **来源不能混**：复制出来的数据必须被认成 copy，不能冒充真爬（否则页面会让人误信）。
2. **计划只读**：plan()/overview() 不改数据库；只有 apply_plan()/delete_date() 才写。
3. **真爬不自动**：CrawlJob 只有显式 start 才跑，取消后状态可查。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from travel_planner import db, jobs, maintenance as mt


def _seed(conn, date: str, trains: int = 12000) -> None:
    """塞一天的最小数据：每个车次两站。"""
    rows = []
    for i in range(trains):
        no = f"G{i:05d}"
        rows.append((no, no, date, 1, "BJP", "北京南", 0, "", "08:00"))
        rows.append((no, no, date, 2, "SHH", "上海虹桥", 0, "12:00", ""))
    with conn:
        conn.executemany(
            "INSERT INTO schedules(train_no,train_code,date,seq,station_code,station_name,day,arr,dep)"
            " VALUES(?,?,?,?,?,?,?,?,?)", rows)


class MaintainBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class TestClassify(MaintainBase):
    def test_source_buckets(self):
        _seed(self.conn, "2026-06-17", 12000)
        db.set_meta(self.conn, "import:2026-06-17", "train_detail_20260617.json")
        _seed(self.conn, "2026-07-01", 12000)
        db.set_meta(self.conn, "crawl:2026-07-01:stats", "pairs=100 discovered=12000")
        _seed(self.conn, "2026-07-02", 12000)
        db.set_meta(self.conn, "copy:2026-07-02:from", "2026-06-17")
        _seed(self.conn, "2026-07-03", 40)
        _seed(self.conn, "2026-07-04", 12000)          # 没有来源记录 → 保守当拷贝

        rows = {r["date"]: r for r in mt.date_rows(self.conn)}
        self.assertEqual(rows["2026-06-17"]["source"], "import")
        self.assertEqual(rows["2026-07-01"]["source"], "crawl")
        self.assertEqual(rows["2026-07-02"]["source"], "copy")
        self.assertEqual(rows["2026-07-02"]["copied_from"], "2026-06-17")
        self.assertEqual(rows["2026-07-03"]["source"], "sparse")
        self.assertEqual(rows["2026-07-04"]["source"], "copy")

    def test_old_style_copy_stat_is_recognised(self):
        """早期复制只写了 crawl:*:stats='copied from ...'，也要认成拷贝。"""
        _seed(self.conn, "2026-08-01", 12000)
        db.set_meta(self.conn, "crawl:2026-08-01:stats",
                    "copied from 2026-06-17 (102769 rows, auto by ensure_date_data)")
        info = mt.classify_date(self.conn, "2026-08-01", 12000)
        self.assertEqual(info["source"], "copy")
        self.assertEqual(info["copied_from"], "2026-06-17")

    def test_pick_base_prefers_truthful(self):
        _seed(self.conn, "2026-06-17", 12000)
        db.set_meta(self.conn, "import:2026-06-17", "x")
        _seed(self.conn, "2026-07-02", 12000)
        db.set_meta(self.conn, "copy:2026-07-02:from", "2026-06-17")
        base = mt.pick_base(mt.date_rows(self.conn))
        self.assertEqual(base["date"], "2026-06-17")
        self.assertEqual(base["source"], "import")

    def test_route_explains_copy(self):
        _seed(self.conn, "2026-07-02", 12000)
        db.set_meta(self.conn, "copy:2026-07-02:from", "2026-06-17")
        notes = mt.route_for_date(self.conn, "2026-07-02")
        self.assertTrue(any("拷贝" in n and "2026-06-17" in n for n in notes))
        self.assertTrue(any("自动复制" in n for n in mt.route_for_date(self.conn, "2027-01-01")))


class TestPlan(MaintainBase):
    def setUp(self):
        super().setUp()
        _seed(self.conn, "2026-06-17", 12000)
        db.set_meta(self.conn, "import:2026-06-17", "x")
        _seed(self.conn, "2026-07-03", 40)            # 车次偏少

    def test_plan_is_read_only(self):
        before = self.conn.execute("SELECT COUNT(*) c FROM schedules").fetchone()["c"]
        mt.plan(self.conn, ["2026-09-01", "2026-09-02"], base="2026-06-17", today="2026-08-01")
        after = self.conn.execute("SELECT COUNT(*) c FROM schedules").fetchone()["c"]
        self.assertEqual(before, after, "plan() 不该写库")

    def test_plan_actions(self):
        plan = mt.plan(self.conn, ["2026-06-17", "2026-07-03", "2026-09-01"],
                       base="2026-06-17", today="2026-08-01")
        by_date = {s["date"]: s for s in plan["steps"]}
        self.assertEqual(by_date["2026-06-17"]["action"], "keep")       # 基准日
        self.assertEqual(by_date["2026-07-03"]["action"], "overwrite")  # 车次少 → 覆盖
        self.assertEqual(by_date["2026-09-01"]["action"], "copy")       # 缺数据 → 拷贝
        self.assertEqual(by_date["2026-09-01"]["from"], "2026-06-17")

    def test_plan_crawl_and_window(self):
        plan = mt.plan(self.conn, ["2026-08-05", "2030-01-01"],
                       base="2026-06-17", crawl=["2026-08-05", "2030-01-01"],
                       today="2026-08-01")
        actions = [(s["date"], s["action"]) for s in plan["steps"]]
        self.assertIn(("2026-08-05", "crawl"), actions)                 # 窗口内 → 真爬
        self.assertIn(("2030-01-01", "skip"), actions)                  # 太远 → 跳过
        # 不能出现「同一天既有 skip 又有 copy」这种自相矛盾的计划
        self.assertEqual([d for d, _ in actions].count("2030-01-01"), 1)

    def test_apply_plan_marks_source(self):
        plan = mt.plan(self.conn, ["2026-09-01"], base="2026-06-17", today="2026-08-01")
        res = mt.apply_plan(self.conn, plan["steps"], dry_run=True)
        self.assertTrue(res["dry_run"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM schedules WHERE date='2026-09-01'").fetchone()["c"], 0,
            "dry-run 不该写库")
        res = mt.apply_plan(self.conn, plan["steps"], dry_run=False)
        self.assertEqual(len(res["done"]), 1)
        info = mt.classify_date(self.conn, "2026-09-01", self.conn.execute(
            "SELECT COUNT(DISTINCT train_no) c FROM schedules WHERE date='2026-09-01'"
        ).fetchone()["c"])
        self.assertEqual(info["source"], "copy")
        self.assertEqual(info["copied_from"], "2026-06-17")

    def test_apply_plan_can_overwrite_sparse_day(self):
        plan = mt.plan(self.conn, ["2026-07-03"], base="2026-06-17", today="2026-08-01")
        mt.apply_plan(self.conn, plan["steps"], dry_run=False)
        trains = self.conn.execute(
            "SELECT COUNT(DISTINCT train_no) c FROM schedules WHERE date='2026-07-03'"
        ).fetchone()["c"]
        self.assertEqual(trains, 12000)

    def test_missing_between(self):
        gaps = mt.missing_between(self.conn, "2026-06-16", "2026-06-19")
        self.assertEqual(gaps, ["2026-06-16", "2026-06-18", "2026-06-19"])
        self.assertEqual(mt.missing_between(self.conn, "2026-06-19", "2026-06-16"), [])

    def test_delete_date_clears_meta(self):
        pre = mt.delete_date(self.conn, "2026-07-03", dry_run=True)
        self.assertEqual(pre["rows"], 80)
        self.assertTrue(self.conn.execute(
            "SELECT COUNT(*) c FROM schedules WHERE date='2026-07-03'").fetchone()["c"])
        out = mt.delete_date(self.conn, "2026-07-03")
        self.assertEqual(out["deleted"], 80)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM schedules WHERE date='2026-07-03'").fetchone()["c"], 0)
        self.assertFalse(mt.delete_date(self.conn, "不是日期")["ok"])


class TestHealth(MaintainBase):
    def test_health_flags(self):
        _seed(self.conn, "2026-06-17", 12000)
        db.set_meta(self.conn, "import:2026-06-17", "x")
        _seed(self.conn, "2026-07-02", 12000)
        db.set_meta(self.conn, "copy:2026-07-02:from", "2026-06-17")
        _seed(self.conn, "2026-07-03", 30)
        h = mt.health(self.conn)
        self.assertEqual(h["dates"], 3)
        self.assertEqual(h["truthful"], 1)
        self.assertEqual(h["copies"], 1)
        self.assertEqual(h["sparse"], ["2026-07-03"])
        self.assertTrue(any(i["fix"] == "delete_sparse" for i in h["items"]))
        self.assertTrue(any("拷贝" in i["text"] for i in h["items"]))

    def test_overview_shape(self):
        _seed(self.conn, "2026-06-17", 12000)
        db.set_meta(self.conn, "import:2026-06-17", "x")
        ov = mt.overview(self.conn, today="2026-08-01")
        self.assertTrue(ov["ok"] if "ok" in ov else True)
        self.assertEqual(ov["today"], "2026-08-01")
        self.assertEqual(ov["base"]["date"], "2026-06-17")
        self.assertIn("items", ov["health"])
        self.assertEqual(ov["dates"][0]["label"], "导入数据集")


class TestCrawlJob(MaintainBase):
    def test_job_does_not_start_by_itself(self):
        """注册表里没有任务时，任何查询都不该触发爬取。"""
        reg = jobs.JobRegistry()
        self.assertFalse(reg.any_running())
        self.assertEqual(reg.snapshots(), [])
        self.assertFalse(reg.cancel()["ok"])

    def test_job_cancel_and_snapshot(self):
        _seed(self.conn, "2026-09-01", 10)
        job = jobs.CrawlJob("2026-09-01", db_path=str(Path(self.tmp.name) / "t.db"))
        self.assertEqual(job.state, jobs.STATE_RUNNING)
        snap = job.snapshot()
        for key in ("date", "state", "phase", "done", "total", "logs"):
            self.assertIn(key, snap)
        job.cancel()
        self.assertTrue(job.cancelled(), "取消请求要能被爬取线程看到")

    def test_registry_single_task(self):
        reg = jobs.JobRegistry()
        first = jobs.CrawlJob("2026-09-02")
        reg._jobs["2026-09-02"] = first
        reg._current = first
        res = reg.start_crawl("2026-09-03")
        self.assertFalse(res["ok"], "已有任务在跑时不该再起一个")
        self.assertIn("已有爬取任务", res["error"])
        self.assertFalse(reg.start_crawl("2026-09-03", force=True)["ok"])

    def test_partial_crawl_is_not_presented_as_done(self):
        job = jobs.CrawlJob("2026-09-01", db_path=str(Path(self.tmp.name) / "t.db"))
        with patch("travel_planner.ingest.crawl.crawl_date",
                   return_value={"state": "partial", "failures": 1, "ingested": 3}):
            job.start()
            self.assertTrue(jobs.wait_for(job))
            job._thread.join(timeout=5)
        self.assertEqual(job.snapshot()["state"], "partial")

    def test_cooldown_snapshot_is_running_and_shows_countdown(self):
        job = jobs.CrawlJob("2026-09-01")
        job._on_progress("pairs", 2, 20, {})
        job._on_progress("cooldown", 2, 20, {"retry_in": 300, "delay": 2, "reason": "限速"})
        snapshot = job.snapshot()
        self.assertEqual(snapshot["state"], "running")
        self.assertEqual(snapshot["phase"], "限速冷却")
        self.assertEqual(snapshot["extra"]["retry_in"], 300)
        self.assertIsNone(snapshot["eta"])


if __name__ == "__main__":
    unittest.main()
