"""RAPTOR 引擎单元测试（手工构造的小规模时刻表）。"""
from __future__ import annotations

import unittest

from travel_planner.engine.model import Trip
from travel_planner.engine.raptor import plan


def T(trip_id: str, code: str, *stops) -> Trip:
    """stops: (站码, 到达分钟, 出发分钟)。"""
    stations = [s for s, _, _ in stops]
    arr = [a for _, a, _ in stops]
    dep = [d for _, _, d in stops]
    return Trip(trip_id, code, stations, arr, dep)


def FP(**pairs) -> dict:
    return {k: list(v) for k, v in pairs.items()}


class TestDirect(unittest.TestCase):
    def test_direct(self):
        trips = [T("t1", "G1", ("A", 600, 600), ("B", 660, 660))]
        fp = FP(A=[("A", 10)], B=[("B", 10)])
        js = plan(trips, fp, ["A"], ["B"], 540, max_transfers=0)
        self.assertEqual(len(js), 1)
        j = js[0]
        self.assertEqual(j.transfers, 0)
        self.assertEqual((j.dep_min, j.arr_min, j.duration), (600, 660, 60))
        self.assertEqual(j.legs[0].train_code, "G1")

    def test_too_early_departure_not_used(self):
        trips = [T("t1", "G1", ("A", 600, 600), ("B", 660, 660))]
        js = plan(trips, FP(A=[("A", 10)], B=[("B", 10)]), ["A"], ["B"], 601,
                  max_transfers=0)
        self.assertEqual(js, [])


class TestTransfer(unittest.TestCase):
    def test_valid_transfer(self):
        trips = [
            T("t1", "G1", ("A", 600, 600), ("B", 660, 660)),
            T("t2", "G2", ("B", 720, 720), ("C", 780, 780)),
        ]
        fp = FP(A=[("A", 10)], B=[("B", 10)], C=[("C", 10)])
        js = plan(trips, fp, ["A"], ["C"], 540, max_transfers=1)
        js = [j for j in js if j.transfers == 1]
        self.assertEqual(len(js), 1)
        self.assertEqual([l.train_code for l in js[0].legs], ["G1", "G2"])
        self.assertEqual((js[0].dep_min, js[0].arr_min), (600, 780))
        self.assertEqual(js[0].wait_total, 60)

    def test_tight_transfer_rejected(self):
        # B 站最小换乘 10 分钟：11:00 到，11:05 发 -> 不可换乘
        trips = [
            T("t1", "G1", ("A", 600, 600), ("B", 660, 660)),
            T("t2", "G2", ("B", 665, 665), ("C", 725, 725)),
        ]
        fp = FP(A=[("A", 10)], B=[("B", 10)], C=[("C", 10)])
        js = plan(trips, fp, ["A"], ["C"], 540, max_transfers=1)
        self.assertEqual(js, [])

    def test_max_transfers_limits(self):
        trips = [
            T("t1", "G1", ("A", 600, 600), ("B", 660, 660)),
            T("t2", "G2", ("B", 720, 720), ("C", 780, 780)),
            T("t3", "G3", ("C", 840, 840), ("D", 900, 900)),
        ]
        fp = FP(A=[("A", 10)], B=[("B", 10)], C=[("C", 10)], D=[("D", 10)])
        self.assertEqual(plan(trips, fp, ["A"], ["D"], 540, max_transfers=1), [])
        js = plan(trips, fp, ["A"], ["D"], 540, max_transfers=2)
        self.assertEqual(len(js), 1)
        self.assertEqual(len(js[0].legs), 3)


class TestThroughRide(unittest.TestCase):
    def test_same_train_counts_one_leg(self):
        trips = [T("t1", "G1", ("A", 600, 600), ("B", 660, 665), ("C", 740, 740))]
        fp = FP(A=[("A", 10)], B=[("B", 10)], C=[("C", 10)])
        js = plan(trips, fp, ["A"], ["C"], 540, max_transfers=2)
        self.assertEqual(len(js), 1)
        self.assertEqual(js[0].transfers, 0)
        self.assertEqual(len(js[0].legs), 1)
        self.assertEqual((js[0].legs[0].from_station, js[0].legs[0].to_station), ("A", "C"))


class TestSameCityTransfer(unittest.TestCase):
    def test_cross_station_within_city(self):
        trips = [
            T("t1", "G1", ("A", 600, 600), ("B1", 660, 660)),
            T("t2", "G2", ("B2", 750, 750), ("C", 810, 810)),
        ]
        # 同城异站 B1->B2 需 90 分钟：11:00 + 90 = 12:30 可换乘
        fp = FP(A=[("A", 10)], B1=[("B1", 10), ("B2", 90)],
                B2=[("B2", 10), ("B1", 90)], C=[("C", 10)])
        js = plan(trips, fp, ["A"], ["C"], 540, max_transfers=1)
        self.assertEqual(len(js), 1)
        self.assertEqual([l.from_station for l in js[0].legs], ["A", "B2"])

    def test_cross_station_too_tight(self):
        trips = [
            T("t1", "G1", ("A", 600, 600), ("B1", 660, 660)),
            T("t2", "G2", ("B2", 740, 740), ("C", 800, 800)),  # 11:00+90=12:30 > 12:20
        ]
        fp = FP(A=[("A", 10)], B1=[("B1", 10), ("B2", 90)],
                B2=[("B2", 10), ("B1", 90)], C=[("C", 10)])
        self.assertEqual(plan(trips, fp, ["A"], ["C"], 540, max_transfers=1), [])


class TestOvernight(unittest.TestCase):
    def test_midnight_crossing(self):
        # 23:00 (1380) -> 次日 01:00 (1500)
        trips = [T("t1", "K1", ("A", 1380, 1380), ("B", 1500, 1500))]
        fp = FP(A=[("A", 10)], B=[("B", 10)])
        js = plan(trips, fp, ["A"], ["B"], 1370, max_transfers=0)
        self.assertEqual(len(js), 1)
        self.assertEqual(js[0].duration, 120)
        self.assertEqual(js[0].arr_min, 1500)


class TestFinalFootpath(unittest.TestCase):
    def test_final_cross_station_arrival_beats_direct(self):
        # G1 到 B1（同城另一站），经接驳 90 分钟到 B2，比直达 B2 的慢车更早
        trips = [
            T("t1", "G1", ("A", 600, 600), ("B1", 660, 660)),
            T("t2", "K1", ("A", 610, 610), ("B2", 800, 800)),
        ]
        fp = FP(A=[("A", 10)], B1=[("B1", 10), ("B2", 90)],
                B2=[("B2", 10), ("B1", 90)])
        js = plan(trips, fp, ["A"], ["B2"], 540, max_transfers=0)
        self.assertEqual(len(js), 1)
        j = js[0]
        self.assertEqual(j.arr_min, 750)          # 11:00 + 90min = 12:30
        self.assertEqual(len(j.legs), 2)
        self.assertEqual(j.legs[0].train_code, "G1")
        self.assertEqual(j.legs[1].train_code, "同城接驳")
        self.assertEqual((j.legs[1].from_station, j.legs[1].to_station), ("B1", "B2"))

    def test_final_same_station_no_extra_leg(self):
        trips = [T("t1", "G1", ("A", 600, 600), ("B", 660, 660))]
        js = plan(trips, FP(A=[("A", 10)], B=[("B", 10)]), ["A"], ["B"], 540,
                  max_transfers=0)
        self.assertEqual(len(js), 1)
        self.assertEqual(len(js[0].legs), 1)
        self.assertEqual(js[0].arr_min, 660)


class TestMultiStartTarget(unittest.TestCase):
    def test_multi_start_picks_best_origin(self):
        # 只有 A2 有车，起点集合含 A1/A2 也能找到
        trips = [T("t1", "G1", ("A2", 600, 600), ("B", 660, 660))]
        fp = FP(A1=[("A1", 10)], A2=[("A2", 10)], B=[("B", 10)])
        js = plan(trips, fp, ["A1", "A2"], ["B"], 540, max_transfers=0)
        self.assertEqual(len(js), 1)
        self.assertEqual(js[0].legs[0].from_station, "A2")

    def test_multi_target_accepts_any(self):
        # 目标集合含 B1/B2，火车只到 B1
        trips = [T("t1", "G1", ("A", 600, 600), ("B1", 660, 660))]
        fp = FP(A=[("A", 10)], B1=[("B1", 10)], B2=[("B2", 10)])
        js = plan(trips, fp, ["A"], ["B1", "B2"], 540, max_transfers=0)
        self.assertEqual(len(js), 1)
        self.assertEqual(js[0].legs[-1].to_station, "B1")
        self.assertEqual(js[0].arr_min, 660)


class TestOriginFootpath(unittest.TestCase):
    def test_start_walks_to_other_station_to_board(self):
        # 车从 B2 发，起点在 B1（同城另一站，90 分钟接驳）→ 应先接驳到 B2 上车
        trips = [T("t1", "G1", ("B2", 600, 600), ("C", 720, 720))]
        fp = FP(B1=[("B1", 10), ("B2", 90)], B2=[("B2", 10), ("B1", 90)],
                C=[("C", 10)])
        js = plan(trips, fp, ["B1"], ["C"], 480, max_transfers=0)
        self.assertEqual(len(js), 1)
        self.assertEqual([l.train_code for l in js[0].legs], ["同城接驳", "G1"])
        self.assertEqual(js[0].legs[0].from_station, "B1")
        self.assertEqual(js[0].legs[0].to_station, "B2")
        self.assertEqual(js[0].legs[0].dep_min, 480)
        self.assertEqual(js[0].arr_min, 720)

    def test_start_walk_too_slow_misses_train(self):
        # 接驳 90 分钟：08:00+90=09:30 > 09:00 发车 → 不可上车
        trips = [T("t1", "G1", ("B2", 540, 540), ("C", 660, 660))]
        fp = FP(B1=[("B1", 10), ("B2", 90)], B2=[("B2", 10), ("B1", 90)],
                C=[("C", 10)])
        self.assertEqual(plan(trips, fp, ["B1"], ["C"], 480, max_transfers=0), [])


class TestEdgeCases(unittest.TestCase):
    def test_unreachable(self):
        self.assertEqual(plan([], FP(A=[("A", 10)], B=[("B", 10)]),
                              ["A"], ["B"], 0, max_transfers=2), [])

    def test_loop_line_target_found(self):
        # 环线车经过 A 两次
        trips = [T("t1", "G1", ("A", 600, 600), ("B", 660, 665),
                   ("A", 720, 725), ("C", 800, 800))]
        fp = FP(A=[("A", 10)], B=[("B", 10)], C=[("C", 10)])
        js = plan(trips, fp, ["A"], ["C"], 540, max_transfers=0)
        self.assertEqual(len(js), 1)
        self.assertEqual(js[0].legs[0].from_station, "A")
        self.assertEqual(js[0].arr_min, 800)


if __name__ == "__main__":
    unittest.main()
