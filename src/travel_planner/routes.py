"""省钱 / 舒适路线库：把「有人真走过、值一提的走法」整理成可查、可打分、可标注来源的数据。

定位（很重要）：**这些数字是参考，不是实时票价**。时刻表与票价随时会变，所以每条都带
``updated``（口径日期）、``source``（来源）与 ``confidence``（可信度）。规划时用本地时刻表
算出来的方案才是「可执行」的，这里提供的是**策略与经验**：什么时候该坐夜车、哪段该买硬座、
哪些地方住城外更划算。

数据流：
    data/route_content/*.json   （人写的原始素材，按 id 合并）
        ↓ scripts/build_route_catalog.py 校验
    src/travel_planner/data/route_catalog.json （运行时目录，别手改）
        ↓ routes.py
    数据台 / 规划页展示（排序、筛选、按城市命中）
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

DATA_DIR = Path(__file__).resolve().parent / "data"
CONTENT_DIR = Path(__file__).resolve().parents[2] / "data" / "route_content"
CATALOG = DATA_DIR / "route_catalog.json"
COORDS = DATA_DIR / "city_coords.json"
COORDS_EXTRA = DATA_DIR / "city_coords_extra.json"
#: 路线库自己的补充坐标（敦煌、阳朔这类 DataV 市级目录里没有的县级点）
COORDS_ROUTES = DATA_DIR / "city_coords_routes.json"

_cache: Dict[str, object] = {"key": None, "data": None}

#: 交通方式的展示名与配色键
MODE_LABEL = {
    "train": "火车", "rail": "火车", "high_speed": "高铁", "bus": "大巴",
    "car": "自驾", "boat": "船", "fly": "飞机", "walk": "步行", "metro": "地铁",
}
TIER_ORDER = ["省钱", "舒适", "观景", "打卡"]


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


#: 车站名常见方位后缀（北京西/上海虹桥/广州东/黄山北…）
_DIRECTIONS = ("东", "南", "西", "北", "站")


def _clean_city(name) -> str:
    """把素材里不太规范的城市名收拾干净，便于查坐标。

    收集来的写法五花八门：``西宁（环湖）````宏村/汤口````黄山北站``。
    统一去掉括号注释、取斜杠前的主要那一个、去掉「站」后缀。
    注意**不**去掉方位字：「北京西」与「北京」是两个不同车站，坐标解析请在
    ``coord_of()`` 里做「先精确、再去方位」的降级查找。
    """
    text = str(name or "").strip()
    if not text:
        return ""
    for sep in ("（", "(", "："):
        if sep in text:
            text = text.split(sep, 1)[0]
    if "/" in text:
        text = text.split("/", 1)[0]
    text = text.strip()
    while text.endswith("站") and len(text) > 1:
        text = text[:-1]
    return text


def coord_of(name, coords: Dict[str, Sequence[float]]):
    """按名字找坐标，三档降级：

    1. 精确匹配；
    2. 逐步去掉末尾 1-3 字（北京西/上海虹桥/黄山北 → 城市）；
    3. **前缀匹配**（大理 → 大理白族自治州、伊犁 → 伊犁哈萨克自治州）——
       取最短的候选，避免「上海」被「上海虹桥」之类更长的键抢走。

    第 2、3 档都要求候选至少 2 个字，防止把「上海」配到「上」。
    """
    text = _clean_city(name)
    if not text:
        return None
    if text in coords:
        return coords[text]
    for cut in range(1, 4):
        if len(text) - cut < 2:
            break
        shorter = text[: len(text) - cut]
        if shorter in coords:
            return coords[shorter]
    if len(text) >= 2:
        candidates = [k for k in coords if k.startswith(text) and len(k) > len(text)]
        if candidates:
            return coords[min(candidates, key=len)]
    return None


def _load_coords() -> Dict[str, Sequence[float]]:
    """城市坐标：两个文件的格式不一样，都要吃得下。

    * ``city_coords.json``：``[{"name": "北京市", "short": "北京", "lon": …, "lat": …}, …]``
    * ``city_coords_extra.json``：``{"cities": [{"name": …, "lon": …, "lat": …}, …]}``
    路线里的城市名用「北京」这种简称，所以 name 与 short 两种写法都登记。
    """
    out: Dict[str, Sequence[float]] = {}

    def add(name: str, lon, lat) -> None:
        if not name or lon is None or lat is None:
            return
        try:
            out.setdefault(str(name), [float(lat), float(lon)])
        except (TypeError, ValueError):
            return

    def eat(items) -> None:
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            return
        for row in items:
            if isinstance(row, dict):
                add(row.get("name"), row.get("lon"), row.get("lat"))
                add(row.get("short"), row.get("lon"), row.get("lat"))
            elif isinstance(row, (list, tuple)) and len(row) >= 3:
                add(row[0], row[1], row[2])

    for path in (COORDS, COORDS_EXTRA, COORDS_ROUTES):
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict) and "cities" in raw:
            eat(raw["cities"])
        else:
            eat(raw)
    # 「北京市」这种全称也映射到同名坐标，方便直接用城市名查
    for name, point in list(out.items()):
        if not name.endswith("市"):
            out.setdefault(name + "市", point)
    return out


def value_score(route: Dict) -> int:
    """性价比分（0-100）：以「每小时的交通花费」为主，速度与舒适度做小幅加成。

    标定依据（公开票价的常见量级）：
    * 普速硬座 ≈ 5-10 元/小时（成都→拉萨硬座约 8 元/小时）→ 高分
    * 普速硬卧 ≈ 15-25 元/小时
    * 高铁二等座 ≈ 50-120 元/小时（京沪 550 元 4.5 小时 ≈ 120）
    * 机票均价 ≈ 100-200 元/小时
    所以 8 元/小时拿满分，120 元/小时约 55 分（高铁是「花钱买时间」而不是不划算）。

    踩过的坑：第一版按「总价 ÷ 总时长」线性折算，1200 元/40 小时算出 89 分 —— 长行程被
    变相奖励；第二版阈值定得太低，把高铁全打到 10 分以下。现在是分段曲线 + 时间惩罚
    + 舒适度小幅加成（±8），三者的量级都经过上面这些真实价位核对。
    """
    hours = float(route.get("total_hours") or 0) or 1.0
    low = float(route.get("cost_low") or 0)
    per_hour = low / hours
    if per_hour <= 8:
        base = 100.0
    elif per_hour >= 130:
        base = 45.0
    else:
        if per_hour <= 30:                      # 8-30 元/小时：普速卧铺档
            base = 100 - (per_hour - 8) * 20 / 22
        else:                                   # 30-130 元/小时：高铁/机票档
            base = 80 - (per_hour - 30) * 35 / 100
    # 时间成本：8 小时以内不扣，之后线性扣到 25 分
    time_penalty = 0.0 if hours <= 8 else min(25, (hours - 8) / 40 * 25)
    # 舒适度做小幅加成（最多 ±8 分），避免「便宜但难受到没人愿意坐」排第一
    comfort = route.get("comfort_score")
    comfort_adj = 0.0 if comfort is None else (float(comfort) - 60) * 8 / 40
    return int(max(0, min(100, round(base - time_penalty + comfort_adj))))


#: 巧思分的加分项（标签是白名单，校验器会拦错拼；防止随手编标签刷分）
CLEVER_WEIGHTS = {
    "extra_stops": 30,      # 顺路多玩了地方：稍微多花钱，换更多体验
    "detour_worth": 22,     # 绕路但值得：比直达贵一点、体验翻倍
    "night_move": 22,       # 夜车/夜船把赶路变成睡觉，省时又省一晚住宿
    "cheap_upgrade": 20,    # 同一条线用更少的钱拿到更好的位置（软卧 vs 一等座等）
    "shoulder": 16,         # 淡季/错峰/反向走，避开人潮与高价
    "combo_ticket": 16,     # 联程/同车接续/中途下车，一张票玩两段
    "local_trick": 16,      # 本地经验：站外换乘、住城外、慢车看同一段风景
    "season_window": 14,    # 抓住很短的窗口（油菜花期、极光、开江、日出）
}


def clever_score(route: Dict) -> int:
    """巧思分（0-100）：这条路线「别出心裁」的程度，与省钱/舒适各自独立。

    为什么单列一个维度：同一段北京→拉萨，多花 200 元在西安下车逛两天、或在青海湖停一晚，
    **性价比分反而更低**（更贵更久），但体验完全不同 —— 只按性价比排序会把这类走法沉底。

    计分：``clever_tags`` 逐项按上表加分（白名单），再加作者显式给的 ``clever_extra``（0-40），
    上限 100。分数 ≥70 的路线**必须**写 ``smart_tips``（妙在哪），校验器会强制，避免分数通胀。
    """
    score = 0.0
    for tag in route.get("clever_tags") or []:
        score += CLEVER_WEIGHTS.get(str(tag), 0)
    extra = route.get("clever_extra")
    if isinstance(extra, (int, float)):
        score += max(0.0, min(40.0, float(extra)))
    return int(max(0, min(100, round(score))))


def catalog() -> Dict:
    """读运行时目录；文件变了自动重载（数据台点刷新就能看到新路线）。"""
    key = (_mtime(CATALOG), _mtime(COORDS), _mtime(COORDS_EXTRA), _mtime(COORDS_ROUTES))
    if _cache["key"] == key and _cache["data"] is not None:
        return _cache["data"]  # type: ignore[return-value]
    data: Dict = {"updated": "", "note": "", "routes": []}
    if CATALOG.exists():
        try:
            loaded = json.loads(CATALOG.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass
    coords = _load_coords()
    for route in data.get("routes") or []:
        route.setdefault("tier", "观景")
        route.setdefault("themes", [])
        route.setdefault("segments", [])
        route.setdefault("save_tips", [])
        route.setdefault("comfort_tips", [])
        route.setdefault("fit_for", [])
        route.setdefault("confidence", "low")
        # 城市名归一后再算分/查坐标：否则「西宁（环湖）」这种写法会让按城市筛选失效
        for key in ("from_city", "to_city"):
            route[key] = _clean_city(route.get(key))
        route["value_score"] = value_score(route)
        route["clever_score"] = clever_score(route)
        route["kind"] = route_kind(route)
        route["kind_label"] = KIND_LABEL[route["kind"]]
        if route.get("comfort_score") is None:
            route["comfort_score"] = 60
        # 环线（起终点同城）也要能在图上落点：取城市坐标，加点偏移避免叠在一起
        for idx, city_key in enumerate(("from_city", "to_city")):
            city = _clean_city(route.get(city_key))
            point = coord_of(city, coords)
            if point:
                route[f"{city_key}_lat"] = point[0]
                route[f"{city_key}_lon"] = point[1]
            else:
                route[f"{city_key}_lat"] = None
                route[f"{city_key}_lon"] = None
        route["modes"] = sorted({MODE_LABEL.get(s.get("mode", ""), s.get("mode", ""))
                                 for s in route["segments"] if s.get("mode")})
    _cache["key"] = key
    _cache["data"] = data
    return data


def routes() -> List[Dict]:
    return list(catalog().get("routes") or [])


def updated() -> str:
    return str(catalog().get("updated") or "")


def route(route_id: str) -> Optional[Dict]:
    for item in routes():
        if item.get("id") == route_id:
            return item
    return None


def categories() -> List[str]:
    seen: List[str] = []
    for item in routes():
        cat = item.get("category") or "其他"
        if cat not in seen:
            seen.append(cat)
    return seen


def themes() -> List[str]:
    seen: List[str] = []
    for item in routes():
        for tag in item.get("themes") or []:
            if tag not in seen:
                seen.append(tag)
    return seen


def sort_routes(items: Iterable[Dict], sort: str = "value") -> List[Dict]:
    """排序：value=性价比、clever=巧思、cheap=省钱、comfort=舒适、hours=耗时短。"""
    out = list(items)
    if sort == "cheap":
        return sorted(out, key=lambda r: (r.get("cost_low") or 0))
    if sort == "comfort":
        return sorted(out, key=lambda r: -(r.get("comfort_score") or 0))
    if sort == "hours":
        return sorted(out, key=lambda r: (r.get("total_hours") or 0))
    if sort == "clever":
        return sorted(out, key=lambda r: (-(r.get("clever_score") or 0),
                                          -(r.get("value_score") or 0)))
    return sorted(out, key=lambda r: -(r.get("value_score") or 0))


def route_kind(item: Dict) -> str:
    """单向 / 环线：起终点同城即环线；素材显式给了 ``kind`` 时以它为准。

    为什么要单独判：环线和单向在规划里的用法完全不同 —— 环线回到起点（可自驾/成环），
    单向是点到点（要接后续行程）。素材里也确有「同城来回但不闭环」的例子，
    所以允许显式覆盖。
    """
    explicit = str(item.get("kind") or "").strip()
    if explicit in ("loop", "oneway"):
        return explicit
    frm, to = _clean_city(item.get("from_city")), _clean_city(item.get("to_city"))
    return "loop" if frm and frm == to else "oneway"


KIND_LABEL = {"loop": "环线", "oneway": "单向"}


def query(*, city: str = "", category: str = "", theme: str = "",
          tier: str = "", clever: str = "", kind: str = "",
          sort: str = "value") -> List[Dict]:
    """按城市/分类/主题/档位/单向环线筛选。

    ``clever="巧思"`` 只留巧思分 ≥60 的；``kind`` 取 ``oneway``/``loop``。
    """
    items = routes()
    if city:
        items = [r for r in items if city in (r.get("from_city"), r.get("to_city"))
                 or city in (r.get("name") or "")]
    if category:
        items = [r for r in items if r.get("category") == category]
    if theme:
        items = [r for r in items if theme in (r.get("themes") or [])]
    if tier:
        items = [r for r in items if r.get("tier") == tier]
    if clever:
        items = [r for r in items if (r.get("clever_score") or 0) >= 60]
    if kind in ("oneway", "loop"):
        items = [r for r in items if r.get("kind") == kind]
    return sort_routes(items, sort)


def stats() -> Dict:
    items = routes()
    clever = [r for r in items if (r.get("clever_score") or 0) >= 60]
    loops = [r for r in items if r.get("kind") == "loop"]
    return {
        "routes": len(items),
        "categories": categories(),
        "themes": themes(),
        "updated": updated(),
        "avg_value": int(sum(r.get("value_score") or 0 for r in items) / len(items)) if items else 0,
        "avg_clever": int(sum(r.get("clever_score") or 0 for r in items) / len(items)) if items else 0,
        "clever_routes": len(clever),
        "clever_tags": sorted({t for r in items for t in (r.get("clever_tags") or [])}),
        "loops": len(loops),
        "oneway": len(items) - len(loops),
    }


def for_cities(cities: Sequence[str], limit: int = 4) -> List[Dict]:
    """给一组城市（比如刚规划过的行程）找相关路线：命中越多排越前。"""
    wanted = {c for c in cities if c}
    if not wanted:
        return []
    scored = []
    for item in routes():
        hits = len(wanted & {item.get("from_city"), item.get("to_city")})
        if hits:
            scored.append((hits, -(item.get("value_score") or 0), item))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [item for _, _, item in scored[:limit]]


def card(item: Dict) -> Dict:
    """给页面用的精简卡片（不暴露内部字段）。"""
    return {
        "id": item.get("id"), "name": item.get("name"),
        "from_city": item.get("from_city"), "to_city": item.get("to_city"),
        "kind": item.get("kind") or "oneway",
        "kind_label": item.get("kind_label") or "单向",
        "category": item.get("category"), "tier": item.get("tier"),
        "themes": item.get("themes") or [], "summary": item.get("summary") or "",
        "modes": item.get("modes") or [],
        "total_hours": item.get("total_hours"),
        "cost_low": item.get("cost_low"), "cost_high": item.get("cost_high"),
        "cost_baseline": item.get("cost_baseline"),
        "extra_value": item.get("extra_value") or "",
        "value_score": item.get("value_score"), "comfort_score": item.get("comfort_score"),
        "clever_score": item.get("clever_score") or 0,
        "clever_tags": item.get("clever_tags") or [],
        "smart_tips": item.get("smart_tips") or [],
        "best_season": item.get("best_season") or "",
        "segments": item.get("segments") or [],
        "save_tips": item.get("save_tips") or [],
        "comfort_tips": item.get("comfort_tips") or [],
        "fit_for": item.get("fit_for") or [],
        "watch_out": item.get("watch_out") or "",
        "source": item.get("source") or "", "source_url": item.get("source_url") or "",
        "confidence": item.get("confidence") or "low",
    }
