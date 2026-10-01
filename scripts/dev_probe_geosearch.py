"""开发探针：维基 geosearch 的框到底怎么写才返回东西。

    python scripts/dev_probe_geosearch.py 上海
    python scripts/dev_probe_geosearch.py            # 用几个内置城市挨个试
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from travel_planner import catalog  # noqa: E402

API = "https://zh.wikipedia.org/w/api.php"
UA = {"User-Agent": "travel-planner-coord-check/1.0 (personal research; contact: local-user)"}


def call(params: dict) -> tuple:
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": f"HTTP {exc.code}",
                          "retry": exc.headers.get("Retry-After") if exc.headers else None}


def main(argv=None) -> int:
    names = argv[1:] or ["上海", "三亚", "东营", "北京"]
    for name in names:
        city = catalog.city(name)
        if not city or city.get("lat") is None:
            print(f"{name}: 目录里没有")
            continue
        lat, lon = float(city["lat"]), float(city["lon"])
        span = 0.8
        bbox = (lat - span, lon - span, lat + span, lon + span)
        variants = {
            "top|left|bottom|right": f"{bbox[2]}|{bbox[1]}|{bbox[0]}|{bbox[3]}",
            "bottom|left|top|right": f"{bbox[0]}|{bbox[1]}|{bbox[2]}|{bbox[3]}",
            "小框(±0.15)": f"{lat + 0.15}|{lon - 0.15}|{lat - 0.15}|{lon + 0.15}",
        }
        print(f"\n== {name}（{lat},{lon}）==")
        for label, value in variants.items():
            status, data = call({"action": "query", "list": "geosearch", "gsbbox": value,
                                 "gslimit": 20, "gsnamespace": 0, "format": "json"})
            items = ((data.get("query") or {}).get("geosearch") or []) if status == 200 else []
            err = data.get("error") if isinstance(data.get("error"), str) else None
            print(f"  {label:22s} bbox={value:38s} → {status} {len(items)} 条"
                  + (f"  {err}" if err else "")
                  + ("  例：" + "、".join(i["title"] for i in items[:4]) if items else ""))
            time.sleep(1.6)
        # 对照组：按坐标搜索（不受框写法影响）
        status, data = call({"action": "query", "list": "geosearch",
                             "gscoord": f"{lat}|{lon}", "gsradius": 10000, "gslimit": 10,
                             "gsnamespace": 0, "format": "json"})
        items = ((data.get("query") or {}).get("geosearch") or []) if status == 200 else []
        print(f"  对照 gscoord 10km        → {status} {len(items)} 条"
              + ("  例：" + "、".join(i["title"] for i in items[:5]) if items else ""))
        time.sleep(1.6)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
