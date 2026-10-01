"""环线 Ultra：北京出发与河北出发的**穷游环线**攻略库。

跟「省钱/舒适/巧思路线」的区别：那个库是**单点走法**（一段车怎么买怎么换），
这个库是**整条环线**（几天、怎么绕一圈、每天住哪花多少、哪些坑），
所以更适合环线出行：点一次就能看到一条能直接照着走完的多日安排。

数据来自 `data/route_content/ultra_loops.json`（子代理收集 + `build_ultra_loops.py`
校验合并：出发地白名单、首尾闭环、天数与每日安排条数一致、不许写车次号与精确时刻）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
CONTENT = ROOT / "data" / "route_content" / "ultra_loops.json"

_cache: Dict[str, object] = {"mtime": None, "data": {}}

#: 出发地区域 → 展示名（北京单独一档，河北按城市列）
ORIGIN_GROUPS = {"北京": "北京", "河北": "河北"}


def _load() -> dict:
    try:
        mtime = CONTENT.stat().st_mtime
    except OSError:
        return {}
    if _cache["mtime"] != mtime:
        try:
            _cache["data"] = json.loads(CONTENT.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            _cache["data"] = {}
        _cache["mtime"] = mtime
    return _cache["data"]  # type: ignore[return-value]


def loops() -> List[dict]:
    return list(_load().get("loops") or [])


def updated() -> str:
    return str(_load().get("updated") or "")


def note() -> str:
    return str(_load().get("note") or "")


def origins() -> List[dict]:
    """出发地清单：先北京、再河北各市（各带条数），用于页面筛选。"""
    counts: Dict[str, int] = {}
    provinces: Dict[str, str] = {}
    for item in loops():
        origin = str(item.get("origin") or "")
        if not origin:
            continue
        counts[origin] = counts.get(origin, 0) + 1
        provinces[origin] = str(item.get("origin_province") or "")
    out = []
    for name in sorted(counts, key=lambda x: (provinces.get(x) != "北京", -counts[x], x)):
        out.append({"name": name, "province": provinces.get(name, ""), "count": counts[name]})
    return out


def card(item: dict) -> dict:
    """列表卡片：够一眼判断「这条线适不适合我」，细节留到展开时再看。"""
    days = int(item.get("days") or 0)
    low = int(item.get("budget_low") or 0)
    high = int(item.get("budget_high") or 0)
    per_day = round((low + high) / 2 / days) if days else 0
    cities = list(item.get("cities") or [])
    # 小环线 = 省内/周边几站；大环线 = 跨省沿大动脉一路串下去（≥8 天 ≥6 城，见 build 校验器）
    scale = str(item.get("scale") or ("large" if days >= 8 and len(set(cities)) >= 6 else "small"))
    return {
        "id": item.get("id"), "name": item.get("name"),
        "origin": item.get("origin"), "origin_province": item.get("origin_province"),
        "days": days, "budget_low": low, "budget_high": high, "per_day": per_day,
        "scale": scale, "scale_label": "大环线" if scale == "large" else "小环线",
        "hub_line": item.get("hub_line") or "",
        "cities": cities,
        "city_count": len(cities),
        "transport": list(item.get("transport") or []),
        "regions": list(item.get("regions") or []),
        "summary": item.get("summary") or "",
        "highlights": list(item.get("highlights") or []),
        "best_season": item.get("best_season") or "",
        "confidence": item.get("confidence") or "",
        "loop": (cities[:1] == cities[-1:]),
        "source": item.get("source") or "",
        "source_url": item.get("source_url") or "",
    }


def detail(loop_id: str) -> Optional[dict]:
    for item in loops():
        if item.get("id") == loop_id:
            return {**card(item), "itinerary": list(item.get("itinerary") or []),
                    "save_tips": list(item.get("save_tips") or []),
                    "comfort_tips": list(item.get("comfort_tips") or []),
                    "fit_for": list(item.get("fit_for") or []),
                    "watch_out": item.get("watch_out") or ""}
    return None


def query(origin: str = "", days_max: int = 0, budget_max: int = 0, q: str = "",
          sort: str = "value", scale: str = "") -> List[dict]:
    """筛选：出发地 / 最长天数 / 预算上限 / 关键字 / 大小环线；排序默认「性价比」。"""
    items = [card(x) for x in loops()]
    if origin:
        items = [x for x in items if x["origin"] == origin]
    if scale in ("large", "small"):
        items = [x for x in items if x["scale"] == scale]
    if days_max:
        items = [x for x in items if 0 < x["days"] <= days_max]
    if budget_max:
        items = [x for x in items if x["budget_low"] <= budget_max]
    key = (q or "").strip()
    if key:
        def hit(x: dict) -> bool:
            blob = "".join([str(x["name"]), str(x["summary"]), "".join(x["cities"]),
                            "".join(x["highlights"]), str(x["origin"]), str(x["hub_line"])])
            return key in blob
        items = [x for x in items if hit(x)]
    if sort == "days":
        items.sort(key=lambda x: (x["days"], x["budget_low"]))
    elif sort == "budget":
        items.sort(key=lambda x: (x["budget_low"], x["days"]))
    elif sort == "origin":
        items.sort(key=lambda x: (x["origin_province"] != "北京", x["origin"], x["days"]))
    elif sort == "scale":                    # 大环线优先，再看天数
        items.sort(key=lambda x: (x["scale"] != "large", -x["days"], x["budget_low"]))
    else:                                   # value：日均越低越靠前，天数长的略加权
        items.sort(key=lambda x: (x["per_day"] * (1 - min(x["days"], 14) * 0.01), x["days"]))
    return items


def stats() -> dict:
    items = [card(x) for x in loops()]
    if not items:
        return {"loops": 0, "origins": 0, "days": [0, 0], "budget": [0, 0], "per_day": [0, 0]}
    per_day = sorted(x["per_day"] for x in items if x["per_day"])
    large = [x for x in items if x["scale"] == "large"]
    return {
        "loops": len(items),
        "origins": len({x["origin"] for x in items}),
        "beijing": sum(1 for x in items if x["origin_province"] == "北京"),
        "hebei": sum(1 for x in items if x["origin_province"] == "河北"),
        "large": len(large),
        "small": len(items) - len(large),
        "large_cities": max((x["city_count"] for x in large), default=0),
        "days": [min(x["days"] for x in items), max(x["days"] for x in items)],
        "large_days": [min((x["days"] for x in large), default=0), max((x["days"] for x in large), default=0)],
        "budget": [min(x["budget_low"] for x in items), max(x["budget_high"] for x in items)],
        "per_day": [per_day[0], per_day[-1]] if per_day else [0, 0],
        "high": sum(1 for x in items if x["confidence"] == "high"),
        "low": sum(1 for x in items if x["confidence"] == "low"),
        "cities": sorted({c for x in items for c in x["cities"]}),
    }
