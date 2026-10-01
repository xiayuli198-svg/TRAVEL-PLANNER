"""SQLite 数据模型与常用查询。"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .engine.model import Trip

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_MCT_MIN = 10        # 同站默认最小换乘时间（分钟）
DEFAULT_CITY_MCT_MIN = 90   # 同城异站默认换乘时间（分钟）
AIR_MCT_MIN = 90            # 机场同场换乘（航班中转）默认最小时间
AIR_AIR_CITY_MIN = 120      # 同城异机场换乘默认时间
AIR_RAIL_CITY_MIN = 150     # 机场 ↔ 铁路站同城接驳默认时间（含值机/地面交通）

AIR_PREFIX = "AIR:"         # 引擎中机场节点的前缀，避免与铁路三字码冲突


def air_code(iata: str) -> str:
    return AIR_PREFIX + iata

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
  code     TEXT PRIMARY KEY,   -- 三字码，如 BJP
  name     TEXT NOT NULL,      -- 站名，如 北京南
  pinyin   TEXT,
  abbrev   TEXT,
  city     TEXT,               -- 城市名，如 北京
  telecode TEXT,
  ordinal  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_station_name ON stations(name);
CREATE INDEX IF NOT EXISTS idx_station_city ON stations(city);

CREATE TABLE IF NOT EXISTS schedules (
  train_no     TEXT NOT NULL,  -- 12306 内部车次号，如 240000G1010D
  train_code   TEXT NOT NULL,  -- 对外车次，如 G101
  date         TEXT NOT NULL,  -- 运行日 YYYY-MM-DD
  seq          INTEGER NOT NULL,
  station_code TEXT NOT NULL,
  station_name TEXT NOT NULL,
  day          INTEGER NOT NULL DEFAULT 0, -- 相对查询日的天偏移（可为负）
  arr          TEXT,           -- HH:MM 或 '--'
  dep          TEXT,
  PRIMARY KEY (train_no, date, seq)
);
CREATE INDEX IF NOT EXISTS idx_sched_station ON schedules(date, station_code, dep);
CREATE INDEX IF NOT EXISTS idx_sched_code ON schedules(date, train_code);

CREATE TABLE IF NOT EXISTS transfer_rules (
  station_code TEXT PRIMARY KEY,
  min_minutes  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS city_transfers (
  station_a   TEXT NOT NULL,
  station_b   TEXT NOT NULL,
  min_minutes INTEGER NOT NULL,
  PRIMARY KEY (station_a, station_b)
);

CREATE TABLE IF NOT EXISTS airports (
  code TEXT PRIMARY KEY,     -- IATA 三字码，如 PKX
  name TEXT NOT NULL,        -- 大兴国际机场
  city TEXT NOT NULL,        -- 城市名（与 stations.city 对齐，如 北京）
  icao TEXT,
  name_en TEXT,
  province TEXT,
  country TEXT DEFAULT '中国',
  lat REAL,
  lon REAL,
  active INTEGER NOT NULL DEFAULT 1,
  kind TEXT,
  source TEXT NOT NULL DEFAULT 'dynamic'
);

CREATE TABLE IF NOT EXISTS flights (
  flight_no   TEXT NOT NULL,  -- 航班号，如 KN5977
  airline     TEXT NOT NULL,
  date        TEXT NOT NULL,  -- 起飞日 YYYY-MM-DD
  seq         INTEGER NOT NULL,       -- 航段序（经停航班多段）
  dep_airport TEXT NOT NULL,
  arr_airport TEXT NOT NULL,
  dep         TEXT NOT NULL,  -- HH:MM
  arr         TEXT NOT NULL,
  arr_day     INTEGER NOT NULL DEFAULT 0,  -- 相对起飞日的天偏移
  price       REAL,           -- 真实票价（元，飞猪 ticketPrice）
  PRIMARY KEY (flight_no, date, seq)
);
CREATE INDEX IF NOT EXISTS idx_flights_route ON flights(date, dep_airport, arr_airport);

CREATE TABLE IF NOT EXISTS flight_route_cache (
  origin      TEXT NOT NULL,
  destination TEXT NOT NULL,
  date        TEXT NOT NULL,
  fetched_at  TEXT NOT NULL,
  raw         TEXT NOT NULL,
  PRIMARY KEY (origin, destination, date)
);

CREATE TABLE IF NOT EXISTS geo_cache (
  name       TEXT NOT NULL,   -- 站名/机场名（含"站"后缀的形式）
  city       TEXT NOT NULL,
  lon        REAL NOT NULL,
  lat        REAL NOT NULL,
  fetched_at TEXT NOT NULL,
  failed     INTEGER NOT NULL DEFAULT 0,  -- 1 = 上次尝试失败（负缓存）
  PRIMARY KEY (name, city)
);


CREATE TABLE IF NOT EXISTS transit_cache (
  from_name  TEXT NOT NULL,
  to_name    TEXT NOT NULL,
  raw        TEXT NOT NULL,   -- JSON（null = 失败缓存）
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (from_name, to_name)
);


-- 自驾车程缓存（高德驾车路径规划；城市级 OD，与日期无关）
CREATE TABLE IF NOT EXISTS drive_route_cache (
  from_city  TEXT NOT NULL,
  to_city    TEXT NOT NULL,
  distance_m INTEGER NOT NULL DEFAULT 0,
  duration_s INTEGER NOT NULL DEFAULT 0,
  tolls      REAL NOT NULL DEFAULT 0,
  strategy   INTEGER NOT NULL DEFAULT 0,
  raw        TEXT NOT NULL DEFAULT '',
  fetched_at TEXT NOT NULL,
  failed     INTEGER NOT NULL DEFAULT 0,   -- 1 = 上次抓取失败（负缓存）
  PRIMARY KEY (from_city, to_city, strategy)
);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""


def default_db_path() -> Path:
    env = os.environ.get("TRAVEL_PLANNER_DB")
    if env:
        return Path(env)
    return PROJECT_ROOT / "data" / "timetable.db"


def connect(path: Optional[str | Path] = None) -> sqlite3.Connection:
    p = Path(path) if path else default_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    # 轻量迁移：旧库补列
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(flights)")}
    if "price" not in cols:
        conn.execute("ALTER TABLE flights ADD COLUMN price REAL")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(geo_cache)")}
    if "failed" not in cols:
        conn.execute("ALTER TABLE geo_cache ADD COLUMN failed INTEGER NOT NULL DEFAULT 0")
    # 机场主数据字段：兼容早期只有 code/name/city 的动态缓存库。
    airport_cols = {r["name"] for r in conn.execute("PRAGMA table_info(airports)")}
    airport_add = {
        "icao": "TEXT", "name_en": "TEXT", "province": "TEXT",
        "country": "TEXT DEFAULT '中国'", "lat": "REAL", "lon": "REAL",
        "active": "INTEGER NOT NULL DEFAULT 1", "kind": "TEXT",
        "source": "TEXT NOT NULL DEFAULT 'dynamic'",
    }
    for name, typ in airport_add.items():
        if name not in airport_cols:
            conn.execute(f"ALTER TABLE airports ADD COLUMN {name} {typ}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_airport_city ON airports(city)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_airport_active ON airports(active)")
    return conn


def import_airports_master(conn: sqlite3.Connection, rows: Iterable[Dict],
                           *, replace: bool = False) -> int:
    """导入静态机场主数据，并与动态机场缓存合并。

    IATA code 是唯一键。静态字段会更新，动态查询写入的机场也会保留；
    ``replace`` 仅清理主数据标记的旧静态记录，不删除动态机场。
    """
    clean = []
    for row in rows:
        code = str(row.get("code") or "").strip().upper()
        name = str(row.get("name") or "").strip()
        city = str(row.get("city") or "").strip()
        if not code or not name or not city:
            continue
        clean.append((code, name, city, row.get("icao") or None,
                      row.get("name_en") or None, row.get("province") or None,
                      row.get("country") or "中国", row.get("lat") or None,
                      row.get("lon") or None, int(row.get("active", 1)),
                      row.get("kind") or "civil"))
    with conn:
        if replace:
            conn.execute("DELETE FROM airports WHERE source='master'")
        conn.executemany(
            "INSERT INTO airports(code,name,city,icao,name_en,province,country,lat,lon,active,kind,source) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,'master') "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name,city=excluded.city,"
            "icao=excluded.icao,name_en=excluded.name_en,province=excluded.province,"
            "country=excluded.country,lat=excluded.lat,lon=excluded.lon,"
            "active=excluded.active,kind=excluded.kind,source='master'",
            clean,
        )
    return len(clean)


# 轻量种子资料：覆盖常见的长途铁路/环线枢纽。用户可在数据库中修改，
# 也可通过 upsert_city_tourism 写入自己的评分和介绍；INSERT OR IGNORE 确保
# 后续升级不会覆盖个人编辑。
CITY_TOURISM_SEED = (
    ("北京", 9.2, "故宫、胡同与中轴线适合第一次到访；城市交通和博物馆密度很高。", "上午故宫或天安门片区，下午景山/什刹海。", "历史,博物馆,美食"),
    ("武威", 7.7, "河西走廊上的安静古城，雷台汉墓和文庙适合短暂停留。", "雷台汉墓—文庙—凉州市场。", "丝路,历史,小城"),
    ("兰州", 7.9, "黄河穿城而过，牛肉面、黄河风情线和甘肃省博物馆适合半日衔接。", "省博物馆—黄河风情线—正宁路夜市。", "丝路,美食,博物馆"),
    ("西宁", 8.0, "高原门户，适合在继续前往青海或西藏前休整和适应海拔。", "东关清真大寺—莫家街，留出时间休息。", "高原,休整,美食"),
    ("拉萨", 9.1, "布达拉宫、大昭寺和八廓街集中体现藏地人文；高海拔应预留适应时间。", "大昭寺—八廓街慢走，避免首日过度安排。", "高原,人文,慢旅行"),
    ("西安", 9.1, "古都遗迹和夜间美食集中，城墙、碑林与回民街可组合成紧凑半日。", "城墙—钟鼓楼—回民街。", "历史,美食,古都"),
    ("成都", 8.9, "适合把行程放慢，茶馆、川菜和宽窄巷子能在转车日轻松完成。", "人民公园喝茶—宽窄巷子—川菜晚餐。", "美食,休闲,熊猫"),
    ("重庆", 8.6, "山城地形和夜景辨识度高，半日适合集中在解放碑或两江沿线。", "解放碑—洪崖洞—两江夜景。", "山城,夜景,美食"),
    ("郑州", 7.2, "交通枢纽，市区景点适合作为短暂补给；若时间充裕可去河南博物院。", "河南博物院—二七广场。", "枢纽,博物馆,美食"),
    ("太原", 7.5, "晋文化资源丰富，山西博物院是转车半日的稳妥选择。", "山西博物院—汾河公园。", "晋文化,博物馆"),
    ("银川", 7.8, "贺兰山与西夏文化特色鲜明，适合在西北线路中安排一晚。", "宁夏博物馆—鼓楼/老城。", "西夏,沙漠,美食"),
    ("乌鲁木齐", 8.0, "新疆交通和物资集散中心，国际大巴扎适合短停，但城市间距离较长。", "新疆博物馆—国际大巴扎。", "新疆,美食,补给"),
    ("昆明", 8.3, "气候舒适、适合休整，滇池和老街可以低强度游览。", "翠湖—云南省博物馆或滇池。", "休整,自然,美食"),
    ("大理", 9.0, "苍山洱海和古城适合慢游，半天也能获得较完整的旅行体验。", "大理古城—人民路—洱海生态廊道。", "洱海,古城,慢旅行"),
    ("丽江", 8.6, "古城夜景和周边雪山辨识度高，注意节假日拥挤和高原反应。", "丽江古城—木府—狮子山。", "古城,雪山,夜景"),
    ("贵阳", 7.9, "气候凉爽、黔菜丰富，适合作为西南长途中的休整点。", "青岩古镇（时间足够时）或市区甲秀楼。", "美食,休整,古镇"),
    ("桂林", 8.5, "山水资源突出；半日先安排象鼻山和两江四湖，漓江完整游览需更多时间。", "象鼻山—两江四湖。", "山水,休闲"),
    ("广州", 8.3, "早茶、骑楼和珠江夜景适合转车日，餐饮选择丰富。", "陈家祠—永庆坊—珠江沿岸。", "美食,岭南,夜景"),
    ("深圳", 7.8, "现代城市与海滨公园为主，适合短暂停留和补给。", "莲花山—市民中心—人才公园。", "城市,海滨,现代"),
    ("厦门", 8.6, "岛城步行体验好，鼓浪屿需要单独安排船票和更完整的半天。", "鼓浪屿或中山路—沙坡尾二选一。", "海滨,步行,小城"),
    ("上海", 8.8, "外滩、老城和博物馆密集，适合按兴趣选择一片区域深入。", "外滩—南京东路—豫园。", "城市,博物馆,夜景"),
    ("南京", 8.5, "六朝古都，城墙、中山陵和博物馆适合分次游览。", "总统府—玄武湖，或夫子庙秦淮河。", "历史,园林,美食"),
    ("杭州", 8.8, "西湖周边适合低强度慢走，公共交通和住宿选择充足。", "西湖断桥—北山街—曲院风荷。", "西湖,园林,慢旅行"),
    ("合肥", 7.1, "省会型枢纽，包公园与安徽博物院适合转车短停。", "安徽博物院—天鹅湖。", "枢纽,博物馆"),
    ("济南", 8.0, "泉水和老城步行线集中，适合半天吃逛。", "趵突泉—大明湖—曲水亭街。", "泉水,老城,美食"),
    ("青岛", 8.4, "沿海老城、海风和啤酒是亮点，车站周边即可完成经典线路。", "栈桥—八大关—小青岛。", "海滨,建筑,美食"),
    ("沈阳", 7.7, "故宫与工业遗产兼具，适合东北线路中的历史停留。", "沈阳故宫—中街。", "历史,东北,美食"),
    ("长春", 7.5, "伪满遗址与电影文化是特色，城市尺度适中。", "伪满皇宫—桂林路。", "历史,电影,美食"),
    ("哈尔滨", 8.5, "中央大街、松花江和建筑街区辨识度高，冬季需预留保暖时间。", "圣索菲亚教堂—中央大街—松花江。", "建筑,冰雪,美食"),
    ("呼和浩特", 7.5, "草原文化与乳制品特色明显，适合北线短停。", "内蒙古博物院—大召寺。", "草原,民族,美食"),
    ("大连", 8.0, "海滨城市与近代建筑适合轻松半日，城市公共交通方便。", "星海广场—滨海路或老街区。", "海滨,建筑,休闲"),
)


def seed_city_tourism(conn: sqlite3.Connection) -> None:
    """写入内置城市资料，不覆盖用户已编辑的记录。"""
    conn.executemany(
        "INSERT OR IGNORE INTO city_tourism "
        "(city,recommendation_score,intro,halfday_plan,tags,updated_at) "
        "VALUES(?,?,?,?,?,datetime('now'))",
        CITY_TOURISM_SEED,
    )
    conn.commit()


def get_city_tourism(conn: sqlite3.Connection, city: str) -> Optional[Dict]:
    """读取城市旅游资料；兼容输入带“市/地区”后缀的常见写法。"""
    if not city:
        return None
    candidates = [city.strip()]
    for suffix in ("市", "地区", "自治州"):
        if city.endswith(suffix) and len(city) > len(suffix):
            candidates.append(city[:-len(suffix)])
    ph = ",".join("?" * len(candidates))
    row = conn.execute(
        f"SELECT city,recommendation_score,intro,halfday_plan,tags,updated_at "
        f"FROM city_tourism WHERE city IN ({ph}) ORDER BY CASE city "
        + " ".join(f"WHEN ? THEN {i}" for i in range(len(candidates)))
        + " ELSE 99 END LIMIT 1",
        [*candidates, *candidates],
    ).fetchone()
    return dict(row) if row else None


def list_city_tourism(conn: sqlite3.Connection, cities: Optional[Sequence[str]] = None) -> List[Dict]:
    """批量读取城市资料，cities 为空时按推荐指数降序返回全部。"""
    if not cities:
        rows = conn.execute(
            "SELECT city,recommendation_score,intro,halfday_plan,tags,updated_at "
            "FROM city_tourism ORDER BY recommendation_score DESC, city").fetchall()
    else:
        clean = [c.strip() for c in cities if c and c.strip()]
        if not clean:
            return []
        ph = ",".join("?" * len(clean))
        rows = conn.execute(
            f"SELECT city,recommendation_score,intro,halfday_plan,tags,updated_at "
            f"FROM city_tourism WHERE city IN ({ph})", clean).fetchall()
    return [dict(r) for r in rows]


def upsert_city_tourism(conn: sqlite3.Connection, city: str,
                        recommendation_score: float, intro: str,
                        halfday_plan: str = "", tags: str = "") -> None:
    """保存个人城市资料。评分限制在 0~10，避免前端展示异常值。"""
    score = max(0.0, min(10.0, float(recommendation_score)))
    with conn:
        conn.execute(
            "INSERT INTO city_tourism(city,recommendation_score,intro,halfday_plan,tags,updated_at) "
            "VALUES(?,?,?,?,?,datetime('now')) ON CONFLICT(city) DO UPDATE SET "
            "recommendation_score=excluded.recommendation_score,intro=excluded.intro,"
            "halfday_plan=excluded.halfday_plan,tags=excluded.tags,updated_at=excluded.updated_at",
            (city.strip(), score, intro.strip(), halfday_plan.strip(), tags.strip()),
        )


def upsert_stations(conn: sqlite3.Connection, rows: Iterable[Dict]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO stations(code,name,pinyin,abbrev,city,telecode,ordinal) "
        "VALUES(:code,:name,:pinyin,:abbrev,:city,:telecode,:ordinal)",
        rows,
    )
    conn.commit()


def upsert_schedule(conn: sqlite3.Connection, train_no: str, train_code: str,
                    date: str, stops: List[Dict]) -> None:
    """整列替换某车次某日的停站序列。stops 每项含 seq/station_code/station_name/arr/dep/day。"""
    rows = []
    for s in stops:
        rows.append((
            train_no, train_code, date,
            s["seq"], s["station_code"], s["station_name"],
            s["day"], s["arr"], s["dep"],
        ))
    with conn:
        conn.execute("DELETE FROM schedules WHERE train_no=? AND date=?", (train_no, date))
        conn.executemany(
            "INSERT INTO schedules(train_no,train_code,date,seq,station_code,station_name,day,arr,dep) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            rows,
        )


def resolve_station(conn: sqlite3.Connection, q: str) -> List[sqlite3.Row]:
    """按三字码/站名精确匹配，否则站名模糊匹配。"""
    rows = conn.execute(
        "SELECT code,name,city FROM stations WHERE code=? OR name=? ORDER BY ordinal LIMIT 20",
        (q, q),
    ).fetchall()
    if rows:
        return rows
    return conn.execute(
        "SELECT code,name,city FROM stations WHERE name LIKE ? ORDER BY ordinal LIMIT 12",
        (f"%{q}%",),
    ).fetchall()


def _tmin(a: Optional[str], b: Optional[str]) -> int:
    """'HH:MM' -> 分钟；'--' 时用另一个值兜底。"""
    for v in (a, b):
        if v and v != "--":
            hh, mm = v.split(":")[:2]
            return int(hh) * 60 + int(mm)
    raise ValueError("无有效时刻")


def load_trips(conn: sqlite3.Connection, date: str) -> List[Trip]:
    """读取某日全部车次，转换为 RAPTOR 用的 Trip（绝对分钟，相对查询日 0 点）。"""
    rows = conn.execute(
        "SELECT train_no, train_code, seq, station_code, station_name, day, arr, dep "
        "FROM schedules WHERE date=? ORDER BY train_no, seq",
        (date,),
    ).fetchall()
    trips: List[Trip] = []
    cur: Optional[Dict] = None
    for r in rows:
        if cur is None or cur["trip_id"] != r["train_no"]:
            if cur is not None:
                trips.append(Trip(**cur))
            cur = {"trip_id": r["train_no"], "code": r["train_code"],
                   "stations": [], "arr": [], "dep": []}
        t_arr = _tmin(r["arr"], r["dep"])
        t_dep = _tmin(r["dep"], r["arr"])
        cur["stations"].append(r["station_code"])
        cur["arr"].append(r["day"] * 1440 + t_arr)
        cur["dep"].append(r["day"] * 1440 + t_dep)
    if cur is not None:
        trips.append(Trip(**cur))
    return trips


def load_flights(conn: sqlite3.Connection, date: str,
                 origins: Optional[Sequence[str]] = None,
                 destinations: Optional[Sequence[str]] = None) -> List[Trip]:
    """读取某日缓存航班为 Trip。

    ``origins`` / ``destinations`` 可限制首段出发、末段到达机场。默认读取
    当日全部航班（兼容旧调用）；规划单条航段时应传入这两个集合，避免把
    同一天其它已缓存航线混进候选图。
    """
    rows = conn.execute(
        "SELECT flight_no, airline, seq, dep_airport, arr_airport, dep, arr, arr_day "
        "FROM flights WHERE date=? ORDER BY flight_no, seq", (date,)).fetchall()
    trips: List[Trip] = []
    cur: Optional[Dict] = None
    for r in rows:
        if cur is None or cur["trip_id"] != r["flight_no"]:
            if cur is not None:
                trips.append(Trip(**cur))
            cur = {"trip_id": r["flight_no"], "code": r["flight_no"],
                   "stations": [], "arr": [], "dep": []}
        dep_min = _tmin(r["dep"], r["arr"])
        arr_min = r["arr_day"] * 1440 + _tmin(r["arr"], r["dep"])
        dep_code = air_code(r["dep_airport"])
        arr_code = air_code(r["arr_airport"])
        # 经停航班：上一航段的到达机场即下一航段出发机场，不重复添加
        if not cur["stations"] or cur["stations"][-1] != dep_code:
            cur["stations"].append(dep_code)
            cur["arr"].append(dep_min)
            cur["dep"].append(dep_min)
        else:
            cur["dep"][-1] = dep_min  # 经停点出发时间以下一航段为准
        cur["stations"].append(arr_code)
        cur["arr"].append(arr_min)
        cur["dep"].append(arr_min)
    if cur is not None:
        trips.append(Trip(**cur))
    if origins is None and destinations is None:
        return trips
    oset = set(origins or ())
    dset = set(destinations or ())
    return [t for t in trips
            if (not oset or (t.stations and t.stations[0] in oset))
            and (not dset or (t.stations and t.stations[-1] in dset))]


def build_mixed_footpaths(conn: sqlite3.Connection,
                          rail_stations: Sequence[str],
                          airports: Sequence[str],
                          default_mct: int = DEFAULT_MCT_MIN,
                          default_city_mct: int = DEFAULT_CITY_MCT_MIN,
                          air_mct: int = AIR_MCT_MIN,
                          air_air_city: int = AIR_AIR_CITY_MIN,
                          air_rail_city: int = AIR_RAIL_CITY_MIN
                          ) -> Dict[str, List[tuple]]:
    """混合步行/换乘边：铁路同站/同城 + 机场同场/同城 + 机场↔铁路站同城。"""
    fp = build_footpaths(conn, rail_stations, default_mct, default_city_mct)
    if not airports:
        return fp
    airs = [a[len(AIR_PREFIX):] if a.startswith(AIR_PREFIX) else a
            for a in airports]
    ph = ",".join("?" * len(airs))
    rows = conn.execute(
        f"SELECT code, city FROM airports WHERE code IN ({ph})", airs).fetchall()

    air_city: Dict[str, List[str]] = {}
    for r in rows:
        air_city.setdefault(r["city"] or "", []).append(air_code(r["code"]))
    # 机场同场自环
    for a in airs:
        pa = air_code(a)
        fp.setdefault(pa, []).append((pa, air_mct))
    # 同城异机场
    for members in air_city.values():
        for a in members:
            for b in members:
                if a != b:
                    fp.setdefault(a, []).append((b, air_air_city))
    # 机场 ↔ 铁路站（同城）
    rail_rows = conn.execute("SELECT code, city FROM stations WHERE code IN ({})"
                             .format(",".join("?" * len(rail_stations))),
                             list(rail_stations)).fetchall()
    rail_city: Dict[str, List[str]] = {}
    for r in rail_rows:
        rail_city.setdefault(r["city"] or "", []).append(r["code"])
    for city, airs_in_city in air_city.items():
        for st in rail_city.get(city, []):
            for a in airs_in_city:
                fp.setdefault(st, []).append((a, air_rail_city))
                fp.setdefault(a, []).append((st, air_rail_city))
    return fp


def build_footpaths(conn: sqlite3.Connection, stations: Sequence[str],
                    default_mct: int = DEFAULT_MCT_MIN,
                    default_city_mct: int = DEFAULT_CITY_MCT_MIN) -> Dict[str, List[tuple]]:
    """步行/换乘边：同站(mct) + 同城异站(city_mct)。键 = 到达站，值 = [(可换乘站, 分钟)]。"""
    codes = list(stations)
    if not codes:
        return {}
    ph = ",".join("?" * len(codes))
    rules = {r["station_code"]: r["min_minutes"]
             for r in conn.execute(
                 f"SELECT station_code, min_minutes FROM transfer_rules WHERE station_code IN ({ph})",
                 codes)}
    fp: Dict[str, List[tuple]] = {c: [(c, rules.get(c, default_mct))] for c in codes}

    city: Dict[str, List[str]] = {}
    name_by_code: Dict[str, str] = {}
    for r in conn.execute(
            f"SELECT code, name, city FROM stations WHERE code IN ({ph})", codes):
        name_by_code[r["code"]] = r["name"]
        if r["city"]:
            city.setdefault(r["city"], []).append(r["code"])
    overrides = {(a, b): m for a, b, m in conn.execute(
        "SELECT station_a, station_b, min_minutes FROM city_transfers")}
    for city_name, members in city.items():
        for a in members:
            for b in members:
                if a == b:
                    continue
                # 仅当两站站名都包含城市名时才视为真·同城换乘
                # （防止把下辖县站误当同城，如 大理-鹤庆）
                if not (city_name in name_by_code[a] and city_name in name_by_code[b]):
                    continue
                fp[a].append((b, overrides.get((a, b), default_city_mct)))
    return fp


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def ensure_date_data(conn: sqlite3.Connection, date: str, base_date: str) -> bool:
    """date 无数据时从 base_date 复制（车次每日近似相同，隔日/调图车次例外）。

    返回该日期最终是否有数据。base_date 与 date 相同时不做任何事。
    """
    if conn.execute("SELECT 1 FROM schedules WHERE date=? LIMIT 1", (date,)).fetchone():
        return True
    if date == base_date:
        return False
    n = conn.execute("SELECT COUNT(*) c FROM schedules WHERE date=?",
                     (base_date,)).fetchone()["c"]
    if n == 0:
        return False
    with conn:
        conn.execute(
            "INSERT INTO schedules(train_no,train_code,date,seq,station_code,"
            "station_name,day,arr,dep) "
            "SELECT train_no,train_code,?,seq,station_code,station_name,day,arr,dep "
            "FROM schedules WHERE date=?",
            (date, base_date))
    set_meta(conn, f"copy:{date}:from", base_date)
    set_meta(conn, f"crawl:{date}:stats",
             f"copied from {base_date} ({n} rows, auto by ensure_date_data)")
    return True

