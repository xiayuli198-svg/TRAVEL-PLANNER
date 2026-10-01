"""城市停留动线（guide / B+）单测：散文 → 有序点位 → 市内腿 → 示意图。

三条底线：
1. **不猜**：对不上的名字保持纯文字，不当点位；
2. **不编时间**：腿只有真查到了才写分钟数，查不到就写「没取到」；
3. **顺序来自散文**：点位按文中出现的先后排，不按坐标远近重排。
全部离线跑（抓取器都是注入的假数据）。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from travel_planner import guide, poi

#: 一段真实的散文（哈尔滨 D1 的写法），用来验证对齐规则
HARBIN_D1 = ("9:00 地铁2号线中央大街站下，从经纬街口走到防洪纪念塔，1.5小时。"
             "转到圣索菲亚教堂看外观和广场，再步行到道里菜市场吃红肠、喝格瓦斯。"
             "中午在中央大街吃俄餐。下午沿斯大林公园走到松花江边，坐缆车过江上太阳岛。")


def _index(*items):
    return [{"name": n, "lon": lon, "lat": lat, "kind": kind, "rating": "",
             "address": "", "source": "测试", "curated": n}
            for n, lon, lat, kind in items]


class TestExtractStops(unittest.TestCase):
    def test_order_follows_the_prose(self):
        index = _index(("中央大街", 126.6189, 45.7739, "道路名"),
                       ("防洪纪念塔", 126.6172, 45.7807, "风景名胜"),
                       ("圣索菲亚教堂", 126.6272, 45.7701, "教堂"),
                       ("太阳岛", 126.5979, 45.7916, "风景名胜"))
        stops = guide.extract_stops(HARBIN_D1, index)
        self.assertEqual([s["name"] for s in stops],
                         ["中央大街", "防洪纪念塔", "圣索菲亚教堂", "太阳岛"],
                         "顺序应当是散文里的先后")

    def test_longest_name_wins(self):
        """「中央大街」和「中央大街步行街」同时在池子里时，别把长名拆成短名。"""
        index = _index(("中央大街", 126.6189, 45.7739, "道路名"),
                       ("中央大街步行街", 126.6190, 45.7740, "步行街"))
        stops = guide.extract_stops("先逛中央大街步行街", index)
        self.assertEqual([s["name"] for s in stops], ["中央大街步行街"])

    def test_unmatched_names_stay_text(self):
        """对不上就什么也不加 —— 不许拿个模糊的名字硬凑坐标。"""
        stops = guide.extract_stops("去老道外吃张包铺，晚上逛夜市", _index(
            ("中央大街", 126.6189, 45.7739, "道路名")))
        self.assertEqual(stops, [])

    def test_generic_words_are_not_stops(self):
        """「博物馆」「公园」这种泛词进池子会闹笑话（散文里到处都是）。"""
        index = _index(("博物馆", 126.64, 45.75, "博物馆"), ("公园", 126.60, 45.78, "公园"))
        self.assertEqual(guide.extract_stops("上午去博物馆，下午逛公园", index), [])

    def test_overlapping_tokens_are_not_double_counted(self):
        index = _index(("太阳岛", 126.5979, 45.7916, "风景名胜"),
                       ("太阳岛风景区", 126.5980, 45.7917, "风景名胜"))
        stops = guide.extract_stops("坐缆车过江上太阳岛风景区", index)
        self.assertEqual(len(stops), 1)


class TestPool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "poi.db"
        self.conn = guide.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_ensure_pool_uses_injected_fetcher(self):
        calls = []

        def fetch(city, code, page):
            calls.append((city, code, page))
            return [{"name": f"{city}-{code}-{page}", "lon": 126.6, "lat": 45.77,
                     "kind": "风景名胜", "type_full": "风景名胜;风景名胜", "address": "",
                     "rating": "4.5"}]

        out = guide.ensure_pool(self.conn, "哈尔滨", pages=1, delay=0, fetch=fetch)
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(calls), len(guide.POOL_TYPES), "4 个类型组各 1 页")
        self.assertEqual(out["pool"], len(guide.POOL_TYPES))
        again = guide.ensure_pool(self.conn, "哈尔滨", pages=1, delay=0, fetch=fetch)
        self.assertEqual(again["added"], 0, "池子有了就不重复抓")
        self.assertEqual(len(calls), len(guide.POOL_TYPES))

    def test_pool_merges_curated_names_first(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO city_poi(city,name,status,poi_name,lon,lat,kind,rating,address,"
                "source,fetched_at) VALUES('哈尔滨','中央大街','ok','中央大街',126.6189,45.7739,"
                "'道路名','4.8','','amap-place','2026-09-22')")
        guide.ensure_pool(self.conn, "哈尔滨", pages=1, delay=0, fetch=lambda c, code, p: [
            {"name": "中央大街", "lon": 1.0, "lat": 1.0, "kind": "", "type_full": "",
             "address": "", "rating": ""},
            {"name": "防洪纪念塔", "lon": 126.6172, "lat": 45.7807, "kind": "风景名胜",
             "type_full": "风景名胜;风景名胜", "address": "", "rating": "4.7"}])
        pool = {p["name"]: p for p in guide.pool_for(self.conn, "哈尔滨")}
        self.assertIn("中央大街", pool)
        self.assertEqual(pool["中央大街"]["lon"], 126.6189, "名单层的坐标优先")
        self.assertEqual(pool["中央大街"]["source"], "名单层")
        self.assertIn("防洪纪念塔", pool)

    def test_city_days_aligns_every_day(self):
        from travel_planner import catalog

        item = catalog.city("哈尔滨")
        self.assertIsNotNone(item)
        guide.ensure_pool(self.conn, "哈尔滨", pages=1, delay=0, fetch=lambda c, code, p: [
            {"name": n, "lon": lon, "lat": lat, "kind": k, "type_full": k,
             "address": "", "rating": ""}
            for n, lon, lat, k in (("中央大街", 126.6189, 45.7739, "风景名胜"),
                                   ("防洪纪念塔", 126.6172, 45.7807, "风景名胜"),
                                   ("圣索菲亚教堂", 126.6272, 45.7701, "教堂"),
                                   ("斯大林公园", 126.6128, 45.7794, "公园"),
                                   ("太阳岛", 126.5979, 45.7916, "风景名胜"),
                                   ("黑龙江省博物馆", 126.6409, 45.7576, "博物馆"),
                                   ("老道外", 126.6407, 45.7818, "街区"))])
        data = guide.city_days(self.conn, "哈尔滨")
        self.assertTrue(data["ok"], data)
        self.assertGreaterEqual(len(data["days"]), 1)
        self.assertTrue(any(d["drawable"] for d in data["days"]),
                        "至少有一天能对上 2 个点，否则画不出动线")
        self.assertGreater(data["matched_total"], 0)
        self.assertIn("自动对上", data["note"])


class TestLegs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "poi.db"
        self.conn = guide.connect(self.db)
        self.stops = [{"name": "中央大街", "lon": 126.6189, "lat": 45.7739, "kind": ""},
                      {"name": "冰雪大世界", "lon": 126.5625, "lat": 45.7770, "kind": ""},
                      {"name": "太阳岛", "lon": 126.5979, "lat": 45.7916, "kind": ""}]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_legs_are_cached_after_first_query(self):
        calls = []

        def fetch(city, a, b):
            calls.append((a["name"], b["name"]))
            return {"mode": "transit", "minutes": 32, "distance_m": 8000, "walk_m": 1158,
                    "lines": [{"line": "地铁2号线", "from": a["name"], "to": b["name"]}],
                    "straight_km": 5.0}

        first = guide.legs_for(self.conn, "哈尔滨", self.stops, fetch=fetch)
        self.assertEqual(len(first), 2)
        self.assertEqual(first[0]["minutes"], 32)
        self.assertFalse(first[0]["cached"])
        again = guide.legs_for(self.conn, "哈尔滨", self.stops, fetch=fetch)
        self.assertEqual(len(calls), 2, "第二次不该再查")
        self.assertTrue(all(item["cached"] for item in again))
        self.assertEqual(again[0]["lines"][0]["line"], "地铁2号线")

    def test_offline_mode_says_not_calculated(self):
        legs = guide.legs_for(self.conn, "哈尔滨", self.stops, live=False)
        self.assertEqual(len(legs), 2)
        for leg in legs:
            self.assertIsNone(leg["minutes"])
            self.assertIn("还没算", leg["note"])
            self.assertIsNotNone(leg["straight_km"], "至少给个直线距离")

    def test_failure_is_not_faked_as_a_time(self):
        def fetch(city, a, b):
            raise RuntimeError("模拟网络错误")

        legs = guide.legs_for(self.conn, "哈尔滨", self.stops, fetch=fetch, delay=0)
        self.assertIsNone(legs[0]["minutes"])
        self.assertIn("查询失败", legs[0]["note"])

    def test_empty_result_keeps_straight_distance(self):
        legs = guide.legs_for(self.conn, "哈尔滨", self.stops, fetch=lambda c, a, b: {},
                              delay=0)
        self.assertIsNone(legs[0]["minutes"])
        self.assertIn("没给出方案", legs[0]["note"])
        self.assertGreater(legs[0]["straight_km"], 0)


class TestSketch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = guide.connect(Path(self.tmp.name) / "poi.db")
        self.harbin_pool = [
            {"name": n, "lon": lon, "lat": lat, "kind": k, "type_full": k,
             "address": "", "rating": ""}
            for n, lon, lat, k in (("中央大街", 126.6189, 45.7739, "风景名胜"),
                                   ("防洪纪念塔", 126.6172, 45.7807, "风景名胜"),
                                   ("圣索菲亚教堂", 126.6272, 45.7701, "教堂"),
                                   ("斯大林公园", 126.6128, 45.7794, "公园"))]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_points_follow_real_geometry(self):
        stops = [{"name": "西", "lon": 126.50, "lat": 45.75},
                 {"name": "东", "lon": 126.70, "lat": 45.75},
                 {"name": "北", "lon": 126.60, "lat": 45.85}]
        pts = guide.sketch_points(stops)
        self.assertEqual(len(pts), 3)
        self.assertLess(pts[0]["x"], pts[1]["x"], "经度大的在右边")
        self.assertLess(pts[2]["y"], pts[0]["y"], "纬度大的在上边")
        self.assertTrue(all(0 <= p["x"] <= 520 and 0 <= p["y"] <= 240 for p in pts))

    def test_svg_has_stops_legs_and_minutes(self):
        stops = [{"name": "中央大街", "lon": 126.6189, "lat": 45.7739},
                 {"name": "冰雪大世界", "lon": 126.5625, "lat": 45.7770}]
        svg = guide.sketch_svg(stops, [{"mode": "transit", "minutes": 32}])
        self.assertIn("<svg", svg)
        self.assertIn("32′", svg, "有腿就把分钟数标在线上")
        self.assertEqual(svg.count("<circle"), 2)

    def test_single_stop_has_no_sketch(self):
        self.assertEqual(guide.sketch_svg([{"name": "x", "lon": 1, "lat": 1}]), "")

    def test_day_view_wires_everything_together(self):
        from travel_planner import catalog

        item = catalog.city("哈尔滨")
        day = ((item.get("guide") or {}).get("itinerary") or [{}])[0].get("day") or "D1"
        guide.ensure_pool(self.conn, "哈尔滨", pages=1, delay=0,
                          fetch=lambda c, code, p: self.harbin_pool)
        view = guide.day_view(self.conn, "哈尔滨", day, live=False)
        self.assertTrue(view["ok"], view)
        self.assertGreaterEqual(len(view["stops"]), 2, view.get("detail", "")[:60])
        self.assertIn("<svg", view["svg"])
        self.assertIn("不是时刻表", view["note"])
        self.assertIn("不做导航", view["note"])

    def test_day_view_rejects_unknown_day(self):
        view = guide.day_view(self.conn, "哈尔滨", "D9", live=False)
        self.assertFalse(view["ok"])
        self.assertIn("D9", view["error"])


if __name__ == "__main__":
    unittest.main()
