"""数据库层测试：混合步行边构建与航班 Trip 加载（用临时 SQLite 文件）。"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
import json
from pathlib import Path

from travel_planner import db
from travel_planner.ingest.fliggy import repair_airport_cities


class TestMixedFootpaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        self.conn.executemany(
            "INSERT INTO stations(code,name,city,ordinal) VALUES(?,?,?,?)",
            [("VNP", "北京南", "北京", 0), ("BJP", "北京", "北京", 1),
             ("SHH", "上海", "上海", 2), ("DLM", "大理", "大理", 3),
             ("HQG", "鹤庆", "大理", 4)])
        self.conn.executemany(
            "INSERT INTO airports(code,name,city) VALUES(?,?,?)",
            [("PKX", "大兴国际机场", "北京"), ("PVG", "浦东国际机场", "上海")])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_air_rail_same_city_edges(self):
        fp = db.build_mixed_footpaths(
            self.conn, ["VNP", "BJP", "SHH", "DLM", "HQG"],
            [db.air_code("PKX"), db.air_code("PVG")])
        vnp = dict(fp["VNP"])
        self.assertIn(db.air_code("PKX"), vnp)          # 北京南 ↔ 大兴机场
        self.assertEqual(vnp[db.air_code("PKX")], db.AIR_RAIL_CITY_MIN)
        pkx = dict(fp[db.air_code("PKX")])
        self.assertIn("VNP", pkx)                       # 反向边
        pvg = dict(fp[db.air_code("PVG")])
        self.assertIn("SHH", pvg)                       # 浦东 ↔ 上海站
        self.assertNotIn(db.air_code("PVG"), dict(fp["VNP"]))  # 北京南 ↔ 浦东（异城）无直连
        # 机场同场自环
        self.assertEqual(dict(fp[db.air_code("PKX")])[db.air_code("PKX")], db.AIR_MCT_MIN)

    def test_county_station_not_same_city(self):
        # 鹤庆(城市=大理) 与 大理站 站名不含城市名 → 无同城铁路边
        fp = db.build_mixed_footpaths(self.conn, ["DLM", "HQG"], [])
        self.assertNotIn("DLM", dict(fp["HQG"]))

    def test_load_flights_trip_shape(self):
        self.conn.execute(
            "INSERT INTO flights(flight_no,airline,date,seq,dep_airport,arr_airport,"
            "dep,arr,arr_day) VALUES('MU5100','东航','2026-09-09',1,'PEK','PVG',"
            "'07:00','08:55',0)")
        self.conn.commit()
        trips = db.load_flights(self.conn, "2026-09-09")
        self.assertEqual(len(trips), 1)
        t = trips[0]
        self.assertEqual(t.code, "MU5100")
        self.assertEqual(t.stations, [db.air_code("PEK"), db.air_code("PVG")])
        self.assertEqual((t.dep[0], t.arr[-1]), (420, 535))

    def test_load_flights_multi_segment(self):
        self.conn.executemany(
            "INSERT INTO flights(flight_no,airline,date,seq,dep_airport,arr_airport,"
            "dep,arr,arr_day) VALUES(?,?,?,?,?,?,?,?,?)",
            [("KN1", "中联航", "2026-09-09", 1, "PKX", "XIY", "08:00", "10:00", 0),
             ("KN1", "中联航", "2026-09-09", 2, "XIY", "PVG", "11:00", "13:00", 0)])
        self.conn.commit()
        trips = db.load_flights(self.conn, "2026-09-09")
        self.assertEqual(len(trips), 1)
        t = trips[0]
        # 经停：PKX(8:00) -> XIY(10:00 到/11:00 发) -> PVG(13:00)
        self.assertEqual(t.stations,
                         [db.air_code("PKX"), db.air_code("XIY"), db.air_code("PVG")])
        self.assertEqual(t.arr[1], 600)
        self.assertEqual(t.dep[1], 660)
        self.assertEqual(t.arr[2], 780)

    def test_load_flights_route_filter(self):
        self.conn.executemany(
            "INSERT INTO flights(flight_no,airline,date,seq,dep_airport,arr_airport,"
            "dep,arr,arr_day) VALUES(?,?,?,?,?,?,?,?,?)",
            [("MU1", "东航", "2026-09-09", 1, "PEK", "PVG", "07:00", "09:00", 0),
             ("MU2", "东航", "2026-09-09", 1, "PKX", "XIY", "08:00", "10:00", 0)])
        self.conn.commit()
        trips = db.load_flights(self.conn, "2026-09-09",
                                [db.air_code("PEK")], [db.air_code("PVG")])
        self.assertEqual([t.code for t in trips], ["MU1"])

    def test_repair_airport_city_uses_route_endpoints(self):
        raw = [{"flight_no": "MU1", "segments": [
            {"dep_airport": "PEK", "arr_airport": "XIY", "dep_name": "首都", "arr_name": "咸阳"},
            {"dep_airport": "XIY", "arr_airport": "PVG", "dep_name": "咸阳", "arr_name": "浦东"},
        ]}]
        self.conn.execute("INSERT INTO flight_route_cache(origin,destination,date,fetched_at,raw) VALUES(?,?,?,?,?)",
                          ("北京", "上海", "2026-09-09", "now", json.dumps(raw, ensure_ascii=False)))
        self.conn.execute("INSERT INTO airports(code,name,city) VALUES('XIY','咸阳','北京')")
        self.conn.commit()
        changed = repair_airport_cities(self.conn)
        self.assertEqual(changed, 1)  # PEK inserted; PVG already exists, XIY is a stopover
        self.assertEqual(self.conn.execute("SELECT city FROM airports WHERE code='PEK'").fetchone()[0], "北京")
        self.assertEqual(self.conn.execute("SELECT city FROM airports WHERE code='PVG'").fetchone()[0], "上海")

    def test_import_airports_master_merges_and_preserves_dynamic(self):
        self.conn.execute("INSERT INTO airports(code,name,city) VALUES('ABC','旧机场名','测试城')")
        count = db.import_airports_master(self.conn, [{
            "code": "PKX", "icao": "ZBAD", "name": "北京大兴国际机场",
            "name_en": "Beijing Daxing International Airport", "city": "北京",
            "province": "北京", "country": "中国", "lat": "39.5099",
            "lon": "116.4105", "active": "1", "kind": "civil",
        }])
        self.assertEqual(count, 1)
        row = self.conn.execute(
            "SELECT name,city,icao,source,active FROM airports WHERE code='PKX'"
        ).fetchone()
        self.assertEqual(tuple(row), ("北京大兴国际机场", "北京", "ZBAD", "master", 1))
        self.assertEqual(
            self.conn.execute("SELECT source FROM airports WHERE code='ABC'").fetchone()[0],
            "dynamic",
        )


if __name__ == "__main__":
    unittest.main()
