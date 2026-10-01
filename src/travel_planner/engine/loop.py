"""大环线多站点行程规划：固定顺序与自由顺序（Held-Karp 寻优）。

时间约定与 raptor 一致：每段方案的 dep_min/arr_min 为相对该段出发日 0 点的分钟。
自由顺序寻优：以「各段最早到达时间之和」为成本，对中间点做 TSP 路径动态规划，
选出最优访问顺序后，再按真实日期逐段重规划生成最终行程。

停留方式（每个到达点的停留方式 StaySpec）：
- transit：纯中转——到站后尽快走（换乘缓冲 30 分钟，当天走不了则次日出发）
- halfday：玩半天——到站玩约 5 小时后走（23:00 前走得掉就当天走，否则次日 08:00）
- nights：住 N 晚——到达日 + N 晚后的次日 08:00 出发
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from .model import Journey

INF = float("inf")

# 计划一段：起点标签, 终点标签, 出发日, 最早出发分钟 -> 最佳方案（无方案返回 None）
PlanLegFn = Callable[[str, str, dt.date, int], Optional[Journey]]

HALFDAY_PLAY_MIN = 300      # 玩半天 = 到站后约 5 小时
HALFDAY_LATEST_DEP = 1380   # 当天 23:00 前走


@dataclass
class StaySpec:
    mode: str = "nights"   # transit / halfday / nights
    nights: int = 1
    # 到达后的额外休整天数。保持与 nights 分开，避免破坏旧版停留参数。
    rest_days: int = 0

    @staticmethod
    def from_value(v: Union["StaySpec", Dict, int]) -> "StaySpec":
        if isinstance(v, StaySpec):
            return v
        if isinstance(v, dict):
            mode = v.get("mode") or "nights"
            if mode not in ("transit", "halfday", "nights"):
                mode = "nights"
            return StaySpec(mode, max(0, int(v.get("nights") or 0)),
                            max(0, int(v.get("rest_days") or 0)))
        n = int(v)
        # 兼容旧的"停留 N 天"写法：0 = 当天就走（纯中转），N = 住 N 晚
        return StaySpec("transit", 0) if n <= 0 else StaySpec("nights", n)


def stay_label(v: Union[StaySpec, Dict, int]) -> str:
    spec = StaySpec.from_value(v)
    if spec.mode == "transit":
        return "纯中转"
    if spec.mode == "halfday":
        base = "玩半天"
    else:
        base = f"住{spec.nights}晚"
    return f"{base}+休整{spec.rest_days}天" if spec.rest_days else base


@dataclass
class LegPlan:
    frm: str
    to: str
    date: dt.date
    dep_min: int
    journey: Optional[Journey]
    error: str = ""


@dataclass
class LoopItinerary:
    stops: List[str]
    stays: List[StaySpec]
    legs: List[LegPlan]
    order: Optional[List[str]] = None   # 自由顺序寻优得到的最优访问顺序（含首尾）

    @property
    def total_days(self) -> int:
        if not self.legs or self.legs[-1].journey is None:
            return 0
        last = self.legs[-1]
        arr_day = last.date + dt.timedelta(days=last.journey.arr_min // 1440)
        return (arr_day - self.legs[0].date).days + 1


def arrival_date(depart_date: dt.date, arr_min: int) -> dt.date:
    return depart_date + dt.timedelta(days=arr_min // 1440)


def _next_departure(arr_day: dt.date, arr_min: int, spec: StaySpec,
                    default_dep: int, buffer: int) -> Tuple[dt.date, int]:
    """根据停留方式计算下一段的 (出发日, 最早出发分钟)。arr_min 为当日分钟数。"""
    if spec.mode == "transit":
        if spec.rest_days:
            return arr_day + dt.timedelta(days=spec.rest_days), default_dep
        d = arr_min + buffer
        if d < 1440:
            return arr_day, d
        return arr_day + dt.timedelta(days=1), default_dep
    if spec.mode == "halfday":
        d = arr_min + HALFDAY_PLAY_MIN
        if d < HALFDAY_LATEST_DEP and spec.rest_days == 0:
            return arr_day, d
        return arr_day + dt.timedelta(days=max(1, spec.rest_days)), default_dep
    return arr_day + dt.timedelta(days=spec.nights + spec.rest_days), default_dep


def plan_fixed(stops: Sequence[str], start_date: dt.date,
               stays: Sequence[Union[StaySpec, Dict, int]],
               plan_leg: PlanLegFn, default_dep: int = 480,
               stopover_buffer: int = 30) -> LoopItinerary:
    """固定顺序逐段规划。

    stays 长度 = len(stops)-1：到达 stops[i+1] 后的停留方式
    （int N：N<=0 = 纯中转，N>=1 = 住 N 晚；dict：{mode, nights}）。
    """
    stops = list(stops)
    stays = [StaySpec.from_value(s) for s in stays]
    if len(stops) < 2:
        raise ValueError("至少需要两个途经点")
    if len(stays) != len(stops) - 1:
        raise ValueError("stays 数量应为途经点数-1")

    legs: List[LegPlan] = []
    cur_date = start_date
    dep_min = default_dep

    for i in range(len(stops) - 1):
        frm, to = stops[i], stops[i + 1]
        j = plan_leg(frm, to, cur_date, dep_min)
        error = ("" if j is not None
                 else f"{frm}→{to} 未找到可行方案（该日数据未覆盖此 OD，或换乘次数上限过低）")
        legs.append(LegPlan(frm, to, cur_date, dep_min, j, error))
        if j is not None:
            arr_day = arrival_date(cur_date, j.arr_min)
            cur_date, dep_min = _next_departure(
                arr_day, j.arr_min % 1440, stays[i], default_dep, stopover_buffer)
        else:
            cur_date = cur_date + dt.timedelta(days=stays[i].nights)
            dep_min = default_dep
    return LoopItinerary(stops=stops, stays=stays, legs=legs)


def held_karp_order(cost: Sequence[Sequence[float]], n_mid: int
                    ) -> Tuple[float, List[int]]:
    """TSP 路径动态规划。节点编号：0=起点，1..n_mid=中间点，n_mid+1=终点。

    返回 (最小总成本, 中间点访问顺序[0-based 索引])；不可达返回 (INF, [])。
    """
    n = n_mid
    size = 1 << n
    dp = [[INF] * (n + 1) for _ in range(size)]          # dp[mask][last]
    par: List[List[Optional[Tuple[int, int]]]] = [[None] * (n + 1) for _ in range(size)]
    dp[0][0] = 0.0

    for mask in range(size):
        for last in range(n + 1):
            c = dp[mask][last]
            if c == INF:
                continue
            if last == 0:  # 从起点出发
                for j in range(n):
                    if mask & (1 << j):
                        continue
                    nc = c + cost[0][1 + j]
                    if nc < dp[mask | (1 << j)][1 + j]:
                        dp[mask | (1 << j)][1 + j] = nc
                        par[mask | (1 << j)][1 + j] = (mask, last)
            else:          # 在中间点 last
                for j in range(n):
                    if mask & (1 << j):
                        continue
                    nc = c + cost[last][1 + j]
                    if nc < dp[mask | (1 << j)][1 + j]:
                        dp[mask | (1 << j)][1 + j] = nc
                        par[mask | (1 << j)][1 + j] = (mask, last)

    best = INF
    best_last: Optional[int] = None
    for last in range(1, n + 1):
        if dp[size - 1][last] == INF:
            continue
        nc = dp[size - 1][last] + cost[last][n + 1]
        if nc < best:
            best = nc
            best_last = last
    if best_last is None:
        return INF, []

    order: List[int] = []
    mask, last = size - 1, best_last
    while mask != 0:
        order.append(last - 1)
        p = par[mask][last]
        if p is None:
            return INF, []
        mask, last = p
    order.reverse()
    return best, order


def solve_free_order(start: str, end: str, middles: Sequence[str],
                     ref_date: dt.date, dep_min: int, plan_leg: PlanLegFn,
                     max_middles: int = 10
                     ) -> Tuple[List[str], float, Dict[Tuple[str, str], float]]:
    """自由顺序寻优：返回 (最优完整顺序[含首尾], 总成本, 各 OD 对的成本缓存)。"""
    middles = list(middles)
    if len(middles) > max_middles:
        raise ValueError(f"中间站点过多（>{max_middles} 个），请先分组或减少站点")
    labels = [start] + middles + [end]
    n = len(middles)
    cache: Dict[Tuple[str, str], float] = {}

    def cost(a: str, b: str) -> float:
        if a == b:
            return INF
        key = (a, b)
        if key not in cache:
            j = plan_leg(a, b, ref_date, dep_min)
            cache[key] = float(j.arr_min) if j is not None else INF
        return cache[key]

    mat = [[cost(labels[i], labels[j]) for j in range(n + 2)]
           for i in range(n + 2)]
    best, order = held_karp_order(mat, n)
    best_labels = [start] + [middles[i] for i in order] + [end]
    return best_labels, best, cache
