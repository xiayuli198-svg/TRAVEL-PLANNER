"""规划引擎的数据模型：Trip / Leg / Journey。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Trip:
    """一趟列车在查询日的运行：stations/arr/dep 一一对应。

    时间均为「相对查询日 0 点的绝对分钟」，可跨天（≥1440）甚至为负
    （前一晚始发的过夜车）。
    """
    trip_id: str            # 12306 内部车次号
    code: str               # 对外车次，如 G101
    stations: List[str]     # 站码序列
    arr: List[int]
    dep: List[int]

    def __post_init__(self) -> None:
        n = len(self.stations)
        if not (len(self.arr) == n and len(self.dep) == n):
            raise ValueError("Trip 各字段长度不一致")


@dataclass
class Leg:
    train_no: str
    train_code: str
    from_station: str       # 站码
    from_name: str
    to_station: str
    to_name: str
    dep_min: int
    arr_min: int
    price: Optional[float] = None      # 预估票价（元）；接驳伪段为附加费或 None


@dataclass
class Journey:
    legs: List[Leg]
    dep_min: int
    arr_min: int
    price: Optional[float] = None      # 全程预估总价（元）

    @property
    def transfers(self) -> int:
        return len(self.legs) - 1

    @property
    def duration(self) -> int:
        return self.arr_min - self.dep_min

    @property
    def wait_total(self) -> int:
        """换乘等待总时长（分钟）。"""
        total = 0
        for a, b in zip(self.legs, self.legs[1:]):
            total += b.dep_min - a.arr_min
        return total
