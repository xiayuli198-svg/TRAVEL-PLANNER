"""限时内最省：在给定「最晚到达」预算内，找出「时间-费用」帕累托前沿行程。

模型与 raptor 一致（round = 已乘交通工具数，≤ max_transfers+1）：
- 费用只计交通工具段（rail/flight）；同城接驳/步行边不计费（展示时另加附加费）；
- 每站维护按到达时间排序的帕累托标签（前缀最小费用），O(log n) 支配剪枝；
- 每轮扫描全部车次：在可上车停站 i 用「此刻可上车的最便宜标签」上车，
  沿途每站 j 松弛 (arr[j], cost+车费(i→j))，超出预算即停。

注：为控制规模，标签按 (时间, 费用) 剪枝时忽略「乘车次数」差异（近似，误差
边界为个别方案多/少 1 次换乘），个人规划场景可接受。

price_of(trip_idx, board_i, alight_j) -> 费用（调用方按里程/真实票价提供）。
"""
from __future__ import annotations

import bisect
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from .model import Journey, Leg, Trip

INF = float("inf")
MAX_RESULTS = 8


class _StationLabels:
    """单站标签库：arrs 升序 + 前缀最小费用（含 rides<=r 的全程可用标签）。"""

    __slots__ = ("arrs", "costs", "rides", "refs", "pref")

    def __init__(self):
        self.arrs: List[float] = []
        self.costs: List[float] = []
        self.rides: List[int] = []
        self.refs: List[dict] = []
        self.pref: List[Tuple[float, dict]] = []   # (min_cost, ref) 前缀

    def dominated(self, arr: float, cost: float) -> bool:
        if not self.arrs:
            return False
        idx = bisect.bisect_right(self.arrs, arr)
        if idx == 0:
            return False
        return self.pref[idx - 1][0] <= cost

    def add(self, arr: float, cost: float, rides: int, ref: dict) -> None:
        if self.dominated(arr, cost):
            return
        idx = bisect.bisect_right(self.arrs, arr)
        # 去掉被新标签支配的后续标签（arr >= 且 cost >= 且非同对象）
        cut = idx
        while cut < len(self.arrs) and self.costs[cut] >= cost:
            cut += 1
        self.arrs[idx:cut] = [arr]
        self.costs[idx:cut] = [cost]
        self.rides[idx:cut] = [rides]
        self.refs[idx:cut] = [ref]
        # 重建前缀最小
        self.pref = []
        best = None
        for c, r in zip(self.costs, self.refs):
            if best is None or c < best[0]:
                best = (c, r)
            self.pref.append(best)

    def iter_rides_le(self, r: int):
        for i in range(len(self.arrs)):
            if self.rides[i] <= r:
                yield self.arrs[i], self.costs[i], self.rides[i], self.refs[i]


def plan_cheapest(trips: Sequence[Trip],
                  footpaths: Dict[str, List[Tuple[str, int]]],
                  starts: Sequence[str], targets: Sequence[str],
                  start_min: int, max_transfers: int, budget_arr: float,
                  price_of: Callable[[int, int, int], float],
                  allow_leg=None) -> List[Journey]:
    """返回按费用升序的帕累托行程（最省在前，最快也在此前沿内）。

    allow_leg(trip, i, j) -> bool：可选段过滤器（绿皮模式等）。
    """
    rounds = max_transfers + 1
    target_set: Set[str] = set(targets)

    # 反向步行边：到达站 S -> [(来源站 X, 耗时 w)]
    rev: Dict[str, List[Tuple[str, int]]] = {}
    for x, edges in footpaths.items():
        for s, w in edges:
            rev.setdefault(s, []).append((x, w))

    station_set = set(starts) | target_set
    for t in trips:
        station_set.update(t.stations)
    for s in footpaths:
        station_set.add(s)
    stores: Dict[str, _StationLabels] = {s: _StationLabels() for s in station_set}

    for s in starts:
        stores[s].add(float(start_min), 0.0, 0, None)

    for r in range(1, rounds + 1):
        # 本轮可上车标签（rides <= r-1）经步行边到上车站 S → eff[S]
        eff: Dict[str, List[Tuple[float, float, str, dict, float]]] = {}
        for x, st in stores.items():
            for arr, cost, rides, ref in st.iter_rides_le(r - 1):
                for s, w in rev.get(x, ()):
                    ww = 0.0 if (rides == 0 and s == x) else float(w)
                    eff.setdefault(s, []).append((arr + ww, cost, x, ref, ww))
        if not eff:
            break
        pref: Dict[str, List] = {}
        arr_lists: Dict[str, List[float]] = {}
        for s, items in eff.items():
            items.sort(key=lambda t: (t[0], t[1]))
            best = None
            p = []
            for it in items:
                if best is None or it[1] < best[1]:
                    best = it
                p.append(best)
            pref[s] = p
            arr_lists[s] = [t[0] for t in items]

        for ti, trip in enumerate(trips):
            n = len(trip.stations)
            boards = []          # (stop_i, 上车成本, 来源站, 标签, 步行时长)
            any_board = False
            for i in range(n):
                s = trip.stations[i]
                if s not in arr_lists:
                    continue
                idx = bisect.bisect_right(arr_lists[s], trip.dep[i])
                if idx == 0:
                    continue
                _, cost, x, ref, ww = pref[s][idx - 1]
                boards.append((i, cost, x, ref, ww))
                any_board = True
            if not any_board:
                continue
            n = len(trip.stations)
            for i, cost_i, x_i, ref_i, w_i in boards:
                for j in range(i + 1, n):
                    arr_j = trip.arr[j]
                    if arr_j > budget_arr:
                        break
                    if allow_leg is not None and not allow_leg(trip, i, j):
                        break  # 约束随 j 单调，其后更不允许
                    nc = cost_i + price_of(ti, i, j)
                    st = stores[trip.stations[j]]
                    ref = dict(rides=r, arr=float(arr_j), trip=ti, board=i, alight=j,
                               src_station=x_i, src_label=ref_i, fp_w=w_i)
                    st.add(float(arr_j), float(nc), r, ref)

    # 终局：标签经步行边（同站 0 分钟）到任一目标站，且不超预算
    final_candidates = []
    for x, st in stores.items():
        for arr, cost, rides, ref in st.iter_rides_le(rounds):
            if x in target_set:
                final_candidates.append((cost, arr, ref, x, 0.0))
                continue
            for t, w in footpaths.get(x, ()):
                if t in target_set and arr + w <= budget_arr:
                    final_candidates.append((cost, arr + w, ref, t, float(w)))
    final_candidates.sort(key=lambda c: (c[0], c[1]))
    best_arr = INF
    kept = []
    for cost, arr, ref, tgt, w in final_candidates:
        if ref is None:
            continue
        if arr < best_arr:
            best_arr = arr
            kept.append((cost, arr, ref, tgt, w))
        if len(kept) >= MAX_RESULTS:
            break

    journeys: List[Journey] = []
    for cost, arr, ref, tgt, w in kept:
        j = _reconstruct(trips, ref, tgt, w, price_of)
        if j is not None:
            j.price = round(float(cost), 0)
            journeys.append(j)
    return journeys


def _reconstruct(trips: Sequence[Trip], ref: dict, final_t: str,
                 final_w: float, price_of: Callable[[int, int, int], float]
                 ) -> Optional[Journey]:
    legs: List[Leg] = []
    cur: Optional[dict] = ref
    while cur is not None and cur.get("trip") is not None:
        trip = trips[cur["trip"]]
        bi, aj = cur["board"], cur["alight"]
        legs.append(Leg(
            train_no=trip.trip_id, train_code=trip.code,
            from_station=trip.stations[bi], from_name="",
            to_station=trip.stations[aj], to_name="",
            dep_min=trip.dep[bi], arr_min=trip.arr[aj],
            price=round(price_of(cur["trip"], bi, aj), 0),
        ))
        if cur.get("src_label"):
            src = cur["src_label"]
            board_st = trip.stations[bi]
            if cur.get("src_station") != board_st and cur.get("fp_w"):
                legs.append(Leg(
                    train_no="", train_code="同城接驳",
                    from_station=cur["src_station"], from_name="",
                    to_station=board_st, to_name="",
                    dep_min=int(src["arr"]), arr_min=int(src["arr"] + cur["fp_w"]),
                    price=None,
                ))
        cur = cur.get("src_label")
    legs.reverse()

    if legs and final_t != legs[-1].to_station and final_w:
        legs.append(Leg(train_no="", train_code="同城接驳",
                        from_station=legs[-1].to_station, from_name="",
                        to_station=final_t, to_name="",
                        dep_min=legs[-1].arr_min,
                        arr_min=legs[-1].arr_min + int(final_w), price=None))
    if not legs:
        return None
    total = sum(l.price or 0 for l in legs if l.train_no)
    return Journey(legs=legs, dep_min=legs[0].dep_min, arr_min=legs[-1].arr_min,
                   price=round(total, 0))
