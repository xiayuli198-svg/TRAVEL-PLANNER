"""定向爬取（od_focus）单测：目标选择、OD 优先级、历史记录、「主动学习」建议。

保护的核心行为：
1. 只爬跟目标城市相关的 OD —— 全量 6006 对，定向要小一个数量级。
2. 主站优先（北京站不能因为郊区车场车次多而被挤出组合）。
3. 历史记录能写进 search_log，并反哺下一次的爬取目标。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from travel_planner import db, od_focus as od


def _seed_stations(conn, city: str, codes: list[tuple[str, str]]) -> None:
    with conn:
        for code, name in codes:
            conn.execute("INSERT INTO stations(code,name,city,ordinal) VALUES(?,?,?,0)",
                         (code, name, city))


def _seed_trains(conn, date: str, code: str, n: int) -> None:
    with conn:
        for i in range(n):
            no = f"{code}{i:04d}"
            conn.execute(
                "INSERT INTO schedules(train_no,train_code,date,seq,station_code,station_name,"
                "day,arr,dep) VALUES(?,?,?,1,?,?,0,'','08:00')",
                (no, no, date, code, code))


class FocusBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        # 两个目标城市 + 一个枢纽城市
        _seed_stations(self.conn, "北京", [("BJP", "北京"), ("BXP", "北京北"), ("VNP", "北京东")])
        _seed_stations(self.conn, "上海", [("SHH", "上海"), ("AOH", "上海虹桥")])
        _seed_stations(self.conn, "郑州", [("ZZF", "郑州东")])
        _seed_trains(self.conn, "2026-06-17", "BJP", 30)
        _seed_trains(self.conn, "2026-06-17", "BXP", 5)
        _seed_trains(self.conn, "2026-06-17", "VNP", 2)
        _seed_trains(self.conn, "2026-06-17", "SHH", 20)
        _seed_trains(self.conn, "2026-06-17", "AOH", 25)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class TestStationPick(FocusBase):
    def test_main_station_first(self):
        codes = [r["code"] for r in od.stations_of(self.conn, "北京")]
        self.assertEqual(codes[0], "BJP", "主站（北京）必须排第一")
        # 上海虹桥车次比上海站多，但没有同名主站时按车次排
        sh = [r["code"] for r in od.stations_of(self.conn, "上海")]
        self.assertEqual(sh[0], "SHH", "同名主站优先于车次更多的虹桥")

    def test_limit(self):
        self.assertEqual(len(od.stations_of(self.conn, "北京", limit=2)), 2)


class TestFocusPairs(FocusBase):
    def test_direct_pairs_first(self):
        info = od.focus_pairs(self.conn, ["北京", "上海"], hubs=["ZZF"])
        pairs = info["pairs"]
        self.assertIn(("BJP", "SHH"), pairs[:6], "两市直达对应当排在最前")
        self.assertIn(("SHH", "BJP"), pairs[:6])
        self.assertIn(("BJP", "ZZF"), pairs, "目标城市 ↔ 枢纽也要有（转车用）")
        self.assertIn(("ZZF", "SHH"), pairs)

    def test_much_smaller_than_full_grid(self):
        info = od.focus_pairs(self.conn, ["北京", "上海"], hubs=["ZZF"])
        self.assertLess(len(info["pairs"]), 100, "两个城市的定向 OD 不该接近全量")
        self.assertGreater(len(info["pairs"]), 0)

    def test_cap_and_report(self):
        info = od.focus_pairs(self.conn, ["北京", "上海"], hubs=["ZZF"], max_pairs=10)
        self.assertEqual(len(info["pairs"]), 10)
        self.assertGreater(info["truncated"], 0, "被截掉多少要说清楚")

    def test_internal_city_pairs(self):
        info = od.focus_pairs(self.conn, ["北京"], hubs=[])
        pairs = set(info["pairs"])
        self.assertIn(("BJP", "BXP"), pairs, "同城各站互连也要有（站间换乘）")

    def test_unknown_city(self):
        info = od.focus_pairs(self.conn, ["不存在的地方"])
        self.assertTrue(info["error"])
        self.assertEqual(info["pairs"], [])

    def test_describe(self):
        info = od.focus_pairs(self.conn, ["北京", "上海"], hubs=["ZZF"])
        text = od.describe_focus(info)
        self.assertIn("北京", text)
        self.assertIn("6006", text, "要说明全省对比，用户才知道省了多少")


class TestLearning(FocusBase):
    def test_remember_and_recent(self):
        self.assertTrue(od.remember_search(self.conn, "北京", "上海", "2026-10-01", "mixed"))
        self.assertTrue(od.remember_search(self.conn, "拉萨", "成都"))
        recent = od.recent_targets(self.conn)
        self.assertIn("北京", recent)
        self.assertIn("拉萨", recent)
        self.assertIn("成都", recent)

    def test_suggest_merges_plan_and_history(self):
        od.remember_search(self.conn, "成都", "拉萨")
        sug = od.suggest_targets(self.conn, ["广州"])
        self.assertIn("广州", sug["from_plan"])
        self.assertIn("成都", sug["from_history"])
        self.assertTrue(sug["hubs"], "建议里要带全国枢纽")

    def test_focused_pairs_uses_history(self):
        od.remember_search(self.conn, "上海", "郑州")
        info = od.focused_pairs_for_plan(self.conn, ["北京"])
        self.assertIn("北京", info["cities_used"])
        self.assertIn("上海", info["cities_used"], "历史目标要参与定向组合")
        self.assertIn("suggest", info)

    def test_legacy_table_gets_columns(self):
        """早期建错的 search_log（列名 to/date）也要能自动补列。"""
        with self.conn:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS search_log(id INTEGER PRIMARY KEY, frm TEXT, "
                "\"to\" TEXT, \"date\" TEXT, mode TEXT, created_at TEXT)")
        self.assertTrue(od.remember_search(self.conn, "北京", "上海"))
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(search_log)")}
        self.assertIn("dest", cols)
        self.assertIn("travel_date", cols)


if __name__ == "__main__":
    unittest.main()
