"""大环线引擎单元测试：日期推进、停留天数、Held-Karp 寻优。"""
from __future__ import annotations

import datetime as dt
import unittest

from travel_planner.engine.loop import (
    INF,
    arrival_date,
    held_karp_order,
    plan_fixed,
    solve_free_order,
)
from travel_planner.engine.model import Journey, Leg


def J(arr_min: int, dep_min: int = 480) -> Journey:
    return Journey(
        legs=[Leg(train_no="t", train_code="G1", from_station="A", from_name="",
                  to_station="B", to_name="", dep_min=dep_min, arr_min=arr_min)],
        dep_min=dep_min, arr_min=arr_min)


class TestArrivalDate(unittest.TestCase):
    def test_same_day(self):
        self.assertEqual(arrival_date(dt.date(2026, 6, 17), 900), dt.date(2026, 6, 17))

    def test_next_day(self):
        self.assertEqual(arrival_date(dt.date(2026, 6, 17), 1500), dt.date(2026, 6, 18))


class TestPlanFixed(unittest.TestCase):
    def test_dates_advance(self):
        calls = []

        def plan_leg(f, t, d, dep):
            calls.append((f, t, d, dep))
            return J(660)

        it = plan_fixed(["A", "B", "C"], dt.date(2026, 6, 17), [2, 0], plan_leg)
        self.assertEqual([(c[2], c[3]) for c in calls],
                         [(dt.date(2026, 6, 17), 480), (dt.date(2026, 6, 19), 480)])
        self.assertEqual(it.total_days, 3)

    def test_same_day_continuation(self):
        calls = []

        def plan_leg(f, t, d, dep):
            calls.append((f, t, d, dep))
            return J(540)  # 09:00 到

        it = plan_fixed(["A", "B", "C"], dt.date(2026, 6, 17), [0, 0], plan_leg)
        self.assertEqual(calls[1][2], dt.date(2026, 6, 17))
        self.assertEqual(calls[1][3], 570)  # 09:00 + 30 分钟缓冲

    def test_late_arrival_pushes_next_day(self):
        def plan_leg(f, t, d, dep):
            return J(23 * 60 + 50)  # 23:50 到

        it = plan_fixed(["A", "B", "C"], dt.date(2026, 6, 17), [0, 0], plan_leg)
        self.assertEqual(it.legs[1].date, dt.date(2026, 6, 18))
        self.assertEqual(it.legs[1].dep_min, 480)

    def test_no_journey_marks_error(self):
        def plan_leg(f, t, d, dep):
            return None

        it = plan_fixed(["A", "B"], dt.date(2026, 6, 17), [1], plan_leg)
        self.assertTrue(it.legs[0].error)
        self.assertEqual(it.legs[0].journey, None)

    def test_stays_length_check(self):
        with self.assertRaises(ValueError):
            plan_fixed(["A", "B"], dt.date(2026, 6, 17), [], lambda *a: J(600))


class TestStaySpec(unittest.TestCase):
    def test_int_zero_is_transit(self):
        from travel_planner.engine.loop import StaySpec, stay_label
        self.assertEqual(StaySpec.from_value(0).mode, "transit")
        self.assertEqual(StaySpec.from_value(2).mode, "nights")
        self.assertEqual(StaySpec.from_value(2).nights, 2)
        self.assertEqual(stay_label(0), "纯中转")
        self.assertEqual(stay_label({"mode": "halfday"}), "玩半天")
        self.assertEqual(stay_label({"mode": "nights", "nights": 3}), "住3晚")

    def test_halfday_departs_same_day(self):
        calls = []

        def plan_leg(f, t, d, dep):
            calls.append((f, t, d, dep))
            return J(600)  # 10:00 到

        it = plan_fixed(["A", "B", "C"], dt.date(2026, 6, 17),
                        [{"mode": "halfday"}, 0], plan_leg)
        # 10:00 + 5h = 15:00 当天出发
        self.assertEqual(calls[1][2], dt.date(2026, 6, 17))
        self.assertEqual(calls[1][3], 900)

    def test_halfday_late_arrival_next_day(self):
        def plan_leg(f, t, d, dep):
            return J(19 * 60)  # 19:00 到，19:00+5h=24:00 >= 23:00 → 次日 08:00

        it = plan_fixed(["A", "B", "C"], dt.date(2026, 6, 17),
                        [{"mode": "halfday"}, 0], plan_leg)
        self.assertEqual(it.legs[1].date, dt.date(2026, 6, 18))
        self.assertEqual(it.legs[1].dep_min, 480)

    def test_nights_via_dict(self):
        calls = []

        def plan_leg(f, t, d, dep):
            calls.append((f, t, d, dep))
            return J(600)

        it = plan_fixed(["A", "B", "C"], dt.date(2026, 6, 17),
                        [{"mode": "nights", "nights": 2}, 0], plan_leg)
        self.assertEqual(calls[1][2], dt.date(2026, 6, 19))  # 17 + 2 晚 = 19 出发
        self.assertEqual(calls[1][3], 480)

    def test_rest_days_are_added_after_halfday(self):
        calls = []

        def plan_leg(f, t, d, dep):
            calls.append((f, t, d, dep))
            return J(600)  # 10:00 到站

        it = plan_fixed(["A", "B", "C"], dt.date(2026, 6, 17),
                        [{"mode": "halfday", "rest_days": 2}, 0], plan_leg)
        self.assertEqual(calls[1][2], dt.date(2026, 6, 19))
        self.assertEqual(calls[1][3], 480)


class TestHeldKarp(unittest.TestCase):
    def test_known_instance(self):
        # 0→1=10, 0→2=15, 1→2=5, 2→1=6, 1→3=20, 2→3=10
        mat = [
            [0, 10, 15, INF],
            [INF, 0, 5, 20],
            [INF, 6, 0, 10],
            [INF, INF, INF, 0],
        ]
        best, order = held_karp_order(mat, 2)
        # 0→1→2→3 = 25；0→2→1→3 = 41
        self.assertEqual(best, 25)
        self.assertEqual(order, [0, 1])

    def test_unreachable(self):
        mat = [[0, INF, INF, INF], [INF, 0, INF, INF],
               [INF, INF, 0, INF], [INF, INF, INF, 0]]
        best, order = held_karp_order(mat, 2)
        self.assertEqual(best, INF)
        self.assertEqual(order, [])


class TestSolveFreeOrder(unittest.TestCase):
    def test_picks_best_order(self):
        costs = {("昆明", "大理"): 600, ("昆明", "丽江"): 700,
                 ("大理", "丽江"): 800, ("丽江", "大理"): 900,
                 ("大理", "昆明"): 650, ("丽江", "昆明"): 750}

        def plan_leg(a, b, d, dep):
            v = costs.get((a, b))
            return J(v) if v is not None else None

        labels, best, cache = solve_free_order(
            "昆明", "昆明", ["大理", "丽江"], dt.date(2026, 6, 17), 480, plan_leg)
        # 昆明→大理→丽江→昆明 = 2150；昆明→丽江→大理→昆明 = 2250
        self.assertEqual(labels, ["昆明", "大理", "丽江", "昆明"])
        self.assertEqual(best, 2150)

    def test_middle_limit(self):
        with self.assertRaises(ValueError):
            solve_free_order("A", "B", [f"M{i}" for i in range(11)],
                             dt.date(2026, 6, 17), 480, lambda *a: J(600))


if __name__ == "__main__":
    unittest.main()
