"""一键导入路线素材：校验 → 写暂存区 → 补坐标 → （可选）正式化。

与脚本的分工：``scripts/build_route_catalog.py`` 是本地命令行流程，
这里把同一套校验与流程包成服务层，让网页上的「一键导入」也能用同一把尺子。

导入的路线**默认进暂存区**（先审后收）：写错了不会污染正式素材，
点一次「把暂存区正式化」才合并进 ``smart_routes.json``。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Dict, List

from . import routes as routes_mod

ROOT = Path(__file__).resolve().parents[2]
CONTENT_DIR = ROOT / "data" / "route_content"
STAGING_DIR = CONTENT_DIR / "_staging"
ARCHIVE_DIR = STAGING_DIR / "_archive"
SMART = CONTENT_DIR / "smart_routes.json"


def _validator():
    """复用构建脚本里的校验器（同一份规则，避免两套口径）。"""
    import importlib.util

    path = ROOT / "scripts" / "build_route_catalog.py"
    spec = importlib.util.spec_from_file_location("build_route_catalog", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                          # type: ignore[union-attr]
    return mod


def validate(raw: Dict) -> Dict:
    """校验一份素材（不落盘），返回逐条报告。"""
    mod = _validator()
    routes = [r for r in (raw.get("routes") or []) if isinstance(r, dict)]
    wrapped = {"updated": str(raw.get("updated") or dt.date.today().isoformat()),
               "note": raw.get("note") or "", "routes": []}
    seen, items, cities = set(), [], []
    for item in routes:
        rid = str(item.get("id") or "")
        if not rid:
            items.append({"id": "", "problems": ["缺少 id"]})
            continue
        if rid in seen:
            items.append({"id": rid, "problems": ["id 重复"]})
            continue
        seen.add(rid)
        copy = dict(item)
        wrapped["routes"].append(copy)
        for key in ("from_city", "to_city"):
            city = routes_mod._clean_city(copy.get(key))
            if city and city not in cities:
                cities.append(city)
        for seg in copy.get("segments") or []:
            for key in ("frm", "to"):
                city = routes_mod._clean_city(seg.get(key))
                if city and city not in cities:
                    cities.append(city)

    problems = mod.validate(wrapped)
    # 把整体问题归到具体条目上，页面才能逐条显示
    per_item: Dict[str, List[str]] = {str(i.get("id") or ""): list(i.get("problems") or [])
                                      for i in items}
    for line in problems:
        rid = ""
        for token in str(line).split(":")[1:2]:
            rid = token.strip()
        per_item.setdefault(rid, []).append(str(line))
    report_items = []
    for item in wrapped["routes"]:
        rid = str(item.get("id"))
        report_items.append({
            "id": rid,
            "name": item.get("name") or "",
            "kind": routes_mod.route_kind(item),
            "clever_score": routes_mod.clever_score(item),
            "value_score": routes_mod.value_score(item),
            "problems": per_item.get(rid, []),
        })
    for rid, issues in per_item.items():
        if rid and not any(x["id"] == rid for x in report_items):
            report_items.append({"id": rid, "name": "", "problems": issues})
    return {
        "routes": report_items,
        "count": len(wrapped["routes"]),
        "problems": problems,
        "cities": cities,
        "new_cities": [c for c in cities if routes_mod.coord_of(c, routes_mod._load_coords()) is None],
    }


def fill_missing_coords(cities: List[str], *, apply: bool = True) -> Dict:
    """给缺坐标的城市自动补坐标（本地目录 → 高德地理编码 + 省份粗筛）。

    直接复用命令行脚本，保证网页导入与本地流程**用同一套防查偏规则**。
    """
    import importlib.util

    path = ROOT / "scripts" / "fill_route_coords.py"
    spec = importlib.util.spec_from_file_location("fill_route_coords", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                          # type: ignore[union-attr]

    have = mod.local_coords()
    missing = [c for c in cities if routes_mod.coord_of(c, have) is None]
    if not missing:
        return {"needed": 0, "added": [], "skipped": [], "note": "坐标都齐了"}
    if not apply:
        return {"needed": len(missing), "added": [], "skipped": missing,
                "note": "预览模式：未查询坐标"}
    key = mod.amap_key()
    if not key:
        return {"needed": len(missing), "added": [], "skipped": missing,
                "note": "没有高德 key，无法自动补坐标（可先跑 scripts/fill_route_coords.py）"}
    added, skipped = [], []
    for city in missing:
        if city in mod.MANUAL:
            lat, lon = mod.MANUAL[city]
            mod._write_one(city, lat, lon, source="manual")
            added.append(city)
            continue
        hint = mod.HINTS.get(city) or {}
        result = mod.geocode(city, key, region=hint.get("city", ""))
        if not result:
            skipped.append(city)
            continue
        lat, lon = result
        ref = hint.get("ref")
        if ref and mod._haversine(ref, (lat, lon)) > mod.MAX_OFFSET_KM:
            skipped.append(city)
            continue
        if not mod.in_any_province(lat, lon):
            skipped.append(city)
            continue
        mod._write_one(city, lat, lon, source="amap-geocode")
        added.append(city)
    return {"needed": len(missing), "added": added, "skipped": skipped,
            "note": f"自动补 {len(added)} 个，查不到/查偏 {len(skipped)} 个"
                    + ("（查偏的会被丢弃，宁缺勿错）" if skipped else "")}


def write_staged(raw: Dict, *, filename: str = "") -> Dict:
    """把素材写进暂存区（不覆盖同名文件）。"""
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    name = (filename or "").strip()
    if not name:
        name = f"imported_{dt.date.today().isoformat().replace('-', '')}.json"
    if not name.endswith(".json"):
        name += ".json"
    if "/" in name or "\\" in name:
        return {"ok": False, "error": "文件名不能带路径分隔符"}
    path = STAGING_DIR / name
    if path.exists():
        return {"ok": False, "error": f"{name} 已存在，换个文件名或先正式化"}
    payload = {"updated": str(raw.get("updated") or dt.date.today().isoformat()),
               "collector": raw.get("collector") or "网页一键导入",
               "note": raw.get("note") or "",
               "routes": [dict(r) for r in (raw.get("routes") or [])]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "file": name, "routes": len(payload["routes"])}


def staged_summary() -> Dict:
    """暂存区概况：有哪些文件、多少条、各自来源。"""
    files, total = [], 0
    if STAGING_DIR.exists():
        for path in sorted(STAGING_DIR.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                files.append({"file": path.name, "routes": 0, "collector": "", "error": "读不了"})
                continue
            count = len(raw.get("routes") or [])
            total += count
            files.append({"file": path.name, "routes": count,
                          "collector": str(raw.get("collector") or ""),
                          "updated": str(raw.get("updated") or "")})
    archived = 0
    if ARCHIVE_DIR.exists():
        archived = len(list(ARCHIVE_DIR.glob("*.json")))
    return {"files": files, "total": total, "archived": archived,
            "note": "暂存区内容还没上线；正式化后会合并进 smart_routes.json 并归档原文件。"}


def promote() -> Dict:
    """把暂存区正式化（复用命令行脚本，逻辑只有一份）。"""
    import importlib.util

    path = ROOT / "scripts" / "promote_staged_routes.py"
    spec = importlib.util.spec_from_file_location("promote_staged_routes", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                          # type: ignore[union-attr]
    routes, collectors = mod.load_staged()
    if not routes:
        return {"ok": False, "error": "暂存区里没有可合并的路线"}
    rc = mod.main([])                                     # 走脚本正式流程（含归档）
    if rc != 0:
        return {"ok": False, "error": "正式化脚本返回失败"}
    return {"ok": True, "promoted": len(routes), "collectors": collectors,
            "note": "已合并进 smart_routes.json，原文件归档到 _staging/_archive"}
