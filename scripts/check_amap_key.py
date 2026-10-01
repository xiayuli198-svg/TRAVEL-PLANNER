"""验证一个高德 key 能不能用在本项目真正会调的那几个接口上（不写任何配置）。

    python -X utf8 scripts/check_amap_key.py <KEY>          # 测新 key
    python -X utf8 scripts/check_amap_key.py --current      # 测当前配置里的 key

本项目用到的接口：
* Web 服务：`/v3/geocode/geo`（站名/景点名 → 坐标）
* Web 服务：`/v3/place/text`（POI 池，城内点位与动线）
* Web 服务：`/v3/direction/transit/integrated`（市内腿的中转指引）
* Web 服务：`/v3/direction/driving`（自驾里程/耗时/过路费）
* Web 服务：`/v3/staticmap`（地图底图，可选）
* Web 端 JS API：地球页签的「高德 3D」按钮（只能在浏览器里用，这里只提示要看什么）

key 的类型是分开的：**Web 服务 key** 与 **Web 端(JS API) key** 不是一回事，
所以下面会把两类结果分开报，别把「JS API 没权限」当成「key 坏了」。
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from travel_planner import db                       # noqa: E402
from travel_planner.ingest import amap              # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

#: 高德常见错误码 → 人话
INFOCODES = {
    "10001": "key 无效（INVALID_USER_KEY）：拼错、或这串不是 Web 服务 key",
    "10002": "该 key 没有这个服务的权限（SERVICE_NOT_AVAILABLE）",
    "10003": "今日调用量超限（DAILY_QUERY_OVER_LIMIT）",
    "10004": "访问过于频繁（ACCESS_TOO_FREQUENT）",
    "10005": "IP 白名单限制（INVALID_USER_IP）：高德控制台里限了来源 IP",
    "10006": "域名白名单限制（INVALID_USER_DOMAIN）：这是 JS API key 用在服务端了",
    "10007": "签名/时间戳无效（INVALID_USER_SIGNATURE）",
    "10009": "key 平台类型不匹配（USERKEY_PLAT_NOMATCH）：把 JS API key 拿来调 Web 服务了",
    "10012": "权限过期（OVER_QUOTA）",
    "10019": "该服务未开通（SERVICE_NOT_OPEN）",
    "20800": "缺少必填参数",
    "20801": "参数不合法",
    "20802": "请求过于频繁",
    "20803": "签名校验失败",
}


def call(path: str, params: dict, timeout: int = 15) -> dict:
    url = f"https://restapi.amap.com/v3/{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:                        # noqa: BLE001
        return {"status": "0", "info": f"请求失败：{type(exc).__name__}: {exc}",
                "infocode": "NETWORK"}


def describe(result: dict) -> tuple[bool, str]:
    if result.get("status") == "1":
        return True, "OK"
    code = str(result.get("infocode") or "")
    info = str(result.get("info") or "")
    return False, f"{code} {info} — {INFOCODES.get(code, '（未收录的错误码）')}"


def main(argv: list[str]) -> int:
    if len(argv) >= 1 and argv[0] == "--current":
        conn = db.connect()
        try:
            key = amap.get_key(conn) or ""
        finally:
            conn.close()
    elif argv:
        key = argv[0].strip()
    else:
        print(__doc__)
        return 2
    if not key:
        print("✗ 没有拿到 key")
        return 2

    print(f"待测 key：{key[:6]}…{key[-4:]}（{len(key)} 位）\n")
    checks = [
        ("地理编码 /v3/geocode/geo", "geocode/geo", {"address": "北京南站", "city": "北京"}),
        ("POI 检索 /v3/place/text", "place/text",
         {"keywords": "趵突泉", "city": "济南", "offset": 1, "page": 1}),
        ("公交中转 /v3/direction/transit/integrated", "direction/transit/integrated",
         {"origin": "116.378,39.865", "destination": "116.407,39.904", "city": "北京",
          "strategy": 0}),
        ("驾车路径 /v3/direction/driving", "direction/driving",
         {"origin": "116.378,39.865", "destination": "116.407,39.904", "strategy": 0,
          "extensions": "base"}),
    ]
    ok_services = 0
    for label, path, params in checks:
        res = call(path, {**params, "key": key})
        good, why = describe(res)
        ok_services += 1 if good else 0
        detail = ""
        if good:
            if path == "geocode/geo":
                g = (res.get("geocodes") or [{}])[0]
                detail = f"{g.get('formatted_address', '')} {g.get('location', '')}"
            elif path == "place/text":
                p = (res.get("pois") or [{}])[0]
                detail = f"{p.get('name', '')} {p.get('location', '')}"
            elif "transit" in path:
                t = ((res.get("route") or {}).get("transits") or [{}])[0]
                detail = f"约 {round(int(t.get('duration') or 0) / 60)} 分钟"
            elif "driving" in path:
                p = ((res.get("route") or {}).get("paths") or [{}])[0]
                detail = f"{p.get('distance', '?')} 米 / {p.get('tolls', '?')} 元过路费"
        print(f"{'✓' if good else '✗'} {label}：{why}" + (f" · {detail}" if detail else ""))

    print(f"\nWeb 服务接口：{ok_services}/{len(checks)} 通过")
    print("提示：地球页签的「高德 3D」用的是 **Web端(JS API) key**，只能在浏览器里验证；")
    print("      如果那里报 INVALID_USER_DOMAIN/10006，说明这串是纯 Web 服务 key，")
    print("      需要在高德控制台另建一个「Web端(JS API)」类型的 key（或给它配域名白名单）。")
    return 0 if ok_services else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
