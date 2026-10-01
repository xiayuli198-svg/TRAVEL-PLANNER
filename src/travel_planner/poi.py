"""城内点位：把城市攻略里的「看点」补上坐标（给只读预览用，先不画地图）。

为什么是这个做法
----------------
「城市内页」缺的其实不是更多 POI，而是**我们自己的文本里提到的那些名字**缺坐标。
所以不批量抓整城 POI（一座城某类最多 600 条、25 条/页，又贵又没必要），
而是按名单逐个直查：239 城共约 900 个看点，1 个名字 1 次请求，落库后不再重查。

实测过的几个坑（都写进校验里）
------------------------------
* 高德把「中央大街」标成 ``地名地址信息;道路名``，按类型抓永远抓不到 —— 所以只用
  ``keywords`` 直查，**不加 types 过滤**；
* 景点名会跨市查偏（同省另一个县、甚至别的省），所以要求「返回名包含查询名」
  且落在该省包围盒内、距市中心不离谱，才收；
* 查不到就记 ``miss``（负缓存），不猜坐标 —— 宁缺勿错。

诚实边界
--------
* 坐标来自高德检索结果，**只用于城内预览与示意图**，不做导航；
* 高德数据只在本地自用缓存，不随工具分发（要分发得走 OSM / Wikidata 路线）；
* 每条都带 ``source`` / ``fetched_at``，页面上照原样标出来。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[2]

SCHEMA = """
CREATE TABLE IF NOT EXISTS city_poi (
  city TEXT NOT NULL,
  name TEXT NOT NULL,                    -- 城市目录里的看点名（我们的口径）
  status TEXT NOT NULL DEFAULT 'ok',     -- ok | miss
  poi_name TEXT NOT NULL DEFAULT '',     -- 检索返回的名字
  lon REAL,
  lat REAL,
  kind TEXT NOT NULL DEFAULT '',         -- 高德 type 的末段
  type_full TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  rating TEXT NOT NULL DEFAULT '',
  tel TEXT NOT NULL DEFAULT '',
  distance_km REAL,                      -- 与城市中心的直线距离
  score REAL,                            -- 挑点时打的分（留痕，便于复核）
  why TEXT NOT NULL DEFAULT '',           -- 为什么挑中/没收
  note TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'amap-place',
  fetched_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (city, name)
);
CREATE INDEX IF NOT EXISTS idx_city_poi_city ON city_poi(city);
CREATE INDEX IF NOT EXISTS idx_city_poi_status ON city_poi(status);
CREATE TABLE IF NOT EXISTS city_poi_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
_FINAL = (STATE_DONE, STATE_FAILED, STATE_CANCELLED)

#: 距市中心的最大容忍距离（公里）。地级市很大：酒泉代管敦煌（约 380 公里）、
#: 阿坝州到黄龙也几百公里，所以放得宽；真正的防线是下面的省份包围盒。
MAX_DISTANCE_KM = 600.0

#: 归一化时要抹平的尾巴。注意只抹「纯后缀」——
#: 「博物馆/公园/寺/塔」这些**是名字的一部分**，抹掉会把「上海博物馆」变成「上海」，
#: 于是任何带「上海」的点都能蹭上（试跑时真发生过）。
TAILS = ("历史文化街区", "历史街区", "遗址公园", "地质公园", "景区", "风景区", "景点",
         "旅游区", "街区", "古镇", "古城")

#: 高德 type 里出现这些词，说明「不是那个地方本身」——商业/服务类场所。
#: 实测：鼓浪屿 → 鼓浪屿船票官方（生活服务;售票处）、天涯海角 → 天涯海角(天鹿雅苑店)
#: （购物相关场所）、蜈支洲岛 → 蜈支洲岛(项目收银处)（游戏厅）。
BAD_TYPE_WORDS = ("购物", "餐饮", "住宿", "生活服务", "商务住宅", "写字楼", "公司企业",
                  "金融保险", "汽车服务", "摩托车", "医疗保健", "银行", "保险", "加油站",
                  "停车场", "售票", "收费站", "地铁站", "公交站", "交通设施", "游戏",
                  "娱乐场所", "洗浴", "酒吧", "网吧", "旅行社", "政府机构", "农林牧渔",
                  "工厂", "仓储", "物流")
#: 「地名地址/道路名」既可能是步行街（中央大街），也可能是某条路（颐和园路），
#: 所以单独归为中性：只有名字完全一致时才认。
NEUTRAL_TYPE_WORDS = ("地名地址", "道路名")
GOOD_TYPE_WORDS = ("风景名胜", "旅游景点", "公园", "博物馆", "纪念馆", "寺庙", "道观",
                   "文化", "世界遗产", "古迹", "景区", "广场", "教堂", "海滩", "植物园",
                   "动物园", "水族馆", "观景", "休闲")
#: 名字里带这些词的，是「同一地方的配套点」（打卡点/售票处/收银台/××店…）。
#: 注意「中央大街」这种步行街在高德那里类型是「道路名」，所以**不能**因为类型就否掉
#: 一个名字完全一致且没有配套词的点。
BAD_NAME_WORDS = ("地铁站", "售票", "票务", "停车场", "直通车", "打卡点", "入口", "出口",
                  "中心", "分公司", "分店", "办事处", "营业厅", "充电站", "公交站",
                  "店", "收银", "接待", "寄存", "检票", "咨询", "服务点", "门市",
                  "超市", "商城", "商铺", "专营", "专卖", "旗舰", "连锁", "候车",
                  "游客中心", "售票处", "服务区", "管理", "办公", "宿舍", "仓库")
#: 收与不收的分界线（宁可记未命中，也不收一个错坐标）
ACCEPT_SCORE = 40.0

#: 州/盟/地区这类名字高德不认，得换成驻地县市名才查得到。
#: 实测：`city=台湾` 查「日月潭」返回空或内地同名点（北京门头沟的日月潭，距市中心 1743 公里），
#: 而 `city=南投县` 第一条就是日月潭；海西州要写德令哈市、伊犁要写伊宁市。
CITY_SEATS = {
    "伊犁": "伊宁市", "海西州": "德令哈市", "海北州": "门源县", "海南州": "共和县",
    "玉树州": "玉树市", "果洛州": "玛沁县", "黄南州": "同仁市",
    "黔南": "都匀市", "黔东南": "凯里市", "黔西南": "兴义市",
    "凉山": "西昌市", "甘孜": "康定市", "阿坝": "马尔康市",
    "阿拉善盟": "阿拉善左旗", "巴音郭楞": "库尔勒市", "大兴安岭": "加格达奇区",
    "西双版纳": "景洪市", "呼伦贝尔": "海拉尔区", "湘西": "吉首市",
    "迪庆": "香格里拉市", "恩施": "恩施市", "博尔塔拉": "博乐市",
    "锡林郭勒盟": "锡林浩特市", "兴安盟": "乌兰浩特市", "通辽": "科尔沁区",
    # 港澳台：用县市级全称才查得到本地点位
    "台湾": "台北市", "香港": "香港", "澳门": "澳门",
}


def split_query(text: str) -> tuple:
    """支持「关键词 @城市」写法（校对结果里就这么给的）。

    港澳台与自治州必须给县市级城市名，所以关键词后面可以带一个 ``@城市`` 覆盖。
    """
    raw = str(text or "")
    if "@" in raw:
        head, _, tail = raw.partition("@")
        return head.strip(), tail.strip()
    return raw.strip(), ""


def city_candidates(city: str) -> List[str]:
    """这座城市该用哪些 ``city=`` 参数试（原名 → 驻地县市名）。"""
    out = [str(city or "").strip()]
    seat = CITY_SEATS.get(out[0])
    if seat and seat not in out:
        out.append(seat)
    return [c for c in out if c]


def default_path() -> Path:
    value = os.environ.get("TRAVEL_PLANNER_POI_DB")
    return Path(value) if value else ROOT / "data" / "poi.db"


def connect(path: Optional[str | Path] = None) -> sqlite3.Connection:
    p = Path(path) if path else default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """老库补列（打分留痕是后加的）。"""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(city_poi)")}
    for name, decl in (("score", "REAL"), ("why", "TEXT NOT NULL DEFAULT ''")):
        if name not in existing:
            conn.execute(f"ALTER TABLE city_poi ADD COLUMN {name} {decl}")


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 名单
def targets() -> List[Dict]:
    """要补坐标的名单：城市目录里每个「看点」（含所属城市与中心点）。"""
    from . import catalog

    out: List[Dict] = []
    for city in catalog.cities():
        if city.get("lat") is None or city.get("lon") is None:
            continue
        for item in city.get("highlights") or []:
            name = str((item or {}).get("name") or "").strip()
            if not name:
                continue
            out.append({"city": city["name"], "name": name,
                        "kind": str((item or {}).get("kind") or ""),
                        "lat": float(city["lat"]), "lon": float(city["lon"]),
                        "province": city.get("province") or ""})
    return out


def norm_name(text: str) -> str:
    """名字归一：去括号注释、去间隔号、抹平「景区/街区」这类纯后缀。

    尾巴必须**从长到短**匹配：「黄龙风景区」先撞上短尾巴「景区」会变成「黄龙风」，
    于是跟高德的正式名「黄龙国家级风景名胜区」对不上（试跑时因此漏了一批）。
    """
    value = re.sub(r"[（(].*?[)）]", "", str(text or "")).strip()
    value = value.replace("·", "").replace(" ", "").replace("　", "")
    for tail in sorted(TAILS, key=len, reverse=True):
        if value.endswith(tail) and len(value) > len(tail) + 1:
            value = value[: -len(tail)]
            break
    return value


#: 名字里这些描述性尾巴可以去掉再查一次（「平江路历史街区」→「平江路」、
#: 「北戴河海滨」→「北戴河」、「额济纳胡杨林」→…）。只对第一次查不到的名字用。
VARIANT_TAILS = ("历史街区", "历史文化街区", "老街", "古街", "老城", "海滨", "海水浴场",
                 "草原", "夜景", "大本营", "世界地质公园", "国家地质公园", "国家森林公园",
                 "森林公园", "国家湿地公园", "湿地公园", "自然保护区", "旅游度假区",
                 "度假区", "滑雪场", "主题乐园", "森林游乐区", "国家公园", "海岸线",
                 "滩涂", "花海", "古村落", "油菜花海", "梯田", "土楼", "石窟", "古城墙")
#: 名字里的「与…」后半段（「天星小轮与维港」→「天星小轮」）
_AND_TAILS = ("与", "和", "及")


def query_variants(name: str) -> List[str]:
    """同一个地方用几种叫法试：全名 → 去掉「与…」后半段 → 去掉描述性尾巴 → 去掉「市」。

    实测这些名字不是高德里的正式 POI 名（「平江路历史街区」「天星小轮与维港」
    「敦煌市博物馆」），照着我们的写法查就是查不到，换个说法就有了。
    """
    out = [name]
    for sep in _AND_TAILS:
        if sep in name:
            head = name.split(sep)[0]
            if len(head) >= 2:
                out.append(head)
            break
    for tail in VARIANT_TAILS:
        if name.endswith(tail) and len(name) > len(tail) + 1:
            out.append(name[: -len(tail)])
            break
    for token, repl in (("市博物馆", "博物馆"), ("市博物院", "博物院"),
                        ("州博物馆", "博物馆"), ("地区博物馆", "博物馆"),
                        ("市立", ""), ("国家", "")):
        if token in name:
            candidate = name.replace(token, repl)
            if len(candidate) >= 2:
                out.append(candidate)
            break
    seen, uniq = set(), []
    for item in out:
        if item and item not in seen:
            seen.add(item)
            uniq.append(item)
    return uniq[:3]


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math

    lon1, lat1 = a
    lon2, lat2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def in_province(lat: float, lon: float) -> str:
    """落在哪个省框里（与 fill_route_coords.py 同一套粗筛，防止跨省查偏）。"""
    try:
        import importlib.util

        path = ROOT / "scripts" / "fill_route_coords.py"
        spec = importlib.util.spec_from_file_location("fill_route_coords", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)                      # type: ignore[union-attr]
        return mod.in_any_province(lat, lon)
    except Exception:                                     # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 挑点
def judge(name: str, target: Dict, poi: Dict) -> Dict:
    """给一个检索结果打分（纯函数，好测）。分数不够就当作没查到。

    打分顺序反映了踩过的坑：**名字**先分档（完全一致 / 包含），
    再看名字里有没有「配套点」的字眼（地铁站/售票处/打卡点），
    然后才用类型、评分、距离做调整 —— 因为「外滩中心」「豫园(地铁站)」「颐和园路」
    的名字都"对得上"，只看名字会挑错，只看类型又会把「中央大街」（高德标成道路名）
    这种真地方误杀。
    """
    poi_name = str(poi.get("name") or "")
    lon, lat = poi.get("lon"), poi.get("lat")
    if not poi_name or lon is None or lat is None:
        return {"ok": False, "reason": "结果缺名字或坐标"}
    want, got = norm_name(name), norm_name(poi_name)
    if not want or not got:
        return {"ok": False, "reason": "名字归一后为空"}
    if want == got:
        score, why = 100.0, "名字完全一致"
    elif want in got or got in want:
        score, why = 60.0, "名字包含"
        # 名字长一点通常是**更完整**（「三清山」→「三清山国家级旅游风景区」），
        # 所以只轻罚；真正的「配套点」由上面的配套词与下面的路名规则挡。
        score -= min(10.0, 1.0 * abs(len(got) - len(want)))
    else:
        return {"ok": False, "reason": f"名字对不上（返回的是「{poi_name}」）"}

    # 名字里多出来的部分若带「配套」字眼，直接否掉（豫园(地铁站)、外滩中心…）
    leftover = poi_name.replace(name, "")
    hit_word = next((w for w in BAD_NAME_WORDS if w in leftover), "")
    if hit_word:
        return {"ok": False, "score": 0.0,
                "reason": f"返回的是配套点「{poi_name}」（带「{hit_word}」），不是那个地方本身"}

    type_full = str(poi.get("type_full") or "")
    if any(word in type_full for word in BAD_TYPE_WORDS):
        # 商业/服务类场所：名字一致也不收（天涯海角(天鹿雅苑店) 就是这么混进来的）
        score -= 20.0 if want == got else 70.0
        why += " · 类型是商业/服务场所"
    elif any(word in type_full for word in NEUTRAL_TYPE_WORDS):
        # 地名地址/道路名：只有名字完全一致才认（中央大街 ✓、颐和园路 ✗）
        if want != got:
            return {"ok": False, "score": 0.0,
                    "reason": f"最接近的是「{poi_name}」（{why} · 类型只是路名/地址），不是那个地方"}
        if "自然地名" in type_full:
            # 纯地理名录条目（山/湖/河）点位常常是随手标的，还经常落在城里；
            # 同页若有真景区就让它赢（实测：三清山/梵净山都栽在这上面）
            score -= 25.0
            why += " · 是地名录不是景区"
    elif any(word in type_full for word in GOOD_TYPE_WORDS):
        score += 30.0
        why += " · 类型像景点"

    distance = _haversine_km((float(lon), float(lat)),
                             (float(target["lon"]), float(target["lat"])))
    if distance > MAX_DISTANCE_KM:
        return {"ok": False, "reason": f"距市中心 {distance:.0f} 公里，判定查偏"}
    province = in_province(float(lat), float(lon))
    if not province:
        return {"ok": False, "reason": "落在任何省份包围盒之外，判定查偏"}
    # 距离只做微调且封顶：地级市动辄几百公里（阿坝州中心到黄龙约 190 公里），
    # 罚太狠会把真地方误杀 —— 防跨省查偏靠的是省份包围盒。
    score -= min(50.0, distance) * 0.3
    try:
        stars = float(poi.get("rating") or 0)
    except (TypeError, ValueError):
        stars = 0.0
    if stars:
        score += stars * 2
        why += f" · ★{stars:g}"
    if score < ACCEPT_SCORE:
        return {"ok": False, "score": round(score, 1),
                "reason": f"最接近的是「{poi_name}」（{why}），但不像那个地方本身"}
    return {"ok": True, "score": round(score, 1), "why": why,
            "distance_km": round(distance, 1), "province": province}


def pick(name: str, target: Dict, pois: Sequence[Dict]) -> Optional[Dict]:
    """从一页结果里挑分数最高的那个；一个都不够格就返回 None（记未命中）。"""
    scored: List[tuple] = []
    rejected: List[tuple] = []
    for poi in pois:
        verdict = judge(name, target, poi)
        if verdict["ok"]:
            scored.append((verdict["score"], poi, verdict))
        elif verdict.get("score") is not None:
            rejected.append((verdict["score"], poi, verdict))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    score, poi, verdict = scored[0]
    distance = verdict["distance_km"]
    return {**poi, "distance_km": distance, "score": score, "why": verdict["why"],
            "note": (f"距市中心 {distance:.0f} 公里（远郊/代管县市）"
                     if distance > 60 else "")}


def near_miss(target: Dict, pois: Sequence[Dict]) -> str:
    """一个都没收时，把最接近的那个写进备注，页面上能看出「差在哪」。"""
    best = None
    for poi in pois:
        verdict = judge(target["name"], target, poi)
        if verdict.get("score") is not None or not verdict["ok"]:
            key = verdict.get("score") or 0
            if best is None or key > best[0]:
                best = (key, verdict["reason"])
    if best is None:
        return "没有返回结果" if not pois else "返回的名字都对不上"
    return best[1]


# ---------------------------------------------------------------- 抓取
AMAP_PLACE = "https://restapi.amap.com/v3/place/text"


def amap_key(conn: Optional[sqlite3.Connection] = None) -> str:
    from . import db
    from .ingest import amap

    own = conn is None
    conn = conn or db.connect()
    try:
        return amap.get_key(conn) or ""
    finally:
        if own:
            conn.close()


def fetch_amap(city: str, name: str, *, key: str, timeout: int = 20,
               retries: int = 3) -> List[Dict]:
    """高德关键词检索（不加 types：步行街/道路名也要能查出来）。

    ``city=""`` 时不限定城市 —— 用来兜底：有些目录名高德不认（伊犁州、阿坝州这类
    州/盟名），限定城市会直接返回空，这时只能全国查一遍，再靠省份包围盒 + 距离校验兜住。

    高德对个人 key 有 QPS 上限（实测跑快了会返回 ``CUQPS_HAS_EXCEEDED_THE_LIMIT``），
    这里退避重试，不把「被限流」当成「查不到」。
    """
    if not key:
        raise RuntimeError("没有高德 key")
    params = {"key": key, "keywords": name, "offset": 25, "page": 1,
              "extensions": "all", "output": "json"}
    if city:
        params["city"] = city
        params["citylimit"] = "true"
    url = f"{AMAP_PLACE}?{urllib.parse.urlencode(params)}"
    last = ""
    for attempt in range(max(1, retries)):
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8", "replace"))
        info = str(raw.get("info") or "")
        if raw.get("status") == "1":
            break
        last = info or str(raw.get("status"))
        if "QPS" in info.upper() or "LIMIT" in info.upper():
            time.sleep(1.0 + attempt)                     # 被限流：等一等再试
            continue
        raise RuntimeError(f"高德返回 {last}")
    else:
        raise RuntimeError(f"高德一直返回 {last}（多半是 QPS 限流）")
    out: List[Dict] = []
    for item in raw.get("pois") or []:
        loc = (item.get("location") or "").split(",")
        if len(loc) != 2:
            continue
        try:
            lon, lat = float(loc[0]), float(loc[1])
        except ValueError:
            continue
        type_full = str(item.get("type") or "")
        out.append({
            "name": item.get("name") or "", "lon": lon, "lat": lat,
            "kind": (type_full.split(";")[-1] if type_full else ""),
            "type_full": type_full,
            "address": item.get("address") or "",
            "rating": str(((item.get("biz_ext") or {}) or {}).get("rating") or ""),
            "tel": item.get("tel") or "",
        })
    return out


# ---------------------------------------------------------------- 落库
def revalidate(conn: sqlite3.Connection, *, db_path: Optional[str | Path] = None) -> Dict:
    """用**当前**规则重审已落库的命中行（不联网）。

    换了挑点规则之后（比如补上「配套点」判定），旧的命中可能已经不达标了 ——
    与其把 900 个名字全部重查一遍，不如先用存下来的 poi_name/type/坐标本地复判，
    不达标的降级为未命中，再用 ``only_miss`` 只补这一批。
    """
    targets = {(t["city"], t["name"]): t for t in targets_all()}
    demoted: List[Dict] = []
    rows = conn.execute(
        "SELECT city,name,poi_name,lon,lat,type_full,rating,distance_km,source FROM city_poi "
        "WHERE status='ok'").fetchall()
    skipped_reviewed = 0
    for row in rows:
        target = targets.get((row["city"], row["name"]))
        if not target:
            continue
        # 人工/子代理**校对过**的条目不再用自动挑点规则二审：
        # 它们的正式名常写成「国立故宫博物院（台北故宫博物院）」这种带旧名的形式，
        # 拿自动规则去比会判成「名字对不上」，把已经核对好的点又打回未命中。
        if "校对" in str(row["source"] or ""):
            skipped_reviewed += 1
            continue
        cand = {"name": row["poi_name"], "lon": row["lon"], "lat": row["lat"],
                "type_full": row["type_full"], "rating": row["rating"]}
        verdict = judge(row["name"], target, cand)
        if verdict["ok"]:
            continue
        demoted.append({"city": row["city"], "name": row["name"],
                        "poi_name": row["poi_name"], "reason": verdict["reason"]})
        with conn:
            conn.execute(
                "UPDATE city_poi SET status='miss', score=?, why=?, note='' WHERE city=? AND name=?",
                (verdict.get("score"), verdict["reason"], row["city"], row["name"]))
    return {"ok": True, "checked": len(rows), "demoted": len(demoted),
            "skipped_reviewed": skipped_reviewed, "samples": demoted[:20]}


def targets_all() -> List[Dict]:
    return targets()


def apply_fixes(conn: sqlite3.Connection, raw) -> Dict:
    """把「看点名称校对」的结果并进库里（``data/route_content/_staging/poi_fixes.json``）。

    校对给出的坐标是**用正式名重查高德**得到的（港澳台与自治州还要把城市名换成县市级全称），
    所以照抄进库时要把 ``source`` / ``confidence`` 一起留下来 —— 页面上要能看出这个点
    「是怎么来的」，而不是假装它跟自动查的一样。
    """
    data = raw if isinstance(raw, dict) else json.loads(
        Path(str(raw)).read_text(encoding="utf-8"))
    fixes = [f for f in (data.get("fixes") or []) if isinstance(f, dict)]
    applied, no_coord, skipped = [], [], []
    for item in fixes:
        city, name = str(item.get("city") or ""), str(item.get("name") or "")
        if not city or not name:
            skipped.append({"city": city, "name": name, "why": "缺 city/name"})
            continue
        target = {"city": city, "name": name, "lat": 0.0, "lon": 0.0}
        lat, lon = item.get("lat"), item.get("lon")
        confidence = str(item.get("confidence") or "")
        if lat is None or lon is None:
            # 没有坐标的（如季节性花海）：保留未命中，但把「建议关键词」记下来
            query = str(item.get("query") or "")
            store(conn, target, None,
                  reason="校对后仍无坐标" + (f"；建议用关键词「{query}」重查" if query else ""))
            no_coord.append({"city": city, "name": name, "query": query,
                             "official_name": item.get("official_name") or ""})
            continue
        official = str(item.get("official_name") or name)
        source_text = str(item.get("source") or "")
        result = {
            "name": official, "lon": float(lon), "lat": float(lat),
            "kind": str(item.get("county") or "校对点位"), "type_full": "",
            "address": str(item.get("county") or ""), "rating": "", "tel": "",
            "distance_km": None, "score": None,
            "why": f"名称校对（{confidence or '未标'}）",
            "note": (("坐标来自公开名核对：" + source_text[:160]) if source_text
                     else f"名称校对（{confidence or '未标'}）"),
        }
        store(conn, target, result, source=f"校对+高德重查（{confidence or '未标'}）")
        applied.append({"city": city, "name": name, "official_name": official,
                        "lat": float(lat), "lon": float(lon), "confidence": confidence})
    return {"ok": True, "total": len(fixes), "applied": len(applied),
            "no_coord": len(no_coord), "skipped": len(skipped),
            "no_coord_items": no_coord[:20], "skipped_items": skipped[:10]}


def _try_one(fetch, target: Dict, variant: str, city: str = "") -> Dict:
    """用某个「关键词 + 城市」查一次：先限定城市，没结果再全国查一遍。

    返回 ``{best, pois, used_city, used_query, error}``。限定城市失败时换成全国检索 ——
    州/盟名（伊犁、阿坝、海西州）高德不认，限定城市会直接返回空。
    """
    keyword, override = split_query(variant)
    city = override or city or target["city"]
    try:
        pois = fetch(city, keyword)
    except Exception as exc:                              # noqa: BLE001
        return {"best": None, "pois": [], "used_city": city, "used_query": keyword,
                "error": f"{type(exc).__name__}: {exc}"}
    used_city = city
    if not pois and city:
        try:
            pois = fetch("", keyword)
            used_city = ""
        except Exception as exc:                          # noqa: BLE001
            return {"best": None, "pois": [], "used_city": city, "used_query": keyword,
                    "error": f"{type(exc).__name__}: {exc}"}
    best = pick(target["name"], target, pois)
    if best is None and used_city:
        # 限定城市时一个能收的都没有（同名点太多 / 城市名不认），换个口径再试
        try:
            alt = fetch("", keyword)
        except Exception:                                 # noqa: BLE001
            alt = []
        alt_best = pick(target["name"], target, alt) if alt else None
        if alt_best:
            best, pois, used_city = alt_best, alt, ""
    return {"best": best, "pois": pois, "used_city": used_city,
            "used_query": keyword, "error": ""}


def store(conn: sqlite3.Connection, target: Dict, result: Optional[Dict],
          *, reason: str = "", source: str = "amap-place") -> Dict:
    """写一条（含未命中的负缓存）。"""
    city, name = target["city"], target["name"]
    row = {
        "city": city, "name": name,
        "status": "ok" if result else "miss",
        "poi_name": (result or {}).get("name") or "",
        "lon": (result or {}).get("lon"), "lat": (result or {}).get("lat"),
        "kind": (result or {}).get("kind") or "",
        "type_full": (result or {}).get("type_full") or "",
        "address": (result or {}).get("address") or "",
        "rating": (result or {}).get("rating") or "",
        "tel": (result or {}).get("tel") or "",
        "distance_km": (result or {}).get("distance_km"),
        "score": (result or {}).get("score"),
        "why": (result or {}).get("why") or (reason if not result else ""),
        "note": (result or {}).get("note") or "",
        "source": source, "fetched_at": _now(),
    }
    with conn:
        conn.execute(
            "INSERT INTO city_poi(city,name,status,poi_name,lon,lat,kind,type_full,address,"
            "rating,tel,distance_km,score,why,note,source,fetched_at) "
            "VALUES(:city,:name,:status,:poi_name,:lon,:lat,:kind,:type_full,:address,"
            ":rating,:tel,:distance_km,:score,:why,:note,:source,:fetched_at) "
            "ON CONFLICT(city,name) DO UPDATE SET status=excluded.status, "
            "poi_name=excluded.poi_name, lon=excluded.lon, lat=excluded.lat, kind=excluded.kind, "
            "type_full=excluded.type_full, address=excluded.address, rating=excluded.rating, "
            "tel=excluded.tel, distance_km=excluded.distance_km, score=excluded.score, "
            "why=excluded.why, note=excluded.note, source=excluded.source, "
            "fetched_at=excluded.fetched_at", row)
    return row


def done_names(conn: sqlite3.Connection, *, refresh: bool = False) -> set:
    if refresh:
        return set()
    return {(r["city"], r["name"]) for r in
            conn.execute("SELECT city,name FROM city_poi").fetchall()}


def harvest(*, limit: int = 0, delay: float = 0.3, refresh: bool = False,
            only_miss: bool = False, only_names: Optional[Sequence] = None,
            db_path: Optional[str | Path] = None, key: str = "",
            fetch: Optional[Callable[[str, str], List[Dict]]] = None,
            progress_cb: Optional[Callable[[str, int, int, Dict], None]] = None,
            cancel_cb: Optional[Callable[[], bool]] = None) -> Dict:
    """按名单直查并落库。

    ``fetch(city, name) -> [poi, ...]`` 可注入：测试用假数据跑，不联网。
    ``only_miss=True`` 只重查上次判为未命中的（换过规则之后补一遍用）。
    ``only_names`` 只重查指定的 ``[(城市, 看点)]``（核查后定点重查用）。
    """
    conn = connect(db_path)
    try:
        if fetch is None:
            key = key or amap_key()
            if not key:
                return {"ok": False, "error": "没有高德 key（数据台可配 set-amap-key）",
                        "queried": 0}
            fetch = lambda city, name: fetch_amap(city, name, key=key)   # noqa: E731
        if only_names is not None:
            wanted = {(str(c), str(n)) for c, n in only_names}
            todo = [t for t in targets() if (t["city"], t["name"]) in wanted]
        elif only_miss:
            miss_keys = {(r["city"], r["name"]) for r in conn.execute(
                "SELECT city,name FROM city_poi WHERE status='miss'").fetchall()}
            todo = [t for t in targets() if (t["city"], t["name"]) in miss_keys]
        else:
            skip = done_names(conn, refresh=refresh)
            todo = [t for t in targets() if (t["city"], t["name"]) not in skip]
        if limit:
            todo = todo[: int(limit)]
        total, ok, miss, failed = len(todo), 0, 0, 0
        misses: List[Dict] = []
        failures: List[Dict] = []
        for index, target in enumerate(todo, 1):
            if cancel_cb is not None and cancel_cb():
                break
            best, pois = None, []
            used_query, used_city, error = target["name"], target["city"], ""
            for variant in query_variants(target["name"]):
                for city in city_candidates(target["city"]):
                    attempt = _try_one(fetch, target, variant, city)
                    error = attempt["error"] or error
                    if attempt["best"]:
                        best, pois = attempt["best"], attempt["pois"]
                        used_query, used_city = attempt["used_query"], attempt["used_city"]
                        break
                    if attempt["pois"]:
                        pois = attempt["pois"]                # 留一批用于「差在哪」的说明
                if best:
                    break
            if best is None and error:
                failed += 1
                failures.append({"city": target["city"], "name": target["name"],
                                 "error": error})
                store(conn, target, None, reason=f"查询失败：{error.split(':')[0]}")
                if progress_cb:
                    progress_cb("查询失败", index, total, {"failed": failed})
                time.sleep(max(0.0, delay))
                continue
            if best:
                if used_query != target["name"]:
                    best["why"] += f" · 按「{used_query}」检索命中"
                if not used_city:
                    best["why"] += " · 未按城市限定（靠省份+距离校验）"
                    best["note"] = (best.get("note") or "") or "全国检索命中，已过省份与距离校验"
            store(conn, target, best,
                  reason=("未命中" if pois else "没有返回结果") if best
                  else near_miss(target, pois))
            if best:
                ok += 1
            else:
                miss += 1
                if len(misses) < 40:
                    misses.append({"city": target["city"], "name": target["name"],
                                   "returned": (pois[0]["name"] if pois else "")})
            if progress_cb:
                progress_cb("补坐标", index, total, {"ok": ok, "miss": miss, "failed": failed})
            time.sleep(max(0.0, delay))
        summary = {"ok": True, "queried": total - (0 if cancel_cb is None else 0),
                   "hit": ok, "miss": miss, "failed": failed, "total": total,
                   "cancelled": bool(cancel_cb is not None and cancel_cb()),
                   "misses": misses, "failures": failures[:20]}
        with conn:
            conn.execute(
                "INSERT INTO city_poi_meta(key,value) VALUES('last_harvest',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps({k: v for k, v in summary.items() if k not in ("misses", "failures")},
                            ensure_ascii=False),))
        return summary
    finally:
        conn.close()


# ---------------------------------------------------------------- 读取
def list_pois(conn: sqlite3.Connection, *, city: str = "", status: str = "",
              limit: int = 0, skip: int = 0) -> Dict:
    where, params = [], []
    if city:
        where.append("city = ?")
        params.append(city)
    if status:
        where.append("status = ?")
        params.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = int(conn.execute(f"SELECT COUNT(*) n FROM city_poi {clause}",
                             params).fetchone()["n"])
    sql = f"SELECT * FROM city_poi {clause} ORDER BY city, status, name"
    if limit:
        sql += " LIMIT ? OFFSET ?"
        params = params + [int(limit), max(0, int(skip))]
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for row in rows:
        row["has_coord"] = row["lon"] is not None and row["lat"] is not None
    return {"ok": True, "total": total, "skip": max(0, int(skip)),
            "limit": int(limit or 0), "pois": rows}


def coverage(conn: sqlite3.Connection) -> Dict:
    """名单覆盖率：目标多少、命中多少、未命中多少、还没查多少。"""
    items = targets()
    by_key = {(r["city"], r["name"]): r for r in
              conn.execute("SELECT city,name,status FROM city_poi").fetchall()}
    hit = miss = 0
    cities_hit: Dict[str, int] = {}
    for target in items:
        row = by_key.get((target["city"], target["name"]))
        if not row:
            continue
        if row["status"] == "ok":
            hit += 1
            cities_hit[target["city"]] = cities_hit.get(target["city"], 0) + 1
        else:
            miss += 1
    total = len(items)
    cities = {t["city"] for t in items}
    return {
        "targets": total, "hit": hit, "miss": miss,
        "pending": total - hit - miss,
        "rate": round(hit / total * 100, 1) if total else 0.0,
        "cities": len(cities),
        "cities_with_hit": len(cities_hit),
        "full_cities": len([c for c in cities
                            if cities_hit.get(c, 0) == sum(1 for t in items if t["city"] == c)]),
    }


def stats(conn: sqlite3.Connection) -> Dict:
    built = conn.execute("SELECT value FROM city_poi_meta WHERE key='last_harvest'").fetchone()
    try:
        last = json.loads(built["value"]) if built else {}
    except (TypeError, ValueError):
        last = {}
    out = coverage(conn)
    out["last_harvest"] = last
    return out


def export_json(conn: sqlite3.Connection) -> str:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM city_poi ORDER BY city, name").fetchall()]
    return json.dumps({"exported": _now(),
                       "note": "城市看点坐标（高德检索结果，本地自用，仅供预览与示意图）",
                       "pois": rows}, ensure_ascii=False, indent=1)


def miss_breakdown(conn: sqlite3.Connection) -> Dict:
    """未命中的分布：港台一条线、其他一条线 —— 报告里要说清「差在哪」。"""
    from . import catalog

    provinces = {c["name"]: (c.get("province") or "") for c in catalog.cities()}
    outside = {"台湾", "香港", "澳门"}
    stats = {"overseas_region": 0, "mainland": 0, "by_province": {}}
    for row in conn.execute("SELECT city,name,why FROM city_poi WHERE status='miss'"):
        province = provinces.get(row["city"], "")
        if province in outside:
            stats["overseas_region"] += 1
        else:
            stats["mainland"] += 1
            stats["by_province"][province or "未知"] = \
                stats["by_province"].get(province or "未知", 0) + 1
    stats["by_province"] = dict(sorted(stats["by_province"].items(),
                                       key=lambda kv: -kv[1])[:12])
    return stats


# ---------------------------------------------------------------- 后台任务
class PoiHarvestJob:
    """后台补坐标：一条一条查，进度可见、可取消、断了能接着跑（已查过的会跳过）。"""

    def __init__(self, *, limit: int = 0, delay: float = 0.3, refresh: bool = False,
                 only_miss: bool = False,
                 db_path: Optional[str | Path] = None, key: str = ""):
        self.limit = limit
        self.delay = delay
        self.refresh = refresh
        self.only_miss = only_miss
        self.db_path = db_path
        self.key = key
        self.state = STATE_RUNNING
        self.phase = "准备"
        self.done = 0
        self.total = 0
        self.hit = 0
        self.miss = 0
        self.failed = 0
        self.error = ""
        self.started = dt.datetime.now()
        self.finished: Optional[dt.datetime] = None
        self.logs: List[str] = []
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _log(self, message: str) -> None:
        self.logs.append(f"{dt.datetime.now().strftime('%H:%M:%S')} {message}")
        if len(self.logs) > 400:
            del self.logs[:100]

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def start(self) -> "PoiHarvestJob":
        self._thread = threading.Thread(target=self._run, name="poi-harvest", daemon=True)
        self._thread.start()
        return self

    def _on_progress(self, phase: str, done: int, total: int, extra: Dict) -> None:
        self.phase = phase
        self.done, self.total = done, total
        self.hit = int(extra.get("ok") or self.hit)
        self.miss = int(extra.get("miss") or self.miss)
        self.failed = int(extra.get("failed") or self.failed)
        if done % 25 == 0 or done == total:
            self._log(f"{phase} {done}/{total}：命中 {self.hit} · 未命中 {self.miss} · 失败 {self.failed}")

    def _run(self) -> None:
        final_state = STATE_DONE
        try:
            self._log("开始按名单补城内点位坐标（1 个名字 1 次请求，已查过的会跳过）")
            out = harvest(limit=self.limit, delay=self.delay, refresh=self.refresh,
                          only_miss=self.only_miss, db_path=self.db_path, key=self.key,
                          progress_cb=self._on_progress, cancel_cb=self.cancelled)
            if not out.get("ok"):
                final_state = STATE_FAILED
                self.error = str(out.get("error") or "失败")
                self._log("失败：" + self.error)
            else:
                self.hit, self.miss, self.failed = out["hit"], out["miss"], out["failed"]
                self.total = out["total"]
                self.done = out["total"]
                if out.get("cancelled") or self._cancel.is_set():
                    final_state = STATE_CANCELLED
                    self._log("已取消（已查过的都留在库里，下次接着跑）")
                else:
                    self._log(f"完成：命中 {self.hit} · 未命中 {self.miss} · 失败 {self.failed}")
        except Exception as exc:                          # noqa: BLE001
            final_state = STATE_FAILED
            self.error = f"{type(exc).__name__}: {exc}"
            self._log("失败：" + self.error)
        finally:
            self.state = final_state
            self.finished = dt.datetime.now()

    def cancel(self) -> None:
        if self.state == STATE_RUNNING:
            self._cancel.set()
            self._log("收到取消请求，当前这条查完就停…")

    def snapshot(self, *, include_logs: int = 40) -> Dict:
        end = self.finished or dt.datetime.now()
        elapsed = (end - self.started).total_seconds()
        pct = round(self.done / self.total * 100) if self.total else None
        eta = None
        if self.state == STATE_RUNNING and self.done and self.total:
            eta = round(elapsed / self.done * (self.total - self.done))
        return {
            "state": self.state, "phase": self.phase, "done": self.done, "total": self.total,
            "percent": pct, "hit": self.hit, "miss": self.miss, "failed": self.failed,
            "elapsed": round(elapsed), "eta": eta, "error": self.error,
            "logs": self.logs[-include_logs:],
            "finished": self.finished.isoformat(timespec="seconds") if self.finished else "",
        }


class HarvestRegistry:
    """同一时刻只跑一个补坐标任务（避免重复请求高德）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Optional[PoiHarvestJob] = None

    def start(self, **kwargs) -> Dict:
        with self._lock:
            cur = self._current
            if cur and cur.state == STATE_RUNNING:
                return {"ok": False, "error": "已有一个补坐标任务在跑，先等它结束或取消",
                        "job": cur.snapshot()}
            job = PoiHarvestJob(**kwargs)
            self._current = job
        job.start()
        return {"ok": True, "job": job.snapshot()}

    def current(self) -> Optional[PoiHarvestJob]:
        return self._current

    def status(self) -> Dict:
        cur = self._current
        return {"ok": True, "job": cur.snapshot() if cur else None}

    def cancel(self) -> Dict:
        cur = self._current
        if not cur:
            return {"ok": False, "error": "没有补坐标任务"}
        cur.cancel()
        return {"ok": True, "job": cur.snapshot()}


#: 全局注册表（web.py 与脚本共用）
HARVEST = HarvestRegistry()


def wait_for(job: PoiHarvestJob, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.state in _FINAL:
            return True
        time.sleep(0.05)
    return job.state in _FINAL
