"""把世界城市 / 经典环线 / 世界之最的文案合并成运行时目录。

输入
    data/world_content/*.json          人工/子任务整理的世界资料
      · 城市：{"region": "世界", "cities": [...]}（可多份，按 name 合并）
      · 环线：{"schema": 1, "loops": [...]}
      · 世界之最：{"schema": 1, "records": [...]}
输出
    src/travel_planner/data/world_catalog.json

校验项：城市名唯一、坐标在世界范围内、看点类型合法、guide 天数与行程条数一致、
环线引用的城市都存在、世界之最分类合法且数值非空。

用法：
    python scripts/build_world_catalog.py                # 生成 + 报告
    python scripts/build_world_catalog.py --check-only    # 只校验
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTENT_DIR = PROJECT_ROOT / "data" / "world_content"
OUT_PATH = PROJECT_ROOT / "src" / "travel_planner" / "data" / "world_catalog.json"

KINDS = {"博物馆", "古迹", "自然", "街区", "古镇", "美食", "夜景", "演出",
         "海岛", "雪山", "湖泊", "寺庙", "园林", "工业", "温泉", "边境", "主题乐园"}
CATEGORIES = {"自然", "工程", "人文", "气候"}
AREAS = {"东亚", "东南亚", "南亚", "中亚", "中东", "欧洲", "北美", "拉美", "非洲",
         "大洋洲", "亚洲", "跨洲"}


def load(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 world_catalog.json")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    cities: Dict[str, dict] = {}
    loops: List[dict] = []
    records: List[dict] = []
    problems: List[str] = []
    notes: List[str] = []

    files = sorted(CONTENT_DIR.glob("*.json")) if CONTENT_DIR.exists() else []
    if not files:
        print(f"没有找到世界资料：{CONTENT_DIR}/*.json", file=sys.stderr)
        return 1

    for path in files:
        data = load(path) or {}
        for raw in data.get("cities") or []:
            name = (raw.get("name") or "").strip()
            if not name:
                problems.append(f"{path.name}: 有城市缺 name")
                continue
            if name in cities:
                notes.append(f"{name}: 在 {path.name} 里重复出现，取后者")
            city = dict(raw)
            city["name"] = name
            city.setdefault("area", "其他")
            if city.get("area") not in AREAS:
                problems.append(f"{name}: 未知区域 {city.get('area')!r}")
            if city.get("lat") is None or city.get("lon") is None:
                problems.append(f"{name}: 缺坐标")
            elif not (-90 <= city["lat"] <= 90 and -180 <= city["lon"] <= 180):
                problems.append(f"{name}: 坐标越界 {city['lat']},{city['lon']}")
            for h in city.get("highlights") or []:
                if isinstance(h, dict) and (h.get("kind") or "") not in KINDS:
                    problems.append(f"{name}: 未知看点类型 {h.get('kind')!r}")
            guide = city.get("guide") or {}
            if guide:
                days = guide.get("days")
                itinerary = guide.get("itinerary") or []
                if not isinstance(days, int) or not 1 <= days <= 8:
                    problems.append(f"{name}: guide.days 非法 {days!r}")
                elif len(itinerary) != days:
                    problems.append(f"{name}: 行程 {len(itinerary)} 条 ≠ 天数 {days}")
                for key in ("summary", "transport", "stay", "eat", "budget"):
                    if not guide.get(key):
                        problems.append(f"{name}: guide 缺 {key}")
            else:
                problems.append(f"{name}: 缺 guide")
            city["night"] = bool(city.get("night"))
            cities[name] = city

        for raw in data.get("loops") or []:
            item = dict(raw)
            item.setdefault("plan_mode", "reference")
            loops.append(item)
        for raw in data.get("records") or []:
            item = dict(raw)
            if item.get("category") not in CATEGORIES:
                problems.append(f"{item.get('name')}: 未知分类 {item.get('category')!r}")
            for key in ("id", "name", "value", "blurb", "visit", "lat", "lon"):
                if not item.get(key):
                    problems.append(f"{item.get('name') or item.get('id')}: 缺 {key}")
            records.append(item)

    # 环线引用的城市必须存在
    for item in loops:
        missing = [c for c in (item.get("cities") or []) if c not in cities]
        if missing:
            problems.append(f"环线「{item.get('name')}」引用了不存在的城市：{missing}")
        item["regions"] = sorted({cities[c]["area"] for c in (item.get("cities") or []) if c in cities})
        item["cross_region"] = len(item["regions"]) > 1

    loops.sort(key=lambda x: (x.get("order", 999), x.get("name", "")))
    records.sort(key=lambda x: (x.get("category", ""), x.get("name", "")))

    print(f"世界城市 {len(cities)} 座 · 环线 {len(loops)} 条 · 世界之最 {len(records)} 条")
    by_area: Dict[str, int] = {}
    for c in cities.values():
        by_area[c["area"]] = by_area.get(c["area"], 0) + 1
    print("  区域分布：" + "、".join(f"{k} {v}" for k, v in sorted(by_area.items(), key=lambda x: -x[1])))
    print("  夜游城市：" + str(sum(1 for c in cities.values() if c["night"])))
    if notes:
        print(f"\n提示（{len(notes)}）：")
        for n in notes:
            print("  ·", n)
    if problems:
        print(f"\n问题（{len(problems)}）：", file=sys.stderr)
        for p in problems:
            print("  !", p, file=sys.stderr)
    if args.check_only:
        return 1 if problems else 0

    payload = {
        "schema": 1,
        "note": "地球模式的运行时目录，由 scripts/build_world_catalog.py 生成；文案源自 data/world_content/*.json。",
        "cities": list(cities.values()),
        "loops": loops,
        "records": records,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已写入 {OUT_PATH.relative_to(PROJECT_ROOT)}（{OUT_PATH.stat().st_size / 1024:.0f} KB）")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
