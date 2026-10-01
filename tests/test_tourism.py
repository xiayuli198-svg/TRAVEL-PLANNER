import tempfile
import unittest
from pathlib import Path

from travel_planner import tourism


class TestTourismDb(unittest.TestCase):
    def test_seed_and_custom_upsert(self):
        with tempfile.TemporaryDirectory() as d:
            conn = tourism.connect(Path(d) / "tourism.db")
            self.assertEqual(tourism.get(conn, "北京") ["city"], "北京")
            tourism.upsert(conn, "测试市", 12, "简介", "半天", "标签")
            item = tourism.get(conn, "测试市")
            self.assertEqual(item["recommendation_score"], 10.0)
            self.assertEqual(item["halfday_plan"], "半天")
            conn.close()


if __name__ == "__main__":
    unittest.main()
