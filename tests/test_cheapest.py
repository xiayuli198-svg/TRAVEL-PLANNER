"""限时最省引擎 + 票价估算 单元测试。"""
from __future__ import annotations

import unittest

from travel_planner.engine import fare
from travel_planner.engine.cheapest import plan_cheapest
from travel_planner.engine.model import Trip


def T(trip_id: str, code: str, *stops) -> Trip:
    stations = [s for s, _, _ in stops]
    arr = [a for _, a, _ in stops]
    dep = [d for _, _, d in stops]
    return Trip(trip_id, code, stations, arr, dep)


def FP(**pairs) -> dict:
    return {k: list(v) for k, v in pairs.items()}


class TestFareRates(unittest.TestCase):
    def test_rates(self):
        self.assertAlmostEqual(fare.rail_rate("G1"), 0.42)
        self.assertAlmostEqual(fare.rail_rate("K123"), 0.105)
        self.assertAlmostEqual(fare.rail_rate("G1", "一等"), 0.70)
        self.assertAlmostEqual(fare.rail_rate("D7", "二等"), 0.31)

    def test_haversine(self):
        # 北京-上海 约 1067 km
        km = fare.haversine_km(116.40, 39.90, 121.47, 31.23)
        self.assertGreater(km, 1000)
        self.assertLess(km, 1120)

    def test_segment_price(self):
        p = fare.rail_segment_price("G1", 1000)
        self.assertAlmostEqual(p, 1000 * 1.22 * 0.42, places=1)


class TestPlanCheapest(unittest.TestCase):
    def _trips(self):
        # 直达高铁：贵且快；普速两段绕行：便宜
        return [
            T("g1", "G1", ("A", 480, 480), ("B", 660, 660)),      # A 08:00→B 11:00
            T("k1", "K1", ("A", 480, 480), ("X", 840, 840)),      # A →X 14:00
            T("k2", "K2", ("X", 900, 900), ("B", 1200, 1200)),    # X →B 20:00（需换乘等待60min）
        ]

    def _price_of(self, ride_prices):
        return lambda ti, i, j: ride_prices[ti]

    def test_cheapest_prefers_slow_cheap(self):
        trips = self._trips()
        fp = FP(A=[("A", 10)], X=[("X", 10)], B=[("B", 10)])
        prices = self._price_of([500, 100, 100])
        # 预算：允许比直达(11:00=660)晚到，如 22:00=1320
        js = plan_cheapest(trips, fp, ["A"], ["B"], 480, 1, 1320, prices)
        self.assertTrue(js)
        best = js[0]
        self.assertEqual(best.price, 200)
        self.assertEqual(best.arr_min, 1200)
        self.assertEqual([l.train_code for l in best.legs], ["K1", "K2"])

    def test_fast_still_in_frontier(self):
        trips = self._trips()
        fp = FP(A=[("A", 10)], X=[("X", 10)], B=[("B", 10)])
        js = plan_cheapest(trips, fp, ["A"], ["B"], 480, 1, 1320,
                           self._price_of([500, 100, 100]))
        codes = [[l.train_code for l in j.legs] for j in js]
        self.assertIn(["G1"], codes)          # 直达 500 也在前沿内
        self.assertEqual(js[0].price, 200)    # 最省在前

    def test_too_tight_budget_empty(self):
        trips = self._trips()
        fp = FP(A=[("A", 10)], X=[("X", 10)], B=[("B", 10)])
        js = plan_cheapest(trips, fp, ["A"], ["B"], 480, 1, 600,
                           self._price_of([500, 100, 100]))
        self.assertEqual(js, [])

    def test_cross_station_transfer_via_footpath(self):
        # 便宜的普速从 C2 出发，需先从 C1（同城）接驳过去
        trips = [
            T("g1", "G1", ("A", 480, 480), ("C1", 600, 600)),   # 贵：A→C1 10:00 500元
            T("k1", "K1", ("C2", 780, 780), ("B", 1080, 1080)),  # 便宜：C2 13:00 出发 100元
        ]
        fp = FP(A=[("A", 10)], C1=[("C1", 10), ("C2", 90)],
                C2=[("C2", 10), ("C1", 90)], B=[("B", 10)])
        js = plan_cheapest(trips, fp, ["A"], ["B"], 480, 1, 2400,
                           self._price_of([500, 100]))
        self.assertTrue(js)
        best = js[0]
        # A→C1(10:00) + 同城接驳90min → C2 13:00 上车 → B
        self.assertEqual(best.price, 100 + 500 if False else 600)
        self.assertEqual(best.arr_min, 1080)
        codes = [l.train_code for l in best.legs]
        self.assertEqual(codes, ["G1", "同城接驳", "K1"])


class TestPlanCheapestOvernight(unittest.TestCase):
    def test_overnight_arrival_day2(self):
        trips = [
            T("g1", "G1", ("A", 1380, 1380), ("B", 1560, 1560)),  # 23:00→次日 02:00
        ]
        fp = FP(A=[("A", 10)], B=[("B", 10)])
        js = plan_cheapest(trips, fp, ["A"], ["B"], 1370, 0, 1700,
                           lambda ti, i, j: 400)
        self.assertEqual(len(js), 1)
        self.assertEqual(js[0].arr_min, 1560)
        self.assertEqual(js[0].price, 400)


if __name__ == "__main__":
    unittest.main()
