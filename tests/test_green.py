"""绿皮省钱模式段过滤器（allow_leg）引擎层测试。"""
from __future__ import annotations

import unittest

from travel_planner.engine.cheapest import plan_cheapest
from travel_planner.engine.model import Trip
from travel_planner.engine.raptor import plan


def T(trip_id: str, code: str, *stops) -> Trip:
    stations = [s for s, _, _ in stops]
    arr = [a for _, a, _ in stops]
    dep = [d for _, _, d in stops]
    return Trip(trip_id, code, stations, arr, dep)


def FP(**pairs) -> dict:
    return {k: list(v) for k, v in pairs.items()}


def green_allow(max_hsr_hops: int = 1):
    """模拟：普速(K/Z/T)任意；高铁每段最多骑 max_hsr_hops 个区间。"""
    def allow(trip, i, j):
        if trip.code.upper().startswith(("K", "Z", "T")) or trip.code.isdigit():
            return True
        return (j - i) <= max_hsr_hops
    return allow


class TestRaptorGreenFilter(unittest.TestCase):
    def test_hsr_long_ride_blocked_uses_conventional(self):
        trips = [
            T("g1", "G1", ("A", 480, 480), ("B", 540, 545), ("C", 620, 620)),  # 直达 G 两段
            T("k1", "K1", ("A", 480, 480), ("C", 700, 700)),                    # 普速直达
        ]
        fp = FP(A=[("A", 10)], B=[("B", 10)], C=[("C", 10)])
        js = plan(trips, fp, ["A"], ["C"], 480, max_transfers=1, allow_leg=green_allow())
        codes = {tuple(l.train_code for l in j.legs) for j in js}
        self.assertIn(("K1",), codes)
        self.assertNotIn(("G1",), codes)   # G 一坐到底超段限，被过滤

    def test_hsr_short_hop_still_allowed(self):
        trips = [T("g1", "G1", ("A", 480, 480), ("B", 540, 540))]
        fp = FP(A=[("A", 10)], B=[("B", 10)])
        js = plan(trips, fp, ["A"], ["B"], 480, max_transfers=0, allow_leg=green_allow())
        self.assertEqual(len(js), 1)
        self.assertEqual(js[0].legs[0].train_code, "G1")


class TestCheapestGreenFilter(unittest.TestCase):
    def test_cheapest_respects_filter(self):
        trips = [
            T("g1", "G1", ("A", 480, 480), ("B", 540, 545), ("C", 620, 620)),
            T("k1", "K1", ("A", 480, 480), ("C", 700, 700)),
        ]
        fp = FP(A=[("A", 10)], B=[("B", 10)], C=[("C", 10)])
        js = plan_cheapest(trips, fp, ["A"], ["C"], 480, 1, 800,
                           lambda ti, i, j: 300 if ti == 0 else 100,
                           allow_leg=green_allow())
        self.assertTrue(js)
        self.assertEqual([l.train_code for l in js[0].legs], ["K1"])


if __name__ == "__main__":
    unittest.main()
