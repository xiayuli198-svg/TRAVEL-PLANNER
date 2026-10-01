"""城市目录 / 大区 / 经典环线的静态数据访问层。

数据文件位于 ``src/travel_planner/data/``：

- ``regions.json``        大区划分（东北 / 江南 / 青藏…），驱动地图上的分区块与环境色
- ``city_catalog.json``   城市旅游目录（评分、简介、半日/一日路线、看点、坐标、是否通铁路）
- ``loop_presets.json``   经典环线（大兴安岭环线、河西走廊…），供地图上一键飞到并成行程

城市目录由 ``scripts/build_city_catalog.py`` 从 ``data/city_content/*.json``
（人工整理的文案）与 ``city_coords.json``（DataV 抓取的坐标）合并生成；
人工可以直接编辑 ``city_catalog.json``，它是唯一的运行时数据源。
"""
from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

DATA_DIR = Path(__file__).resolve().parent / "data"

# kind 取值（与前端图标映射保持一致）
HIGHLIGHT_KINDS = (
    "博物馆", "古迹", "自然", "街区", "古镇", "美食", "夜景", "演出",
    "海岛", "雪山", "湖泊", "寺庙", "园林", "工业", "温泉", "边境", "主题乐园",
)

CITY_SUFFIXES = ("市", "地区", "自治州", "盟", "特别行政区")


def _load(name: str) -> dict:
    path = DATA_DIR / name
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _norm(city: str) -> str:
    value = (city or "").strip()
    for suffix in CITY_SUFFIXES:
        if value.endswith(suffix) and len(value) > len(suffix):
            return value[: -len(suffix)]
    return value


# ------------------------------------------------------------------ 大区
@functools.lru_cache(maxsize=1)
def regions() -> List[dict]:
    return list(_load("regions.json").get("regions") or [])


@functools.lru_cache(maxsize=1)
def _region_index() -> Dict[str, dict]:
    index: Dict[str, dict] = {}
    for region in regions():
        index[region["id"]] = region
        index[region["name"]] = region
        for province in region.get("provinces") or []:
            index.setdefault(f"province::{province}", region)
    return index


def region_by(value: str) -> Optional[dict]:
    """按大区 id / 名称 / 省名查找大区。"""
    key = (value or "").strip()
    return _region_index().get(key) or _region_index().get(f"province::{key}")


def region_of_province(province: str) -> Optional[str]:
    region = _region_index().get(f"province::{_norm(province)}")
    return region["name"] if region else None


# ------------------------------------------------------------------ 城市
@functools.lru_cache(maxsize=1)
def _cities() -> List[dict]:
    data = _load("city_catalog.json")
    out: List[dict] = []
    for item in data.get("cities") or []:
        city = dict(item)
        city.setdefault("region", region_of_province(city.get("province", "")) or "")
        city.setdefault("hours", 4)
        city.setdefault("days", 1)
        city.setdefault("highlights", [])
        city.setdefault("tags", [])
        city.setdefault("rail", True)
        city.setdefault("plan_name", city["name"])
        out.append(city)
    return out


@functools.lru_cache(maxsize=1)
def _city_index() -> Dict[str, dict]:
    index: Dict[str, dict] = {}
    for city in _cities():
        index[city["name"]] = city
        index.setdefault(_norm(city["name"]), city)
        for alias in city.get("aliases") or []:
            index.setdefault(alias, city)
    return index


def city(name: str) -> Optional[dict]:
    key = (name or "").strip()
    if not key:
        return None
    return _city_index().get(key) or _city_index().get(_norm(key))


def cities(region: Optional[str] = None, keyword: str = "",
           rail_only: bool = False) -> List[dict]:
    """按大区/关键字过滤城市目录，按推荐指数降序。"""
    region_name = region_by(region)["name"] if region else None
    key = (keyword or "").strip()
    out = []
    for item in _cities():
        if region_name and item["region"] != region_name:
            continue
        if rail_only and not item.get("rail", True):
            continue
        if key and key not in item["name"] and key not in (item.get("province") or ""):
            continue
        out.append(item)
    out.sort(key=lambda c: (-float(c.get("score") or 0), c["name"]))
    return out


def plan_label(name: str) -> str:
    """目录城市名 -> 规划时实际使用的地点名（无铁路的地级州退回驻地城市）。"""
    item = city(name)
    if not item:
        return (name or "").strip()
    return item.get("plan_name") or item["name"]


def display_name(plan_name: str) -> str:
    """规划地点名 -> 目录里的展示名（反向查找，用于把行程的 to 映射回卡片）。"""
    for item in _cities():
        if item.get("plan_name") == plan_name or item["name"] == plan_name:
            return item["name"]
    return plan_name


# ------------------------------------------------------------------ 环线
def _bounds(points: Sequence[Sequence[float]]) -> Optional[List[float]]:
    if not points:
        return None
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return [min(lons), min(lats), max(lons), max(lats)]


@functools.lru_cache(maxsize=1)
def loops() -> List[dict]:
    out: List[dict] = []
    for raw in _load("loop_presets.json").get("loops") or []:
        item = dict(raw)
        stops: List[dict] = []
        missing: List[str] = []
        for name in item.get("cities") or []:
            entry = city(name)
            if not entry:
                missing.append(name)
                continue
            stops.append({
                "name": entry["name"],
                "plan_name": entry.get("plan_name") or entry["name"],
                "province": entry.get("province", ""),
                "region": entry.get("region", ""),
                "lon": entry.get("lon"),
                "lat": entry.get("lat"),
                "score": entry.get("score"),
                "rail": entry.get("rail", True),
            })
        item["stops"] = stops
        item["names"] = [s["name"] for s in stops]
        item["plan_names"] = [s["plan_name"] for s in stops]
        item["missing"] = missing
        item["regions"] = sorted({s["region"] for s in stops if s["region"]})
        item["cross_region"] = len(item["regions"]) > 1
        item["bounds"] = _bounds([[s["lon"], s["lat"]] for s in stops
                                  if s["lon"] is not None and s["lat"] is not None])
        item.setdefault("plan_mode", "rail")
        out.append(item)
    out.sort(key=lambda x: (x.get("order", 999), x["name"]))
    return out


def loop(loop_id: str) -> Optional[dict]:
    for item in loops():
        if item.get("id") == loop_id or item.get("name") == loop_id:
            return item
    return None


# ------------------------------------------------------------------ 概览
def stats() -> dict:
    all_cities = _cities()
    by_region: Dict[str, int] = {}
    for item in all_cities:
        by_region[item["region"]] = by_region.get(item["region"], 0) + 1
    return {
        "cities": len(all_cities),
        "regions": len(regions()),
        "loops": len(loops()),
        "rail_cities": sum(1 for c in all_cities if c.get("rail", True)),
        "by_region": by_region,
    }


def reload() -> None:
    """清缓存（改完 data/*.json 后无需重启进程）。"""
    for fn in (_cities, _city_index, regions, _region_index, loops):
        fn.cache_clear()
