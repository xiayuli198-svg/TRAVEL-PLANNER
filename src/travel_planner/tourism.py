"""独立的城市旅游资料库。

旅游资料与时刻表分开保存，便于个人维护、备份和替换种子数据。数据库路径
可通过 ``TRAVEL_PLANNER_TOURISM_DB`` 覆盖，默认是 ``data/tourism.db``。

数据来源分两层：

1. ``src/travel_planner/data/city_catalog.json`` —— 内置城市目录（200+ 城市，
   含坐标、评分、半日/一日路线、看点、是否通铁路），首次连接时写入数据库；
2. 数据库本身 —— 用户可以用 ``upsert`` / 直接改 SQLite 覆盖任何一条记录，
   内置目录只做 ``INSERT OR IGNORE`` 与「空字段补齐」，不会覆盖个人编辑。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from . import catalog
from .db import CITY_TOURISM_SEED

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 新增列（相对最初的 5 列版本）；用于老库的平滑迁移。
EXTRA_COLUMNS = (
    ("region", "TEXT NOT NULL DEFAULT ''"),
    ("province", "TEXT NOT NULL DEFAULT ''"),
    ("lat", "REAL"),
    ("lon", "REAL"),
    ("highlights", "TEXT NOT NULL DEFAULT ''"),
    ("oneday_plan", "TEXT NOT NULL DEFAULT ''"),
    ("best_season", "TEXT NOT NULL DEFAULT ''"),
    ("suggest_hours", "REAL NOT NULL DEFAULT 0"),
    ("suggest_days", "REAL NOT NULL DEFAULT 0"),
    ("food", "TEXT NOT NULL DEFAULT ''"),
    ("photo", "TEXT NOT NULL DEFAULT ''"),
    ("hub", "INTEGER NOT NULL DEFAULT 0"),
    ("note", "TEXT NOT NULL DEFAULT ''"),
    ("rail", "INTEGER NOT NULL DEFAULT 1"),
    ("plan_name", "TEXT NOT NULL DEFAULT ''"),
    ("night", "INTEGER NOT NULL DEFAULT 0"),
    ("guide", "TEXT NOT NULL DEFAULT ''"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS city_tourism (
  city TEXT PRIMARY KEY,
  recommendation_score REAL NOT NULL DEFAULT 7.0,
  intro TEXT NOT NULL DEFAULT '',
  halfday_plan TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT '',
  region TEXT NOT NULL DEFAULT '',
  province TEXT NOT NULL DEFAULT '',
  lat REAL,
  lon REAL,
  highlights TEXT NOT NULL DEFAULT '',
  oneday_plan TEXT NOT NULL DEFAULT '',
  best_season TEXT NOT NULL DEFAULT '',
  suggest_hours REAL NOT NULL DEFAULT 0,
  suggest_days REAL NOT NULL DEFAULT 0,
  food TEXT NOT NULL DEFAULT '',
  photo TEXT NOT NULL DEFAULT '',
  hub INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '',
  rail INTEGER NOT NULL DEFAULT 1,
  plan_name TEXT NOT NULL DEFAULT '',
  night INTEGER NOT NULL DEFAULT 0,
  guide TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_city_tourism_score
  ON city_tourism(recommendation_score DESC);
CREATE TABLE IF NOT EXISTS city_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# 依赖新增列的索引，必须等 _migrate 补完列之后再建（老库没有 region 列）。
REGION_INDEX = """
CREATE INDEX IF NOT EXISTS idx_city_tourism_region ON city_tourism(region);
"""

SELECT_COLUMNS = (
    "city,recommendation_score,intro,halfday_plan,tags,updated_at,region,province,"
    "lat,lon,highlights,oneday_plan,best_season,suggest_hours,suggest_days,food,"
    "photo,hub,note,rail,plan_name,night,guide"
)


def default_path() -> Path:
    value = os.environ.get("TRAVEL_PLANNER_TOURISM_DB")
    return Path(value) if value else PROJECT_ROOT / "data" / "tourism.db"


def connect(path: Optional[str | Path] = None) -> sqlite3.Connection:
    p = Path(path) if path else default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    seed(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """老库补列：city_tourism 最初只有 5 列。"""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(city_tourism)")}
    for name, decl in EXTRA_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE city_tourism ADD COLUMN {name} {decl}")
    conn.executescript(REGION_INDEX)
    conn.commit()


# ---------------------------------------------------------------- 种子写入
def _signature() -> str:
    cities = catalog.cities()
    payload = "|".join(
        f"{c['name']}:{c.get('score')}:{len(c.get('intro') or '')}:{c.get('plan_name') or ''}:"
        f"{1 if c.get('night') else 0}:{len(json.dumps(c.get('guide') or {}, ensure_ascii=False))}"
        for c in cities)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM city_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO city_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _highlights_text(items: Iterable) -> str:
    parts: List[str] = []
    for item in items or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            kind = str(item.get("kind") or "").strip()
            if name:
                parts.append(f"{name}|{kind}")
        elif item:
            parts.append(str(item).strip())
    return "；".join(parts)


def _row_from_catalog(item: dict) -> tuple:
    return (
        item["name"],
        float(item.get("score") or 7.0),
        (item.get("intro") or "").strip(),
        (item.get("halfday") or "").strip(),
        ",".join(item.get("tags") or []),
        item.get("region") or "",
        item.get("province") or "",
        item.get("lat"),
        item.get("lon"),
        _highlights_text(item.get("highlights")),
        (item.get("oneday") or "").strip(),
        (item.get("best_season") or "").strip(),
        float(item.get("hours") or 0),
        float(item.get("days") or 0),
        (item.get("food") or "").strip(),
        (item.get("photo") or ""),
        1 if item.get("hub") else 0,
        (item.get("note") or "").strip(),
        1 if item.get("rail", True) else 0,
        item.get("plan_name") or item["name"],
        1 if item.get("night") else 0,
        json.dumps(item.get("guide") or {}, ensure_ascii=False) if item.get("guide") else "",
    )


INSERT_SQL = (
    "INSERT OR IGNORE INTO city_tourism "
    "(city,recommendation_score,intro,halfday_plan,tags,region,province,lat,lon,"
    " highlights,oneday_plan,best_season,suggest_hours,suggest_days,food,photo,hub,note,"
    " rail,plan_name,night,guide,updated_at) "
    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))"
)

# 只补齐空字段，不覆盖任何已有内容（含用户手改的评分与文字）。
BACKFILL_SQL = """
UPDATE city_tourism SET
  region = CASE WHEN COALESCE(region,'')='' THEN ? ELSE region END,
  province = CASE WHEN COALESCE(province,'')='' THEN ? ELSE province END,
  lat = COALESCE(lat, ?),
  lon = COALESCE(lon, ?),
  highlights = CASE WHEN COALESCE(highlights,'')='' THEN ? ELSE highlights END,
  oneday_plan = CASE WHEN COALESCE(oneday_plan,'')='' THEN ? ELSE oneday_plan END,
  best_season = CASE WHEN COALESCE(best_season,'')='' THEN ? ELSE best_season END,
  suggest_hours = CASE WHEN COALESCE(suggest_hours,0)=0 THEN ? ELSE suggest_hours END,
  suggest_days = CASE WHEN COALESCE(suggest_days,0)=0 THEN ? ELSE suggest_days END,
  food = CASE WHEN COALESCE(food,'')='' THEN ? ELSE food END,
  hub = CASE WHEN COALESCE(hub,0)=0 THEN ? ELSE hub END,
  note = CASE WHEN COALESCE(note,'')='' THEN ? ELSE note END,
  plan_name = CASE WHEN COALESCE(plan_name,'')='' THEN ? ELSE plan_name END,
  night = CASE WHEN COALESCE(night,0)=0 THEN ? ELSE night END,
  guide = CASE WHEN COALESCE(guide,'')='' THEN ? ELSE guide END
WHERE city = ?
"""


def seed(conn: sqlite3.Connection, force: bool = False) -> None:
    """写入内置城市目录；不覆盖用户已编辑的记录，只补齐空字段。"""
    signature = _signature()
    if not force and _meta(conn, "catalog_signature") == signature:
        return
    rows = [_row_from_catalog(item) for item in catalog.cities()]
    with conn:
        conn.executemany(INSERT_SQL, rows)
        # 老库（或新增列）补齐：把目录里有的信息填进空字段。
        for item in catalog.cities():
            conn.execute(BACKFILL_SQL, (
                item.get("region") or "", item.get("province") or "",
                item.get("lat"), item.get("lon"),
                _highlights_text(item.get("highlights")),
                (item.get("oneday") or "").strip(),
                (item.get("best_season") or "").strip(),
                float(item.get("hours") or 0), float(item.get("days") or 0),
                (item.get("food") or "").strip(),
                1 if item.get("hub") else 0,
                (item.get("note") or "").strip(),
                item.get("plan_name") or item["name"],
                1 if item.get("night") else 0,
                json.dumps(item.get("guide") or {}, ensure_ascii=False) if item.get("guide") else "",
                item["name"],
            ))
        # 旧版内置的 31 城（来自 db.CITY_TOURISM_SEED）保持可用。
        conn.executemany(
            "INSERT OR IGNORE INTO city_tourism "
            "(city,recommendation_score,intro,halfday_plan,tags,updated_at) "
            "VALUES(?,?,?,?,?,datetime('now'))", CITY_TOURISM_SEED)
        _set_meta(conn, "catalog_signature", signature)
        _set_meta(conn, "catalog_cities", str(len(rows)))


# ---------------------------------------------------------------- 读取
def _candidates(city: str) -> List[str]:
    city = (city or "").strip()
    if not city:
        return []
    values = [city]
    for suffix in ("市", "地区", "自治州", "盟", "特别行政区"):
        if city.endswith(suffix) and len(city) > len(suffix):
            values.append(city[:-len(suffix)])
    return values


def _parse_highlights(text: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for chunk in (text or "").split("；"):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, kind = chunk.partition("|")
        name = name.strip()
        if name:
            out.append({"name": name, "kind": kind.strip() or "看点"})
    return out


def _row_to_dict(row: sqlite3.Row) -> Dict:
    data = dict(row)
    data["highlights"] = _parse_highlights(data.get("highlights") or "")
    data["highlight_names"] = [h["name"] for h in data["highlights"]]
    data["tags"] = [t for t in (data.get("tags") or "").replace("，", ",").split(",") if t]
    data["hub"] = bool(data.get("hub"))
    data["rail"] = bool(data.get("rail", 1))
    data["night"] = bool(data.get("night", 0))
    raw_guide = data.get("guide") or ""
    if raw_guide:
        try:
            data["guide"] = json.loads(raw_guide)
        except json.JSONDecodeError:
            data["guide"] = {}
    else:
        data["guide"] = {}
    return data


def get(conn: sqlite3.Connection, city: str) -> Optional[Dict]:
    values = _candidates(city)
    if not values:
        return None
    ph = ",".join("?" * len(values))
    row = conn.execute(
        f"SELECT {SELECT_COLUMNS} FROM city_tourism WHERE city IN ({ph})", values).fetchone()
    return _row_to_dict(row) if row else None


def list_for(conn: sqlite3.Connection, cities: Optional[Sequence[str]] = None) -> List[Dict]:
    if not cities:
        rows = conn.execute(
            f"SELECT {SELECT_COLUMNS} FROM city_tourism "
            "ORDER BY recommendation_score DESC, city").fetchall()
    else:
        values = [c for c in cities if c]
        if not values:
            return []
        ph = ",".join("?" * len(values))
        rows = conn.execute(
            f"SELECT {SELECT_COLUMNS} FROM city_tourism WHERE city IN ({ph})", values).fetchall()
    return [_row_to_dict(r) for r in rows]


def in_region(conn: sqlite3.Connection, region: str) -> List[Dict]:
    rows = conn.execute(
        f"SELECT {SELECT_COLUMNS} FROM city_tourism WHERE region=? "
        "ORDER BY recommendation_score DESC, city", (region,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def counts(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT COALESCE(NULLIF(region,''),'未分组') region, COUNT(*) c "
        "FROM city_tourism GROUP BY region").fetchall()
    return {r["region"]: r["c"] for r in rows}


def upsert(conn: sqlite3.Connection, city: str, recommendation_score: float,
           intro: str, halfday_plan: str = "", tags: str = "", **extra) -> None:
    """写入/更新城市资料；extra 可带 region/province/lat/lon/highlights 等新字段。"""
    score = max(0.0, min(10.0, float(recommendation_score)))
    fields = {
        "city": (city or "").strip(),
        "recommendation_score": score,
        "intro": (intro or "").strip(),
        "halfday_plan": (halfday_plan or "").strip(),
        "tags": (tags or "").strip(),
    }
    allowed = {name for name, _ in EXTRA_COLUMNS}
    for key, value in extra.items():
        if key in allowed and value is not None:
            fields[key] = value
    columns = ",".join(fields)
    placeholders = ",".join("?" * len(fields))
    updates = ",".join(f"{k}=excluded.{k}" for k in fields if k != "city")
    with conn:
        conn.execute(
            f"INSERT INTO city_tourism({columns},updated_at) "
            f"VALUES({placeholders},datetime('now')) ON CONFLICT(city) DO UPDATE SET "
            f"{updates},updated_at=excluded.updated_at",
            tuple(fields.values()),
        )


def export_json(conn: sqlite3.Connection) -> str:
    """导出当前资料库为 JSON（备份/分享用）。"""
    return json.dumps({"cities": list_for(conn)}, ensure_ascii=False, indent=1)
