"""游览模式的串城标签与航班展示回归测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from travel_planner import db, service
from travel_planner.engine.model import Journey, Leg


class TestTourChain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        self.conn.executemany(
            "INSERT INTO stations(code,name,city,ordinal) VALUES(?,?,?,?)",
            [("BJP", "北京", "北京", 0),
             ("VNP", "北京南", "北京", 1),
             ("LXA", "拉萨", "拉萨", 2)],
        )
        self.conn.executemany(
            "INSERT INTO airports(code,name,city) VALUES(?,?,?)",
            [("PKX", "大兴国际机场", "北京"),
             ("LXA", "贡嘎机场", "拉萨")],
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_airport_nodes_are_not_collapsed_with_same_city_rail(self):
        # 北京铁路小站应折叠，但北京机场必须保留，供后续 plan_leg
        # 产生机场↔车站接驳及航班段。
        j = Journey([
            Leg("r1", "K1", "BJP", "", "VNP", "", 600, 660),
            Leg("f1", "MU1", "AIR:PKX", "", "AIR:LXA", "", 800, 940),
            Leg("r2", "Z1", "LXA", "", "BJP", "", 1000, 1200),
        ], 600, 1200)
        names = {"BJP": "北京", "VNP": "北京南", "AIR:PKX": "大兴国际机场",
                 "AIR:LXA": "贡嘎机场", "LXA": "拉萨"}
        labels = service._tour_chain_labels(self.conn, names, j)
        self.assertEqual(labels, ["北京", "大兴国际机场", "贡嘎机场", "拉萨", "北京"])


if __name__ == "__main__":
    unittest.main()
