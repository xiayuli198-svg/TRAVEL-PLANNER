"""复制时刻表到新日期的安全性与便捷返回值测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from travel_planner import db, service


class TestCopyDate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        self.conn.executemany(
            "INSERT INTO schedules(train_no,train_code,date,seq,station_code,"
            "station_name,day,arr,dep) VALUES(?,?,?,?,?,?,?,?,?)",
            [
                ("G1", "G1", "2026-09-09", 1, "BJP", "北京", 0, "", "08:00"),
                ("G1", "G1", "2026-09-09", 2, "SHH", "上海", 0, "12:00", ""),
                ("D2", "D2", "2026-09-09", 1, "BJP", "北京", 0, "", "09:00"),
            ],
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def rows(self, date):
        return self.conn.execute(
            "SELECT train_no,seq FROM schedules WHERE date=? ORDER BY train_no,seq",
            (date,),
        ).fetchall()

    def test_copy_to_empty_date(self):
        result = service.copy_date(self.conn, "2026-09-09", "2026-09-10")
        self.assertTrue(result["ok"])
        self.assertEqual(result["rows"], 3)
        self.assertEqual(result["trains"], 2)
        self.assertEqual(result["source_date"], "2026-09-09")
        self.assertEqual(len(self.rows("2026-09-10")), 3)

    def test_existing_target_is_protected_by_default(self):
        self.conn.execute(
            "INSERT INTO schedules(train_no,train_code,date,seq,station_code,"
            "station_name,day,arr,dep) VALUES('OLD','OLD','2026-09-10',1,'X','旧车',0,'','10:00')"
        )
        self.conn.commit()
        result = service.copy_date(self.conn, "2026-09-09", "2026-09-10")
        self.assertFalse(result["ok"])
        self.assertTrue(result["target_exists"])
        self.assertEqual(result["target_rows"], 1)
        self.assertEqual([tuple(r) for r in self.rows("2026-09-10")], [("OLD", 1)])

    def test_overwrite_replaces_existing_target(self):
        self.conn.execute(
            "INSERT INTO schedules(train_no,train_code,date,seq,station_code,"
            "station_name,day,arr,dep) VALUES('OLD','OLD','2026-09-10',1,'X','旧车',0,'','10:00')"
        )
        self.conn.commit()
        result = service.copy_date(self.conn, "2026-09-09", "2026-09-10", overwrite=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["overwritten"])
        self.assertEqual(len(self.rows("2026-09-10")), 3)
        self.assertNotIn(("OLD", 1), [tuple(r) for r in self.rows("2026-09-10")])

    def test_same_date_and_invalid_date_are_safe(self):
        before = self.rows("2026-09-09")
        same = service.copy_date(self.conn, "2026-09-09", "2026-09-09")
        self.assertFalse(same["ok"])
        self.assertIn("不能相同", same["error"])
        self.assertEqual(self.rows("2026-09-09"), before)
        invalid = service.copy_date(self.conn, "2026/09/09", "2026-09-10")
        self.assertFalse(invalid["ok"])
        self.assertIn("YYYY-MM-DD", invalid["error"])

    def test_batch_copy_skips_existing_and_supports_dry_run(self):
        self.conn.execute(
            "INSERT INTO schedules(train_no,train_code,date,seq,station_code,"
            "station_name,day,arr,dep) VALUES('OLD','OLD','2026-09-11',1,'X','旧车',0,'','10:00')"
        )
        self.conn.commit()
        preview = service.copy_dates(
            self.conn, "2026-09-09", ["2026-09-10", "2026-09-11"], dry_run=True
        )
        self.assertTrue(preview["ok"])
        self.assertEqual([x["status"] for x in preview["results"]], ["would_copy", "skipped"])
        self.assertEqual(len(self.rows("2026-09-10")), 0)
        result = service.copy_dates(self.conn, "2026-09-09", ["2026-09-10", "2026-09-11"])
        self.assertEqual([x["status"] for x in result["results"]], ["copied", "skipped"])
        self.assertEqual(len(self.rows("2026-09-10")), 3)
        self.assertEqual(len(self.rows("2026-09-11")), 1)


if __name__ == "__main__":
    unittest.main()
