"""爬取断点续爬的测试：中断、断电、换范围之后都必须能接着爬。

以前这套逻辑**一行测试都没有**，而它有个静默的错位 bug：
旧断点只记「第几个 OD 做完了」（`index 596`），恢复时用 `pairs[596:]`。
全国爬到第 596 对中断、之后改用「定向爬取」（320 对）勾续爬 → 切片直接是空列表，
跑完报「入库 0 趟」，看着就像「根本没续爬，得重来」。

现在按 **OD 成员**记账（`crawl_progress`），所以这里盯四件事：
1. 中断后续爬只补没做完的那些；
2. 断电时「正在做」的那一对会被重试（不是跳过）；
3. **换了 OD 列表**也不会错位跳空或重复爬；
4. 已入库的车次不重复抓；旧格式断点只在能安全翻译时才迁移。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from travel_planner import crawl_progress, db
from travel_planner.ingest import crawl
from travel_planner.ingest.client12306 import RateLimited, TemporaryRequestError
from tests.test_client12306 import FakeClock

DATE = "2099-01-01"
HUB_CODES = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]


class CrawlHarness(unittest.TestCase):
    """用假 client 跑真实 crawl_date：不联网，但走完整流程（含落盘与入库）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="crawl-resume-test-"))
        self._root = db.PROJECT_ROOT
        db.PROJECT_ROOT = self.tmp                 # 断点面包屑文件也进临时目录
        self.conn = db.connect(str(self.tmp / "t.db"))
        for i, code in enumerate(HUB_CODES):
            self.conn.execute(
                "INSERT OR REPLACE INTO stations(code,name,city,ordinal) VALUES(?,?,?,?)",
                (code, f"站{code}", f"城{code}", i))
        self.conn.commit()
        self.calls = {"left": [], "info": []}
        # warmup 也会联网；测试和演示必须连初始化请求一起隔离。
        warmup_patch = patch.object(crawl.Client12306, "warmup")
        self.warmup = warmup_patch.start()
        self.addCleanup(warmup_patch.stop)
        # 打桩：每次 OD 查询返回一趟独有车次，方便数「到底查了几对」
        self._orig = (crawl.query_left_ticket, crawl.query_train_info, crawl._resolve_and_anchor,
                      crawl.time.sleep)
        crawl.query_left_ticket = self._fake_left
        crawl.query_train_info = self._fake_info
        crawl._resolve_and_anchor = lambda conn, stops, frm, **kw: stops
        crawl.time.sleep = lambda *_: None          # 重试等待对测试没意义，别拉长整套测试
        self.fail_on: set[str] = set()             # 让某些 OD 抛错，模拟断电/网络崩
        self.fail_train_on: set[str] = set()

    def tearDown(self):
        (crawl.query_left_ticket, crawl.query_train_info, crawl._resolve_and_anchor,
         crawl.time.sleep) = self._orig
        db.PROJECT_ROOT = self._root
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_left(self, client, date, frm, to):
        key = f"{frm}:{to}"
        self.calls["left"].append(key)
        if key in self.fail_on:
            raise ConnectionError(f"模拟断电：{key}")
        n = len(self.calls["left"])
        code = f"K{n:03d}"
        return [{"train_no": f"{code}00", "train_code": code, "from_code": frm,
                 "to_code": to, "from": frm, "to": to}]

    def _fake_info(self, client, train_no, date):
        self.calls["info"].append(train_no)
        if train_no in self.fail_train_on:
            raise ConnectionError(f"模拟断电：{train_no}")
        return [{"seq": "01", "station_code": "AAA", "station_name": "站AAA",
                 "arr": "08:00", "dep": "08:05", "day": "0"},
                {"seq": "02", "station_code": "BBB", "station_name": "站BBB",
                 "arr": "10:00", "dep": "10:05", "day": "0"}]

    # ---- 便捷方法 ----
    def pairs(self, n=None):
        codes = HUB_CODES if n is None else HUB_CODES[:n]
        return [(a, b) for a in codes for b in codes if a != b]

    def run_crawl(self, ods, *, resume=True, cancel_after=None):
        """``cancel_after=k``：处理完第 k 对之后收工（模拟「跑到第 k 个就断电/取消」）。"""
        def cancel():
            return cancel_after is not None and len(self.calls["left"]) >= cancel_after

        self.calls["left"].clear()
        self.calls["info"].clear()
        stats = crawl.crawl_date(self.conn, DATE, ods=ods, delay=0, resume=resume,
                                 progress_every=1, cancel_cb=cancel)
        return stats, list(self.calls["left"]), list(self.calls["info"])


class TestResumeByMembership(CrawlHarness):
    def test_cancel_then_resume_only_does_the_rest(self):
        ods = self.pairs(4)
        stats1, left1, _ = self.run_crawl(ods, cancel_after=3)
        self.assertEqual(len(left1), 3, "取消前只该查 3 对")

        stats2, left2, _ = self.run_crawl(ods)
        done_before = set(left1)
        self.assertFalse(done_before & set(left2),
                         f"续爬重复查了已完成的 OD：{done_before & set(left2)}")
        self.assertEqual(len(left2), len(ods) - 3, "续爬只该补剩下的那些")
        self.assertEqual(stats2["skipped_pairs"], 3, "续爬应当报告「跳过已完成 3 对」")

    def test_resume_with_a_different_pair_list_does_not_misalign(self):
        """这条就是旧实现的致命伤：全国爬到一半，改用定向爬取继续。"""
        wide = self.pairs(6)                      # 相当于「全国」按枢纽枚举出来的一大串
        narrow = wide[:3] + [("HHH", "AAA")]      # 相当于「定向」重算出来的另一份列表
        self.run_crawl(wide[:3], cancel_after=3)  # 前 3 对已完成

        _, left, _ = self.run_crawl(narrow)       # 换列表续爬
        self.assertEqual(left, ["HHH:AAA"],
                         f"只该补没做过的那一对，实际查了 {left}")

    def test_power_loss_retries_the_in_flight_pair(self):
        """断电时正在做的那一对要留在 running，续爬重新做它（不能算完成）。"""
        ods = self.pairs(3)
        self.fail_on = {"CCC:AAA"}                # 第 5 对（含重复）会炸
        self.run_crawl(ods)
        summary = crawl_progress.summary(self.conn, DATE)
        self.assertGreaterEqual(summary["failed"], 1, "失败的对要记成 failed")

        self.fail_on = set()
        _, left, _ = self.run_crawl(ods)
        self.assertIn("CCC:AAA", left, "失败/未完成的对续爬时要重做")

    def test_already_ingested_trains_are_not_refetched(self):
        ods = self.pairs(3)
        _, _, info1 = self.run_crawl(ods)
        self.assertTrue(info1)
        _, _, info2 = self.run_crawl(ods, resume=False)   # 不续爬，但车次已在库里
        self.assertEqual(info2, [], f"已入库的车次不该重复抓，实际 {info2}")

    def test_running_marked_before_request(self):
        """记账顺序：先写 running 再发请求 —— 否则断电时那一对无人认领。"""
        ods = [("AAA", "BBB")]
        self.fail_on = {"AAA:BBB"}
        self.run_crawl(ods)
        rows = crawl_progress.progress_rows(self.conn, DATE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], crawl_progress.STATUS_FAILED)
        self.assertGreaterEqual(rows[0]["tries"], 1)


class TestProgressLedger(CrawlHarness):
    def test_summary_reports_remaining_and_resumable(self):
        ods = self.pairs(5)
        self.run_crawl(ods, cancel_after=2)
        s = crawl_progress.summary(self.conn, DATE)
        self.assertEqual(s["planned"], len(ods))
        self.assertEqual(s["done"], 2)
        self.assertEqual(s["remaining"], len(ods) - 2)
        self.assertTrue(s["resumable"])
        self.assertEqual(s["state"], "cancelled")
        self.assertIn("中断", crawl_progress.note_for(s))
    def test_finished_run_is_not_resumable(self):
        self.run_crawl(self.pairs(2))
        s = crawl_progress.summary(self.conn, DATE)
        self.assertEqual(s["state"], "done")
        self.assertFalse(s["resumable"])
        self.assertEqual(s["remaining"], 0)

    def test_resume_false_starts_over(self):
        ods = self.pairs(3)
        self.run_crawl(ods, cancel_after=2)
        _, left, _ = self.run_crawl(ods, resume=False)
        self.assertEqual(len(left), len(ods), "不勾续爬就该从头来过")
        s = crawl_progress.summary(self.conn, DATE)
        self.assertEqual(s["done"], len(ods))

    def test_legacy_index_state_migrates_for_national_crawl(self):
        """旧格式断点（只有序号）在全国爬取时能安全翻译：那份列表是确定性的。"""
        ods = self.pairs(6)
        path = self.tmp / "data" / f"crawl_state_{DATE}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("phase pairs\nindex 4\n", encoding="utf-8")

        # 全国爬取：hubs 给了完整列表，crawl_date 会自己枚举 pairs（与 ods 顺序一致）
        self.calls["left"].clear()
        stats = crawl.crawl_date(self.conn, DATE, hubs=HUB_CODES, delay=0, resume=True,
                                 progress_every=1)
        done = crawl_progress.done_ods(self.conn, DATE)
        self.assertIn("AAA:BBB", done, "旧断点里已完成的对要迁移进来")
        self.assertEqual(stats["skipped_pairs"], 4, "应当跳过迁移进来的 4 对")

    def test_truncated_legacy_state_does_not_crash(self):
        """断电会把面包屑文件写坏 —— 那只是「人看的」文件，不能让它把整次爬取炸掉。"""
        path = self.tmp / "data" / f"crawl_state_{DATE}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("phase pairs\nind", encoding="utf-8")

        _, left, _ = self.run_crawl(self.pairs(2))
        self.assertEqual(len(left), 2, "坏文件应当被忽略，正常从头爬")

    def test_discovered_jsonl_with_half_written_line(self):
        """discovered 文件被断电写了一半：跳过坏行，别丢整份。"""
        path = self.tmp / "data" / f"discovered_{DATE}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        good = json.dumps({"train_no": "K90000", "code": "K900", "from": "AAA"},
                          ensure_ascii=False)
        path.write_text(good + "\n" + '{"train_no": "K9010', encoding="utf-8")

        ods = [("AAA", "BBB")]
        self.fail_train_on = {"K90000"}
        self.run_crawl(ods)
        # 坏行被跳过（没抛 JSONDecodeError），好行仍然被当成「已发现」
        s = crawl_progress.summary(self.conn, DATE)
        self.assertGreaterEqual(s["discovered"], 1)


class TestCrawlRecovery(CrawlHarness):
    def clock(self):
        clock = FakeClock()
        for target, fn in (("time.monotonic", clock.monotonic), ("time.sleep", clock.sleep)):
            stub = patch("travel_planner.ingest.client12306." + target, fn)
            stub.start()
            self.addCleanup(stub.stop)
        return clock

    def test_rate_limit_retries_same_od_after_increasing_cooldowns(self):
        clock = self.clock()
        attempts, progress = [], []

        def left(client, date, frm, to):
            attempts.append((frm, to, clock.now, client.delay))
            if len(attempts) <= 5:
                raise RateLimited("风控", retry_after=400 if len(attempts) == 1 else 0)
            return self._fake_left(client, date, frm, to)

        with patch.object(crawl, "query_left_ticket", left):
            result = crawl.crawl_date(self.conn, DATE, ods=[("AAA", "BBB")], delay=1,
                                     progress_cb=lambda *args: progress.append(args))
        self.assertEqual(result["state"], "done")
        self.assertEqual(result["ingested"], 1)
        self.assertEqual(len(attempts), 6, "超过原先四轮冷却仍能继续")
        for i, minimum in enumerate((400, 600, 1200, 1800, 1800)):
            self.assertGreaterEqual(attempts[i + 1][2] - attempts[i][2], minimum)
        self.assertEqual({a[:2] for a in attempts}, {("AAA", "BBB")})
        self.assertTrue(any(p[0] == "cooldown" and p[3]["retry_in"] > 0 for p in progress))
        self.assertGreater(attempts[-1][3], attempts[0][3])

    def test_network_outage_resumes_current_od(self):
        self.clock()
        train = self._fake_left(None, DATE, "AAA", "BBB")
        with patch.object(crawl, "query_left_ticket", side_effect=[TemporaryRequestError("offline"), train]) as left:
            result = crawl.crawl_date(self.conn, DATE, ods=[("AAA", "BBB")], cooldown_seconds=2)
        self.assertEqual(left.call_count, 2)
        self.assertEqual(result["state"], "done")

    def test_cancel_during_cooldown_keeps_current_od_resumable(self):
        clock = self.clock()
        beginning = clock.now
        with patch.object(crawl, "query_left_ticket", side_effect=RateLimited("blocked")) as left:
            result = crawl.crawl_date(self.conn, DATE, ods=[("AAA", "BBB")],
                                     cancel_cb=lambda: clock.now >= beginning + 1)
        self.assertTrue(result["cancelled"])
        self.assertLessEqual(clock.now - beginning, 1.25)
        left.assert_called_once()
        self.assertEqual(crawl_progress.summary(self.conn, DATE)["running"], 1)
        _, resumed, _ = self.run_crawl([("AAA", "BBB")])
        self.assertEqual(resumed, ["AAA:BBB"])

    def test_all_failed_national_run_is_not_migrated_as_completed(self):
        self.fail_on = {"AAA:BBB", "BBB:AAA"}
        result = crawl.crawl_date(self.conn, DATE, hubs=HUB_CODES[:2], resume=True)
        self.assertEqual(result["state"], "partial")
        self.assertTrue(crawl_progress.summary(self.conn, DATE)["resumable"])
        self.fail_on.clear()
        self.calls["left"].clear()
        result = crawl.crawl_date(self.conn, DATE, hubs=HUB_CODES[:2], resume=True)
        self.assertEqual(set(self.calls["left"]), {"AAA:BBB", "BBB:AAA"})
        self.assertEqual(result["skipped_pairs"], 0)

    def test_legacy_failure_log_prevents_skipping_failed_od(self):
        folder = self.tmp / "data"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"crawl_state_{DATE}.txt").write_text("phase pairs\nindex 2\n", encoding="utf-8")
        (folder / f"crawl_failures_{DATE}.txt").write_text("2026-09-26 pair AAA->BBB: blocked\n", encoding="utf-8")
        result = crawl.crawl_date(self.conn, DATE, hubs=HUB_CODES[:2], resume=True)
        self.assertEqual(self.calls["left"], ["AAA:BBB"])
        self.assertEqual(result["skipped_pairs"], 1)

    def test_failed_train_is_partial_then_ingested_on_resume(self):
        self.fail_train_on.add("K00100")
        result, _, info = self.run_crawl([("AAA", "BBB")])
        self.assertEqual(result["state"], "partial")
        self.assertEqual(info, ["K00100"], "停站传输重试不能在爬取层再叠三遍")
        self.assertTrue(crawl_progress.summary(self.conn, DATE)["resumable"])
        self.fail_train_on.clear()
        result, left, info = self.run_crawl([("AAA", "BBB")])
        self.assertEqual(left, [])
        self.assertEqual(info, ["K00100"])
        self.assertEqual(result["state"], "done")

    def test_new_records_after_partial_json_line_survive_next_resume(self):
        path = self.tmp / "data" / f"discovered_{DATE}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"train_no": "broken', encoding="utf-8")
        self.run_crawl([("AAA", "BBB")], cancel_after=1)
        result, left, info = self.run_crawl([("AAA", "BBB")])
        self.assertEqual(left, [])
        self.assertEqual(info, ["K00100"])
        self.assertEqual(result["ingested"], 1)

    def test_changed_scope_statistics_only_count_current_pairs(self):
        self.run_crawl(self.pairs(3))
        _, left, _ = self.run_crawl([("HHH", "AAA"), ("HHH", "BBB")], cancel_after=1)
        summary = crawl_progress.summary(self.conn, DATE)
        self.assertEqual(left, ["HHH:AAA"])
        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["planned"], 2)
        self.assertEqual(summary["remaining"], 1)
        self.assertEqual(summary["percent"], 50)
        self.assertEqual(summary["finished_at"], "")

    def test_fatal_storage_error_updates_persisted_state(self):
        with patch.object(db, "upsert_schedule", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.run_crawl([("AAA", "BBB")])
        self.assertEqual(crawl_progress.summary(self.conn, DATE)["state"], "failed")


if __name__ == "__main__":
    unittest.main()
