"""票价估算（纯函数，元/公里费率按公开运价规则近似）。

注意：一切为「估算」——高铁二等座约 0.42 元/km（分线路与浮动折扣有出入），
普速按硬座 0.105 元/km 上下。飞猪航班价格为真实票价（另存 flights.price）。
"""
from __future__ import annotations

import math
from typing import Optional

# 元/公里（里程 = 车站间大圆距离 × 绕行系数，见 KM_FACTOR）
RAIL_RATES = {
    # 列车类别(前缀) -> {席别: 元/km}
    "G": {"商务": 1.30, "一等": 0.70, "二等": 0.42},
    "C": {"商务": 1.30, "一等": 0.70, "二等": 0.42},
    "D": {"软卧": 0.58, "一等": 0.50, "二等": 0.31},
    "Z": {"软卧": 0.29, "硬卧": 0.19, "硬座": 0.105},
    "T": {"软卧": 0.29, "硬卧": 0.19, "硬座": 0.105},
    "K": {"软卧": 0.29, "硬卧": 0.19, "硬座": 0.105},
}
DEFAULT_RATE = 0.105          # 数字普客等未匹配类别按硬座
DEFAULT_SEAT = "二等"          # 高铁默认席别（最省目标也用同席别横向比较）

KM_FACTOR = {"G": 1.22, "C": 1.22, "D": 1.25, "Z": 1.35, "T": 1.35, "K": 1.35}
DEFAULT_KM_FACTOR = 1.32

# 无坐标兜底：按列车类别取平均站间距（km/停站区间），估算精度约 ±30%
AVG_SPACING_KM = {"G": 55.0, "C": 45.0, "D": 50.0, "Z": 62.0, "T": 55.0, "K": 38.0}
DEFAULT_AVG_SPACING = 32.0

# 同城接驳/城市转场附加费（估算，元）
RAIL_TRANSFER_FEE = 5       # 同城站间地铁/公交转场
AIR_RAIL_FEE = 30           # 机场↔铁路站（机场线/大巴/打车分摊）
AIR_TRANSFER_FEE = 30       # 同城异机场转场

# 机票估算兜底（当真实价缺失）
AIR_KM_FACTOR = 1.12
AIR_RATE_CNY_KM = 0.72      # 元/km（经济舱含部分附加费近似）
AIR_BASE_FEE = 110          # 机建+燃油+杂项近似

# 绿皮省钱模式：高铁/动车只允许「本段票价 ≤ 此值」的短途衔接
GREEN_MAX_HSR_FARE = 50.0

CONVENTIONAL_CLASSES = {"Z", "T", "K"}   # 普速类（纯数字车次归入 K）


def is_conventional(code: str) -> bool:
    return train_class(code) in CONVENTIONAL_CLASSES


def hsr_segment_ok(code: str, price, max_fare: float = GREEN_MAX_HSR_FARE) -> bool:
    """绿皮模式下的段级约束：普速任意；高铁/动车段价 ≤ 上限。"""
    if is_conventional(code):
        return True
    return price is not None and float(price) <= max_fare


def train_class(code: str) -> str:
    code = (code or "").upper()
    for prefix in ("G", "C", "D", "Z", "T", "K"):
        if code.startswith(prefix):
            return prefix
    return "K"  # 数字车次等按普速


def rail_rate(code: str, seat: Optional[str] = None) -> float:
    cls = train_class(code)
    rates = RAIL_RATES.get(cls)
    if not rates:
        return DEFAULT_RATE
    seat = seat or (DEFAULT_SEAT if cls in ("G", "C", "D") else "硬座")
    return rates.get(seat, DEFAULT_RATE)


def km_factor(code: str) -> float:
    cls = train_class(code)
    return KM_FACTOR.get(cls, DEFAULT_KM_FACTOR)


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """大圆距离（km）。"""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def avg_spacing_km(code: str) -> float:
    return AVG_SPACING_KM.get(train_class(code), DEFAULT_AVG_SPACING)


def rail_segment_price(code: str, km: float) -> float:
    """铁路某段票价估算 = 里程 × 绕行系数 × 分席别单价。"""
    return km * km_factor(code) * rail_rate(code)


def air_est_price(km: float) -> float:
    return max(150.0, km * AIR_KM_FACTOR * AIR_RATE_CNY_KM + AIR_BASE_FEE)
