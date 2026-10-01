"""RAPTOR 换乘规划引擎（确定性算法）。

思路（Dibbelt et al. 的 Round-Based Public Transit Routing）：
- 第 r 轮扫描 = 用「≤ r 趟车」能到达的站与时刻；
- 每轮：从上一轮标记的站上车，沿整条列车线扫过去更新最早到达；
- 轮间：用步行/换乘边（同站最小换乘时间、同城异站换乘时间）传播标记；
- 正确性依赖：每条列车线只需在「最早可上车的站」上车一次。

父指针分两类：
- parent[(站, 轮)]      乘车到达（第 r 轮坐某车次到达该站）
- mark_parent[(站, 轮)] 第 r 轮可上车标记的来源（从上一轮某站经步行边而来）

支持多个起点（同城多站）与多个终点（到达任意目标站或经同城接驳收尾）。
时间一律为相对查询日 0 点的绝对分钟。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Set, Tuple

from .model import Journey, Leg, Trip

INF = float("inf")


def plan(trips: Sequence[Trip],
         footpaths: Dict[str, List[Tuple[str, int]]],
         starts: Sequence[str], targets: Sequence[str], start_min: int,
         max_transfers: int = 3, allow_leg=None) -> List[Journey]:
    """规划 starts -> targets、start_min 之后出发、最多 max_transfers 次换乘的方案。

    footpaths: 到达站 -> [(下一可上车点, 耗时分钟)]。
    allow_leg(trip, i, j) -> bool：可选段过滤器（如绿皮模式限制高铁段价）；
    约束随 j 单调时返回 False 即停止沿线松弛。
    返回每个「段数」下的最早到达方案（最多 max_transfers+1 段）。
    """
    rounds = max_transfers + 1
    target_set: Set[str] = set(targets)

    station_set = set(starts) | target_set
    for t in trips:
        station_set.update(t.stations)

    tau: Dict[str, List[float]] = {s: [INF] * (rounds + 1) for s in station_set}
    parent: Dict[Tuple[str, int], Tuple[int, int]] = {}
    mark_parent: Dict[Tuple[str, int], Tuple[str, int]] = {}
    for s in starts:
        tau[s][0] = float(start_min)

    marked: Dict[str, float] = {s: float(start_min) for s in starts}
    # 起点也可先经步行边到附近站上车（如 上海南 → 上海虹桥 再乘车）
    origin_fp: Dict[str, Tuple[str, float]] = {}
    for s in starts:
        for nxt, w in footpaths.get(s, ()):
            if nxt == s:
                continue
            t2 = start_min + w
            if t2 < marked.get(nxt, INF):
                marked[nxt] = t2
                origin_fp[nxt] = (s, t2)

    for r in range(1, rounds + 1):
        improved: Dict[str, float] = {}
        for ti, trip in enumerate(trips):
            board_seq = _earliest_boardable(trip, marked)
            if board_seq is None:
                continue
            for j in range(board_seq + 1, len(trip.stations)):
                if allow_leg is not None and not allow_leg(trip, board_seq, j):
                    break  # 约束随 j 单调（费用递增），其后只可能更不允许
                s = trip.stations[j]
                a = trip.arr[j]
                if a < tau[s][r]:
                    tau[s][r] = a
                    parent[(s, r)] = (ti, board_seq)
                    if a < improved.get(s, INF):
                        improved[s] = a

        new_marked: Dict[str, float] = {}
        for s, a in improved.items():
            for nxt, w in footpaths.get(s, ()):
                t2 = a + w
                if t2 < new_marked.get(nxt, INF):
                    new_marked[nxt] = t2
                    mark_parent[(nxt, r)] = (s, r)
        marked = new_marked
        if not marked:
            break

    # 收尾：允许到达任意目标站（同站权重 0），或经同城接驳边收尾
    journeys: List[Journey] = []
    for r in range(1, rounds + 1):
        best_s: Optional[str] = None
        best_tgt: Optional[str] = None
        best_w = INF
        for s, trow in tau.items():
            t = trow[r]
            if t == INF:
                continue
            fw = _final_weight(footpaths, s, target_set)
            if fw is None:
                continue
            w, tgt = fw
            if t + w < best_w:
                best_w = t + w
                best_s = s
                best_tgt = tgt
        if best_s is None:
            continue
        legs = _reconstruct(parent, mark_parent, origin_fp, trips, set(starts),
                            start_min, best_s, r)
        if not legs:
            continue
        if best_s != best_tgt:
            legs.append(Leg(
                train_no="", train_code="同城接驳",
                from_station=best_s, from_name="",
                to_station=best_tgt, to_name="",
                dep_min=int(tau[best_s][r]), arr_min=int(best_w),
            ))
        journeys.append(Journey(legs=legs, dep_min=legs[0].dep_min,
                                arr_min=int(best_w)))
    return journeys


def _earliest_boardable(trip: Trip, marked: Dict[str, float]) -> Optional[int]:
    for i, s in enumerate(trip.stations):
        t = marked.get(s)
        if t is not None and t <= trip.dep[i]:
            return i
    return None


def _final_weight(footpaths: Dict[str, List[Tuple[str, int]]],
                  s: str, target_set: Set[str]) -> Optional[Tuple[float, str]]:
    """从 s 到任一目标站的最小接驳耗时；不可达返回 None。同站为 0。"""
    if s in target_set:
        return (0, s)
    best: Optional[Tuple[float, str]] = None
    for nxt, w in footpaths.get(s, ()):
        if nxt in target_set and (best is None or w < best[0]):
            best = (w, nxt)
    return best


def _reconstruct(parent: Dict[Tuple[str, int], Tuple[int, int]],
                 mark_parent: Dict[Tuple[str, int], Tuple[str, int]],
                 origin_fp: Dict[str, Tuple[str, float]],
                 trips: Sequence[Trip], starts: Set[str], start_min: int,
                 stop: str, r: int) -> List[Leg]:
    """沿 parent/mark_parent 回溯出完整行程（段列表）。"""
    legs: List[Leg] = []
    cur_station = stop
    cur_round = r
    while True:
        entry = parent.get((cur_station, cur_round))
        if entry is None:
            return []
        ti, board_seq = entry
        trip = trips[ti]
        seq_end = None
        for j in range(board_seq + 1, len(trip.stations)):
            if trip.stations[j] == cur_station:
                seq_end = j
                break
        if seq_end is None:
            return []
        legs.append(Leg(
            train_no=trip.trip_id,
            train_code=trip.code,
            from_station=trip.stations[board_seq],
            from_name="",
            to_station=trip.stations[seq_end],
            to_name="",
            dep_min=trip.dep[board_seq],
            arr_min=trip.arr[seq_end],
        ))
        if cur_round == 1:
            # 第一段必须从某个起点站上车；若经起点步行边到其他站，则补「出发接驳」段
            board_st = trip.stations[board_seq]
            if board_st not in starts:
                src = origin_fp.get(board_st)
                if src is None:
                    return []
                s0, t0 = src
                legs.append(Leg(
                    train_no="", train_code="同城接驳",
                    from_station=s0, from_name="",
                    to_station=board_st, to_name="",
                    dep_min=start_min, arr_min=int(t0),
                ))
            break
        # 上一轮：怎么到达上车点（可能经步行边）
        mp = mark_parent.get((trip.stations[board_seq], cur_round - 1))
        if mp is None:
            return []
        cur_station, cur_round = mp
    legs.reverse()
    return legs
