"""「我的行程」单测：存档、去重、排不出的留痕、批量建档记账。

重点：
1. 同一路线 + 同一天 + 同一套偏好只存一份（重排是覆盖，不是堆积）；
2. **排不出的条目不能被静默丢掉** —— 必须进 ``trip_skips`` 并说清原因，
   因为这正是路线库里「同城选站 / 没有铁路站的小镇」那批条目的体检结果；
3. 批量建档只在本地时刻表上读、写只落 trips.db（不联网、不碰 12306）。
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from travel_planner import db, routes as R, trips


def _seed_timetable(conn) -> None:
    """一个最小时刻表：G1 北京 → 南京 → 上海（2026-06-17）。"""
    with conn:
        for code, name, city in (("BJP", "北京", "北京"), ("NJH", "南京", "南京"),
                                 ("SHH", "上海", "上海")):
            conn.execute("INSERT INTO stations(code,name,city,ordinal) VALUES(?,?,?,0)",
                         (code, name, city))
        for seq, code, name, arr, dep in ((1, "BJP", "北京", "", "08:00"),
                                          (2, "NJH", "南京", "12:00", "12:10"),
                                          (3, "SHH", "上海", "14:00", "")):
            conn.execute(
                "INSERT INTO schedules(train_no,train_code,date,seq,station_code,"
                "station_name,day,arr,dep) VALUES('T1','G1','2026-06-17',?,?,?,0,?,?)",
                (seq, code, name, arr, dep))
    db.set_meta(conn, "import:2026-06-17", "test")


class TripCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tt_path = root / "timetable.db"
        self.trip_path = root / "trips.db"
        self.tt = db.connect(self.tt_path)
        _seed_timetable(self.tt)
        self.store = trips.connect(self.trip_path)

    def tearDown(self):
        self.tt.close()
        self.store.close()
        self.tmp.cleanup()

    @staticmethod
    def route(segments=None, **over):
        item = {"id": "t-bj-sh", "name": "自测·北京到上海", "kind": "oneway",
                "from_city": "北京", "to_city": "上海",
                "segments": segments or [{"frm": "北京", "to": "上海", "mode": "high_speed",
                                          "hours": 6, "price": "约 550 元"}],
                "total_hours": 6}
        item.update(over)
        return item


class TestSaveAndList(TripCase):
    def test_save_then_list_and_stats(self):
        out = trips.save_route(self.tt, self.store, self.route(), "2026-06-17")
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["created"], "created")
        listing = trips.list_saved(self.store)
        self.assertEqual(listing["total"], 1)
        item = listing["trips"][0]
        self.assertEqual(item["route_id"], "t-bj-sh")
        self.assertEqual(item["stops"], ["北京", "上海"])
        self.assertGreaterEqual(item["ride_count"], 1)
        self.assertNotIn("payload", item, "列表不该带完整方案（列表要轻）")
        stats = trips.stats(self.store)
        self.assertEqual(stats["trips"], 1)
        self.assertEqual(stats["oneway"], 1)

    def test_detail_keeps_full_plan_and_source(self):
        out = trips.save_route(self.tt, self.store, self.route(), "2026-06-17")
        plan = trips.get(self.store, out["id"])["plan"]
        self.assertTrue(plan["ok"])
        self.assertIn("data_source", plan)
        self.assertTrue(plan["note"])
        codes = [l.get("code") for day in plan["days"]
                 for l in (day.get("journey") or {}).get("legs") or []]
        self.assertIn("G1", codes)

    def test_same_route_same_day_is_one_row(self):
        first = trips.save_route(self.tt, self.store, self.route(), "2026-06-17")
        again = trips.save_route(self.tt, self.store, self.route(), "2026-06-17", replace=False)
        self.assertTrue(again.get("skipped"), again)
        self.assertEqual(trips.list_saved(self.store)["total"], 1)
        # 换偏好就是另一份：停留方式不同，行程也不同，必须能分开存
        other = trips.save_route(self.tt, self.store, self.route(), "2026-06-17",
                                 replace=False, stay_mode="two_nights")
        self.assertTrue(other["ok"] and not other.get("skipped"), other)
        listing = trips.list_saved(self.store)
        self.assertEqual(listing["total"], 2)
        self.assertNotEqual(listing["trips"][0]["id"], first["id"])

    def test_replace_updates_in_place(self):
        first = trips.save_route(self.tt, self.store, self.route(), "2026-06-17")
        again = trips.save_route(self.tt, self.store, self.route(), "2026-06-17", replace=True)
        self.assertEqual(again["created"], "updated")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(trips.list_saved(self.store)["total"], 1)

    def test_different_day_is_another_row(self):
        trips.save_route(self.tt, self.store, self.route(), "2026-06-17")
        trips.save_route(self.tt, self.store, self.route(), "2026-06-18")
        self.assertEqual(trips.list_saved(self.store)["total"], 2)
        self.assertEqual(trips.list_saved(self.store, date="2026-06-18")["total"], 1)

    def test_loop_is_marked_as_loop(self):
        loop = self.route([
            {"frm": "北京", "to": "南京", "mode": "high_speed", "hours": 4, "price": "约 400 元"},
            {"frm": "南京", "to": "北京", "mode": "high_speed", "hours": 4, "price": "约 400 元"}],
            id="t-loop", kind="loop", loop=True)
        out = trips.save_route(self.tt, self.store, loop, "2026-06-17")
        self.assertTrue(out["ok"], out.get("error"))
        listing = trips.list_saved(self.store, kind="loop")
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["trips"][0]["kind_label"], "环线")

    def test_ground_connector_is_recorded(self):
        """含地面接驳的存档要标出来：铁路段已核实，接驳段是估算。"""
        route = self.route([
            {"frm": "北京", "to": "南京", "mode": "high_speed", "hours": 4, "price": "约 400 元"},
            {"frm": "南京", "to": "婺源", "mode": "bus", "hours": 3.5,
             "price": "约 90 元", "note": "景区直通车"}], id="t-ground", total_hours=8)
        out = trips.save_route(self.tt, self.store, route, "2026-06-17")
        self.assertTrue(out["ok"], out.get("error"))
        item = trips.list_saved(self.store)["trips"][0]
        self.assertEqual(item["ground_count"], 1)
        self.assertEqual(item["status"], "partial")
        self.assertIn("地面接驳", item["status_label"])
        self.assertIn("地面接驳", trips.get(self.store, out["id"])["plan"]["note"])
        self.assertEqual(trips.stats(self.store)["partial"], 1)


class TestSkips(TripCase):
    """排不出来的条目要留痕：数据台据此列出「路线库里排不出行程的那些」。"""

    def test_single_city_route_is_recorded(self):
        city = self.route([{"frm": "南京", "to": "南京", "mode": "metro",
                            "hours": 0.5, "price": "约 5 元"}], id="t-city", kind="loop")
        out = trips.save_route(self.tt, self.store, city, "2026-06-17")
        self.assertFalse(out["ok"])
        rows = trips.skips(self.store)
        self.assertEqual([r["route_id"] for r in rows], ["t-city"])
        self.assertIn("一个城市", rows[0]["reason"])
        self.assertEqual(trips.list_saved(self.store)["total"], 0)

    def test_unknown_stop_reason_names_the_stop(self):
        bad = self.route([{"frm": "北京", "to": "黑马河", "mode": "bus",
                           "hours": 3, "price": "约 40 元"}], id="t-nostation")
        out = trips.save_route(self.tt, self.store, bad, "2026-06-17")
        self.assertFalse(out["ok"])
        self.assertIn("黑马河", out["error"])
        self.assertIn("黑马河", trips.skips(self.store)[0]["reason"])

    def test_skip_is_cleared_once_it_can_be_planned(self):
        """先排不出、后来数据补上能排出来了，旧的白名单不能一直挂着。"""
        route = self.route(id="t-flaky")
        trips.remember_skip(self.store, "t-flaky", "自测·北京到上海", "当时没数据")
        self.assertEqual(len(trips.skips(self.store)), 1)
        out = trips.save_route(self.tt, self.store, route, "2026-06-17")
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(trips.skips(self.store), [])

    def test_skip_reasons_are_classified(self):
        """47 条排不出的不能堆成一坨：按原因归类，页面才能分组显示。"""
        base = self.route([{"frm": "北京", "to": "黑马河", "mode": "bus",
                            "hours": 3, "price": "约 40 元"}], id="t-base")
        out = trips.save_route(self.tt, self.store, base, "2026-06-17")
        self.assertEqual(out["fail_kind"], "base")
        self.assertEqual(trips.skips(self.store)[0]["fail_label"],
                         trips.SKIP_LABELS["base"])

        single = self.route([{"frm": "南京", "to": "南京", "mode": "metro",
                              "hours": 0.5, "price": "约 5 元"}], id="t-city", kind="loop")
        trips.save_route(self.tt, self.store, single, "2026-06-17")
        kinds = {r["route_id"]: r["fail_kind"] for r in trips.skips(self.store)}
        self.assertEqual(kinds["t-city"], "single_city")

        # 反向（测试库里只有 北京→上海 的单向车）→ 缺这一天的车次数据
        reverse = self.route([{"frm": "上海", "to": "北京", "mode": "high_speed",
                               "hours": 6, "price": "约 550 元"}], id="t-reverse")
        out3 = trips.save_route(self.tt, self.store, reverse, "2026-06-17")
        self.assertEqual(out3["fail_kind"], "no_data")
        groups = {g["label"]: g["count"] for g in trips.coverage(self.store)["skip_groups"]}
        self.assertEqual(groups[trips.SKIP_LABELS["base"]], 1)
        self.assertEqual(groups[trips.SKIP_LABELS["no_data"]], 1)


class TestMaintenance(TripCase):
    def test_rename_delete_and_export(self):
        out = trips.save_route(self.tt, self.store, self.route(), "2026-06-17")
        self.assertTrue(trips.rename(self.store, out["id"], "国庆北京到上海")["ok"])
        self.assertEqual(trips.get(self.store, out["id"])["name"], "国庆北京到上海")
        self.assertFalse(trips.rename(self.store, out["id"], "  ")["ok"])
        blob = json.loads(trips.export_json(self.store))
        self.assertEqual(len(blob["trips"]), 1)
        self.assertIn("days", blob["trips"][0]["plan"])
        self.assertTrue(trips.remove(self.store, out["id"])["ok"])
        self.assertIsNone(trips.get(self.store, out["id"]))
        self.assertFalse(trips.remove(self.store, out["id"])["ok"])

    def test_search_and_paging(self):
        for i in range(5):
            route = self.route(id=f"t-{i}", name=f"自测·北京到上海 {i}")
            trips.save_route(self.tt, self.store, route, "2026-06-17", replace=False,
                             stay_mode=["transit", "one_night", "two_nights"][i % 3])
        page = trips.list_saved(self.store, limit=2)
        self.assertEqual(page["total"], 5)
        self.assertEqual(len(page["trips"]), 2)
        self.assertTrue(page["has_more"])
        self.assertEqual(len(trips.list_saved(self.store, limit=2, skip=4)["trips"]), 1)
        self.assertEqual(trips.list_saved(self.store, q="北京到上海 3")["total"], 1)

    def test_coverage_counts_only_library_routes(self):
        """覆盖率是「路线库」的覆盖率：自测路线（不在库里）不该算进去。"""
        library = R.query()
        cov = trips.coverage(self.store)
        self.assertEqual(cov["routes"], len(library))
        self.assertEqual(cov["saved"], 0)
        self.assertEqual(cov["pending"], len(library))
        trips.save_route(self.tt, self.store, self.route(id="t-not-in-library"), "2026-06-17")
        self.assertEqual(trips.coverage(self.store)["saved"], 0)
        first = library[0]
        trips.remember_skip(self.store, first["id"], first.get("name") or "", "测试")
        after = trips.coverage(self.store)
        self.assertEqual(after["skipped"], 1)
        self.assertEqual(after["pending"], len(library) - 1)
        self.assertEqual(after["skips"][0]["route_id"], first["id"])


class TestBatchBuild(TripCase):
    """批量建档：进度、记账、取消 —— 用临时库跑，不依赖真实的 467MB 时刻表。"""

    def _ids(self, count=4):
        return [r["id"] for r in R.query()[:count]]

    def test_batch_job_books_every_route(self):
        ids = self._ids(4)
        out = trips.BUILD.start("2026-06-17", ids=ids, limit=4,
                               db_path=str(self.tt_path), trip_db=str(self.trip_path))
        self.assertTrue(out["ok"], out)
        job = trips.BUILD.current()
        self.assertTrue(trips.wait_for(job, timeout=120))
        snap = job.snapshot()
        self.assertEqual(snap["total"], len(ids))
        self.assertEqual(snap["done"], len(ids))
        self.assertEqual(snap["saved"] + snap["failed"] + snap["skipped"], len(ids))
        self.assertEqual(snap["state"], "done")
        # 临时时刻表里没有这些城市的车次：应当记成「排不出」，不许假成功
        self.assertEqual(snap["saved"], 0)
        self.assertEqual(snap["failed"], len(ids))
        self.assertTrue(snap["failures"])
        self.assertEqual(len(trips.skips(self.store)), len(ids))
        self.assertEqual(trips.stats(self.store)["last_build"]["total"], len(ids))

    def test_running_job_refuses_a_second_start(self):
        """同一时刻只允许一个批量建档任务（防止重复写 trips.db）。"""
        class FakeJob:
            state = trips.STATE_RUNNING

            def snapshot(self):
                return {"state": self.state, "date": "2026-06-17"}

        original = trips.BUILD._current
        trips.BUILD._current = FakeJob()                       # type: ignore[assignment]
        try:
            out = trips.BUILD.start("2026-06-17", ids=["x"],
                                    db_path=str(self.tt_path), trip_db=str(self.trip_path))
        finally:
            trips.BUILD._current = original                    # type: ignore[assignment]
        self.assertFalse(out["ok"])
        self.assertIn("在跑", out["error"])

    def test_cancel_stops_the_loop(self):
        ids = self._ids(120)
        job = trips.TripBuildJob("2026-06-17", ids=ids,
                                 db_path=str(self.tt_path), trip_db=str(self.trip_path))
        job._cancel.set()                                       # 起跑前就要求取消
        job.start()
        self.assertTrue(trips.wait_for(job, timeout=120))
        snap = job.snapshot()
        self.assertEqual(snap["state"], "cancelled")
        self.assertEqual(snap["saved"], 0)
        self.assertTrue(threading.current_thread().is_alive())


if __name__ == "__main__":
    unittest.main()
