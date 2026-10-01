"""抓取并简化中国省级边界，供「地图漫游」模式的立体地图使用。

数据源：阿里云 DataV.GeoAtlas 公开边界服务
    https://geo.datav.aliyun.com/areas_v3/bound/100000_full.json   （省级面）
    https://geo.datav.aliyun.com/areas_v3/bound/{adcode}_full.json （市级面/中心点）

产出：
    web/data/china.geo.json                  省级轮廓（已 Douglas-Peucker 简化）
    src/travel_planner/data/city_coords.json 地级市中心点（城市目录的坐标底稿）

只做一次性生成；结果已提交到仓库，离线可用。想更新边界时重跑本脚本：

    python scripts/fetch_china_geo.py                 # 默认容差 0.012°
    python scripts/fetch_china_geo.py --tolerance 0.02 --keep-raw
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_OUT = PROJECT_ROOT / "web" / "data" / "china.geo.json"
COORD_OUT = PROJECT_ROOT / "src" / "travel_planner" / "data" / "city_coords.json"
RAW_DIR = PROJECT_ROOT / "data" / "geo_raw"

BASE = "https://geo.datav.aliyun.com/areas_v3/bound"
UA = "travel-planner/1.0 (personal use; geo cache fetch)"

SUFFIXES = ("特别行政区", "维吾尔自治区", "回族自治区", "壮族自治区", "自治区", "省", "市")

# 省级短名 -> 地图上的着色分组由 regions.json 决定；这里只做展示名清洗。
NAME_ALIASES = {
    "内蒙古自治区": "内蒙古",
    "广西壮族自治区": "广西",
    "西藏自治区": "西藏",
    "宁夏回族自治区": "宁夏",
    "新疆维吾尔自治区": "新疆",
    "香港特别行政区": "香港",
    "澳门特别行政区": "澳门",
    "台湾省": "台湾",
}


def short_name(name: str) -> str:
    if name in NAME_ALIASES:
        return NAME_ALIASES[name]
    for suffix in SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def fetch_json(url: str, cache: Path | None = None, retries: int = 3) -> dict:
    if cache is not None and cache.exists() and cache.stat().st_size > 0:
        return json.loads(cache.read_text(encoding="utf-8"))
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if cache is not None:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return payload
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"下载失败 {url}: {last_error}")


# ---------------------------------------------------------------- 几何工具
def ring_area(ring: Sequence[Sequence[float]]) -> float:
    total = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[i + 1][0], ring[i + 1][1]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def perp_distance(pt, a, b) -> float:
    (px, py), (ax, ay), (bx, by) = pt, a, b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def simplify(points: List[List[float]], tolerance: float) -> List[List[float]]:
    """Douglas-Peucker（迭代实现，避免深递归）。"""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack: List[Tuple[int, int]] = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        max_dist, index = 0.0, -1
        for i in range(first + 1, last):
            d = perp_distance(points[i], points[first], points[last])
            if d > max_dist:
                max_dist, index = d, i
        if index != -1 and max_dist > tolerance:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))
    return [p for p, k in zip(points, keep) if k]


def flatten_rings(geometry: dict) -> List[List[List[float]]]:
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        return [list(map(list, ring)) for ring in coords]
    if kind == "MultiPolygon":
        out: List[List[List[float]]] = []
        for polygon in coords:
            for ring in polygon:
                out.append(list(map(list, ring)))
        return out
    return []


def ring_bbox(ring: Sequence[Sequence[float]]) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def round_ring(ring: Iterable[Sequence[float]], digits: int = 4) -> List[List[float]]:
    out = [[round(float(p[0]), digits), round(float(p[1]), digits)] for p in ring]
    # 去重（取整后可能产生连续重复点）
    dedup: List[List[float]] = []
    for pt in out:
        if not dedup or dedup[-1] != pt:
            dedup.append(pt)
    if len(dedup) >= 3 and dedup[0] != dedup[-1]:
        dedup.append(dedup[0])
    return dedup


def build_provinces(national: dict, tolerance: float, min_island_area: float) -> List[dict]:
    provinces = []
    for feature in national.get("features", []):
        prop = feature.get("properties") or {}
        name = prop.get("name") or ""
        adcode = prop.get("adcode")
        if not name or not adcode:
            continue
        rings_raw = flatten_rings(feature.get("geometry") or {})
        if not rings_raw:
            continue
        rings = sorted(rings_raw, key=ring_area, reverse=True)
        kept: List[List[List[float]]] = []
        for idx, ring in enumerate(rings):
            if idx > 0 and ring_area(ring) < min_island_area:
                continue
            simple = simplify(ring, tolerance)
            simple = round_ring(simple)
            if len(simple) >= 4:
                kept.append(simple)
        if not kept:
            continue
        xs = [p[0] for ring in kept for p in ring]
        ys = [p[1] for ring in kept for p in ring]
        provinces.append({
            "adcode": int(adcode),
            "name": name,
            "short": short_name(name),
            "center": prop.get("center") or prop.get("centroid"),
            "bbox": [round(min(xs), 4), round(min(ys), 4), round(max(xs), 4), round(max(ys), 4)],
            "rings": kept,
        })
    provinces.sort(key=lambda p: p["adcode"])
    return provinces


def build_city_coords(provinces: Sequence[dict], raw_dir: Path) -> List[dict]:
    cities: List[dict] = []
    for prov in provinces:
        adcode = prov["adcode"]
        url = f"{BASE}/{adcode}_full.json"
        try:
            data = fetch_json(url, cache=raw_dir / f"{adcode}_full.json")
        except RuntimeError as exc:
            print(f"  ! {prov['short']} 市界下载失败：{exc}", file=sys.stderr)
            continue
        for feature in data.get("features", []):
            prop = feature.get("properties") or {}
            name = prop.get("name") or ""
            if not name or name == prov["name"]:
                continue
            center = prop.get("center") or prop.get("centroid")
            if not center or len(center) != 2:
                continue
            cities.append({
                "name": name,
                "short": short_name(name),
                "adcode": prop.get("adcode"),
                "province": prov["short"],
                "lon": round(float(center[0]), 4),
                "lat": round(float(center[1]), 4),
                "level": prop.get("level") or "city",
            })
    cities.sort(key=lambda c: (c["province"], c["name"]))
    return cities


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取并简化中国省级边界")
    parser.add_argument("--tolerance", type=float, default=0.012,
                        help="简化容差（度），越大文件越小，默认 0.012")
    parser.add_argument("--min-island-area", type=float, default=0.02,
                        help="丢弃小于该面积（平方度）的离岛，默认 0.02")
    parser.add_argument("--keep-raw", action="store_true", help="保留下载的原始 JSON")
    parser.add_argument("--skip-cities", action="store_true", help="不抓取市级中心点")
    args = parser.parse_args()

    raw_dir = RAW_DIR if args.keep_raw else RAW_DIR
    print("下载全国省级边界…")
    national = fetch_json(f"{BASE}/100000_full.json", cache=raw_dir / "100000_full.json")

    provinces = build_provinces(national, args.tolerance, args.min_island_area)
    payload = {
        "source": f"{BASE}/100000_full.json",
        "note": "个人本地使用的省级轮廓；DataV.GeoAtlas 公开服务，已简化。",
        "tolerance": args.tolerance,
        "provinces": provinces,
    }
    WEB_OUT.parent.mkdir(parents=True, exist_ok=True)
    WEB_OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                       encoding="utf-8")

    points = sum(len(r) for p in provinces for r in p["rings"])
    print(f"省级轮廓：{len(provinces)} 个，{points} 个点，"
          f"{WEB_OUT.stat().st_size / 1024:.0f} KB -> {WEB_OUT.relative_to(PROJECT_ROOT)}")

    if not args.skip_cities:
        print("下载市级中心点…")
        cities = build_city_coords(provinces, raw_dir)
        COORD_OUT.parent.mkdir(parents=True, exist_ok=True)
        COORD_OUT.write_text(json.dumps({
            "source": f"{BASE}/{{adcode}}_full.json",
            "note": "地级市中心点，作为城市目录的坐标底稿；人工整理后并入 city_catalog.json。",
            "cities": cities,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"城市中心点：{len(cities)} 个 -> {COORD_OUT.relative_to(PROJECT_ROOT)}")

    if not args.keep_raw:
        for leftover in raw_dir.glob("*.json"):
            leftover.unlink()
        try:
            raw_dir.rmdir()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
