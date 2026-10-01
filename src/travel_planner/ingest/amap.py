"""高德开放平台 Web服务 API 客户端：地理编码 + 公交路径规划（中转指引）。

个人自用、带本地缓存（geo_cache / transit_cache），key 存 meta 表或环境变量 AMAP_KEY。
配额：个人认证开发者公交路径规划 15 万次/月，本工具用量远低于此。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

BASE = "https://restapi.amap.com/v3"
KEY_META = "amap_key"


def get_key(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute(f"SELECT value FROM meta WHERE key='{KEY_META}'").fetchone()
    if row and row["value"]:
        return row["value"]
    return os.environ.get("AMAP_KEY")


def _get(url: str, timeout: int = 15) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def geocode(conn: sqlite3.Connection, key: str, name: str, city: str,
            retry_failed: bool = False) -> Optional[Tuple[float, float]]:
    """地址/站名 → 经纬度（缓存，含失败负缓存）。

    ``retry_failed=True`` 时**忽略负缓存**，重新问一次高德：

    这一条是踩出来的。`geo_cache` 把查不到的名字记成 ``failed=1`` 以避免反复撞，
    但这也意味着「换了个 key 再跑一遍 prewarm」会全部秒回 None —— 调用方看到的是
    「新增成功 0 失败 685」，看着像新 key 也不能用，其实是自己短路了，一个请求都没发。
    """
    row = conn.execute(
        "SELECT lon,lat,failed FROM geo_cache WHERE name=? AND city=?",
        (name, city)).fetchone()
    if row and not (retry_failed and row["failed"]):
        if row["failed"]:
            return None
        return (row["lon"], row["lat"])
    params = urllib.parse.urlencode({"address": name, "city": city, "key": key})
    try:
        rj = _get(f"{BASE}/geocode/geo?{params}")
    except Exception:  # noqa: BLE001
        rj = {}
    if rj.get("status") != "1" or not rj.get("geocodes"):
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO geo_cache(name,city,lon,lat,fetched_at,failed) "
                "VALUES(?,?,0,0,?,1)",
                (name, city, dt.datetime.now().isoformat(timespec="seconds")))
        return None
    loc = (rj["geocodes"][0].get("location") or "").strip()
    try:
        lon_s, lat_s = loc.split(",")
        lon, lat = float(lon_s), float(lat_s)
    except ValueError:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO geo_cache(name,city,lon,lat,fetched_at,failed) "
                "VALUES(?,?,0,0,?,1)",
                (name, city, dt.datetime.now().isoformat(timespec="seconds")))
        return None
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO geo_cache(name,city,lon,lat,fetched_at,failed) "
            "VALUES(?,?,?,?,?,0)",
            (name, city, lon, lat, dt.datetime.now().isoformat(timespec="seconds")))
    return (lon, lat)


def transit_guide(conn: sqlite3.Connection, key: str,
                  from_name: str, from_city: str,
                  to_name: str, to_city: str,
                  frm_is_airport: bool = False, to_is_airport: bool = False
                  ) -> Optional[Dict]:
    """两点间公交/地铁中转指引（缓存，含失败缓存）。

    返回 {duration_sec, walk_m, distance_m, lines:[{line, from, to}]} 或 None。
    """
    row = conn.execute("SELECT raw FROM transit_cache WHERE from_name=? AND to_name=?",
                       (from_name, to_name)).fetchone()
    if row:
        return json.loads(row["raw"])

    frm_addr = from_name + ("" if frm_is_airport else "站")
    to_addr = to_name + ("" if to_is_airport else "站")
    frm_xy = geocode(conn, key, frm_addr, from_city)
    to_xy = geocode(conn, key, to_addr, to_city)
    guide: Optional[Dict] = None
    if frm_xy and to_xy:
        params = urllib.parse.urlencode({
            "origin": f"{frm_xy[0]},{frm_xy[1]}",
            "destination": f"{to_xy[0]},{to_xy[1]}",
            "city": from_city,
            "strategy": "0",
            "key": key,
        })
        try:
            rj = _get(f"{BASE}/direction/transit/integrated?{params}")
        except Exception:  # noqa: BLE001
            rj = {}
        if rj.get("status") == "1":
            route = rj.get("route") or {}
            transits = route.get("transits") or []
            if transits:
                pick = _pick_transit(transits)
                lines: List[Dict[str, str]] = []
                for seg in pick.get("segments") or []:
                    buslines = ((seg or {}).get("bus") or {}).get("buslines") or []
                    if buslines:
                        bl = buslines[0]
                        lines.append({
                            "line": bl.get("name") or "",
                            "from": (bl.get("departure_stop") or {}).get("name") or "",
                            "to": (bl.get("arrival_stop") or {}).get("name") or "",
                        })
                guide = {
                    "duration_sec": int(pick.get("duration") or 0),
                    "walk_m": int(pick.get("walking_distance") or 0),
                    "distance_m": int(pick.get("distance") or 0),
                    "lines": lines,
                    "has_subway": any("地铁" in ln["line"] for ln in lines),
                }
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO transit_cache(from_name,to_name,raw,fetched_at) "
            "VALUES(?,?,?,?)",
            (from_name, to_name, json.dumps(guide, ensure_ascii=False),
             dt.datetime.now().isoformat(timespec="seconds")))
    return guide


def _pick_transit(transits: list) -> dict:
    """选择中转方案：默认取最快；若最快的纯公交而存在含地铁方案（慢不超过 25 分钟），
    按「地铁优先、公交次之」改选地铁方案。"""
    best = transits[0]
    best_dur = int(best.get("duration") or 0)

    def has_metro(t):
        for seg in t.get("segments") or []:
            for bl in ((seg or {}).get("bus") or {}).get("buslines") or []:
                if "地铁" in (bl.get("name") or ""):
                    return True
        return False

    if not has_metro(best):
        for t in transits[1:]:
            if has_metro(t) and int(t.get("duration") or 0) <= best_dur + 1500:
                return t
    return best


def fmt_guide(g: Optional[Dict]) -> str:
    if not g:
        return ""
    mins = max(1, round(g["duration_sec"] / 60))
    lines = g.get("lines") or []
    parts = [f"{ln['line']} {ln['from']}→{ln['to']}" for ln in lines]
    body = " → ".join(parts) if parts else "步行"
    has_sub = any("地铁" in (ln.get("line") or "") for ln in lines)
    if parts and not has_sub:
        body = "🚌 " + body  # 纯公交方案（含机场线等）标注巴士
    return f"{body} | 约 {mins} 分钟 · 步行 {g['walk_m']} 米"
