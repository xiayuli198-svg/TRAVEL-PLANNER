"""飞猪官方 AI MCP search_flight 客户端（个人规划用途，本地缓存）。

协议：HMAC-SHA256 签名 + AES-GCM 加密的 x-ff-ctx。
凭证通过 `FLIGGY_API_KEY` / `FLIGGY_SIGN_SECRET` 环境变量提供；该端点属半官方
公开接口，可能被收回/限流——请求保持克制并缓存结果（flight_route_cache）。

需要依赖: pip install cryptography
"""
from __future__ import annotations

import base64
import datetime as dt
import gzip
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

MCP_URL = "https://flyai.open.fliggy.com/mcp"
X_TTID = "ai2c(sk.clawhub)"
UA = "flyai-cli/1.0.6"
SIGN_VER = "7"

_device_id_cache: Optional[str] = None


def _credentials() -> tuple[str, str]:
    """读取飞猪凭证，避免把可调用的密钥放进源码或数据库。"""
    api_key = os.environ.get("FLIGGY_API_KEY", "").strip()
    sign_secret = os.environ.get("FLIGGY_SIGN_SECRET", "").strip()
    if not api_key or not sign_secret:
        raise RuntimeError(
            "缺少飞猪凭证，请设置 FLIGGY_API_KEY 和 FLIGGY_SIGN_SECRET；"
            "未配置时可使用纯铁路模式"
        )
    return api_key, sign_secret


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _device_id() -> str:
    global _device_id_cache
    if _device_id_cache is not None:
        return _device_id_cache
    # 与项目数据目录统一，避免正式服务和探针生成两个不同的设备指纹。
    path = Path(__file__).resolve().parents[3] / "data" / ".fliggy_device_id"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and len(path.read_text().strip()) == 64:
        _device_id_cache = path.read_text().strip()
    else:
        _device_id_cache = hashlib.sha256(secrets.token_bytes(16)).hexdigest()
        path.write_text(_device_id_cache)
    return _device_id_cache


def _ctx_dict() -> dict:
    return {
        "machine": {"platform": "windows", "arch": "AMD64", "cpus": 8,
                    "memoryTierGB": 16, "osType": "windows",
                    "nodeVersion": "v22.22.0", "osReleaseMajor": "10"},
        "fingerprint": {"language": "zh-CN", "platform": "windows",
                        "userAgent": f"{UA} (Node.js v22.22.0; windows AMD64)",
                        "hardwareConcurrency": 8, "deviceMemory": 8,
                        "clientSurface": "cli", "timezoneOffset": -480,
                        "deviceId": _device_id()},
    }


def _build_ff_ctx() -> str:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as e:
        raise RuntimeError("需要 pip install cryptography") from e
    _, sign_secret = _credentials()
    key = hashlib.sha256(sign_secret.encode()).digest()
    iv = secrets.token_bytes(12)
    compressed = gzip.compress(json.dumps(_ctx_dict()).encode())
    enc = AESGCM(key).encrypt(iv, compressed, None)
    return base64.b64encode(bytes([0x01]) + iv + enc).decode()


def search_flights(origin: str, destination: str, date: str,
                   limit: int = 50, timeout: int = 20) -> List[Dict]:
    """查询某日 OD 的全部航班。

    返回去重后的航班列表，每项：
    {flight_no, airline, segments: [{dep_airport, arr_airport, dep_date, dep_time,
                                    arr_date, arr_time, arr_day, dep_name, arr_name}...]}
    origin/destination 可为城市名（如 北京）或 IATA 码。
    """
    args: Dict = {"origin": origin, "destination": destination, "depDate": date}
    if limit:
        args["limit"] = limit
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search_flight", "arguments": args},
    }, ensure_ascii=False)
    ts = str(int(time.time() * 1000))
    nonce = secrets.token_hex(16)
    api_key, sign_secret = _credentials()
    auth = f"Bearer {api_key}"
    sign_string = (f"POST\n/mcp\n{ts}\n{nonce}\n{_sha256_hex(body)}\n{_sha256_hex(auth)}")
    sig = hmac.new(sign_secret.encode(), sign_string.encode(),
                   hashlib.sha256).digest()
    sig = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": auth,
        "x-ff-ctx": _build_ff_ctx(),
        "x-ttid": X_TTID,
        "User-Agent": UA,
        "x-flyai-ts": ts,
        "x-flyai-sign-ver": SIGN_VER,
        "x-flyai-sign-alg": "hmac-sha256",
        "x-flyai-nonce": nonce,
        "x-flyai-sign": sig,
    }
    req = urllib.request.Request(MCP_URL, data=body.encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        rj = json.loads(resp.read())
    content = (rj.get("result") or {}).get("content") or []
    if not content:
        raise ValueError(f"响应无 content: {str(rj)[:200]}")
    text = content[0].get("text")
    if not text:
        raise ValueError(f"content 无 text: {str(content[0])[:200]}")
    data = json.loads(text)
    items = ((data.get("data") or {}).get("itemList")) or []
    if not items:
        raise ValueError(f"无航班数据: {(data.get('message') or data.get('systemMessage') or '')[:200]}")

    seen = {}
    flights: List[Dict] = []
    for item in items:
        journeys = item.get("journeys") or []
        if not journeys:
            continue
        segs = journeys[0].get("segments") or []
        if not segs:
            continue
        fno = segs[0].get("marketingTransportNo", "")
        airline = segs[0].get("marketingTransportName", "")
        try:
            price = float(item.get("ticketPrice") or 0) or None
        except (TypeError, ValueError):
            price = None
        seg_out = []
        ok = True
        for s in segs:
            dep_dt = (s.get("depDateTime") or "").strip()
            arr_dt = (s.get("arrDateTime") or "").strip()
            if len(dep_dt) < 16 or len(arr_dt) < 16:
                ok = False
                break
            dep_date = dep_dt[:10]
            arr_date = arr_dt[:10]
            try:
                arr_day = (dt.date.fromisoformat(arr_date)
                           - dt.date.fromisoformat(dep_date)).days
            except ValueError:
                ok = False
                break
            seg_out.append({
                "dep_airport": s.get("depStationCode") or "",
                "arr_airport": s.get("arrStationCode") or "",
                "dep_name": s.get("depStationName") or "",
                "arr_name": s.get("arrStationName") or "",
                "dep_date": dep_date, "dep_time": dep_dt[11:16],
                "arr_date": arr_date, "arr_time": arr_dt[11:16],
                "arr_day": arr_day,
            })
        if not ok or not fno:
            continue
        key = (fno, seg_out[0]["dep_date"], seg_out[0]["dep_time"])
        if key in seen:
            # 同航班多舱位/多价格：保留最低价
            if price is not None and (seen[key]["price"] is None or price < seen[key]["price"]):
                seen[key]["price"] = price
            continue
        seen[key] = {"flight_no": fno, "airline": airline,
                     "segments": seg_out, "price": price}
    return list(seen.values())


def fetch_and_store(conn: sqlite3.Connection, origin_city: str, dest_city: str,
                    date: str, limit: int = 50, force: bool = False) -> int:
    """查询并把航班/机场入库（带缓存）。返回该航线当日航班数。"""
    row = conn.execute(
        "SELECT raw FROM flight_route_cache WHERE origin=? AND destination=? AND date=?",
        (origin_city, dest_city, date)).fetchone()
    if row and not force:
        # 直接以该 OD 的原始缓存计数，不依赖 airports.city；旧版本可能把
        # 经停机场误标成端点城市，按城市反查会返回错误数量。
        try:
            return len(json.loads(row["raw"] or "[]"))
        except (TypeError, ValueError):
            pass

    flights = search_flights(origin_city, dest_city, date, limit=limit)
    incoming = [f["flight_no"] for f in flights]

    for f in flights:
        segs = f["segments"]
        # 只有整条航程的首个出发机场/最后到达机场能确定城市；经停机场
        # 不要误标成 OD 两端城市，否则后续按城市筛选会把它当作端点。
        endpoint_city = {}
        if segs:
            endpoint_city[segs[0]["dep_airport"]] = origin_city
            endpoint_city[segs[-1]["arr_airport"]] = dest_city
        for s in segs:
            for code, name in ((s["dep_airport"], s["dep_name"]),
                               (s["arr_airport"], s["arr_name"])):
                if code:
                    conn.execute(
                        "INSERT OR IGNORE INTO airports(code,name,city) VALUES(?,?,?)",
                        (code, name, endpoint_city.get(code, "")))
    with conn:
        if incoming:
            ph = ",".join("?" * len(incoming))
            conn.execute(f"DELETE FROM flights WHERE date=? AND flight_no IN ({ph})",
                         [date] + incoming)
        for f in flights:
            for i, s in enumerate(f["segments"], start=1):
                conn.execute(
                    "INSERT INTO flights(flight_no,airline,date,seq,dep_airport,"
                    "arr_airport,dep,arr,arr_day,price) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (f["flight_no"], f["airline"], date, i,
                     s["dep_airport"], s["arr_airport"],
                     s["dep_time"], s["arr_time"], s["arr_day"],
                     f.get("price")))
        conn.execute(
            "INSERT OR REPLACE INTO flight_route_cache(origin,destination,date,"
            "fetched_at,raw) VALUES(?,?,?,?,?)",
            (origin_city, dest_city, date,
             dt.datetime.now().isoformat(timespec="seconds"),
             json.dumps(flights, ensure_ascii=False)))
    return len(flights)


def repair_airport_cities(conn: sqlite3.Connection) -> int:
    """根据已缓存航线的 OD 端点，修复机场表中被经停段误标的城市。

    早期版本把每个经停段的出发/到达机场强行标成 OD 两端城市，导致例如
    西安经停机场被写成北京或上海。此修复只采信每条航程的首段出发、末段
    到达机场，并按投票最多的城市更新；无法推断的机场保持原值。
    返回更新行数。
    """
    votes: Dict[str, Dict[str, int]] = {}
    names: Dict[str, str] = {}
    rows = conn.execute("SELECT origin,destination,raw FROM flight_route_cache").fetchall()
    for row in rows:
        try:
            flights = json.loads(row["raw"] or "[]")
        except (TypeError, ValueError):
            continue
        for flight in flights:
            segs = flight.get("segments") or []
            if not segs:
                continue
            endpoints = ((segs[0], "dep_airport", "dep_name", row["origin"]),
                         (segs[-1], "arr_airport", "arr_name", row["destination"]))
            for seg, code_key, name_key, city in endpoints:
                code = (seg.get(code_key) or "").strip().upper()
                if not code or not city:
                    continue
                names.setdefault(code, seg.get(name_key) or "")
                city_votes = votes.setdefault(code, {})
                city_votes[city] = city_votes.get(city, 0) + 1
    changed = 0
    with conn:
        for code, city_votes in votes.items():
            city = max(city_votes, key=city_votes.get)
            name = names.get(code)
            cur = conn.execute("SELECT city,name FROM airports WHERE code=?", (code,)).fetchone()
            if cur is None:
                conn.execute("INSERT INTO airports(code,name,city) VALUES(?,?,?)",
                             (code, name or code, city)); changed += 1
            elif cur[0] != city or (name and not cur[1]):
                conn.execute("UPDATE airports SET city=?, name=COALESCE(NULLIF(name,''),?) WHERE code=?",
                             (city, name or cur[1], code)); changed += 1
    return changed
