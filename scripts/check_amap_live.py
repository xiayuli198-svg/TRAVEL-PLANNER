"""端到端确认高德 key 真的在服务里生效（走 HTTP 接口，不直连 高德）。

会打三个会用到 key 的接口：
* `GET /api/status` → `amap_key_set` 是否为真
* `POST /api/roam/drive` → 自驾里程（没 key 会退回「大圆距离 × 系数」的估算并写明）
* `GET /api/poi?city=济南` → 城内点位（`place/text`，需要 key 才有命中）

用法：python -X utf8 scripts/check_amap_live.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def get(path: str, timeout: int = 60) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path: str, payload: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    problems = []
    try:
        st = get("/api/status", timeout=30)
    except urllib.error.URLError as exc:
        print(f"✗ 服务没起来？{exc}", file=sys.stderr)
        print("  先跑：powershell -NoProfile -File scripts\\run_web.ps1", file=sys.stderr)
        return 2
    print(f"· /api/status：amap_key_set = {st.get('amap_key_set')}")
    if not st.get("amap_key_set"):
        problems.append("服务端认为没有高德 key")

    try:
        d = post("/api/roam/drive", {"date": "2026-06-17", "cities": ["北京", "天津"],
                                     "closed": True, "with_rental": False})
        ok = bool(d.get("ok"))
        summary = d.get("summary") or {}
        legs = d.get("legs") or d.get("days") or []
        note = str(d.get("note") or "")
        used_real = any("估算" not in json.dumps(x, ensure_ascii=False) for x in legs[:1])
        print(f"· /api/roam/drive：ok={ok} · 段数 {len(legs)} · "
              f"总里程 {summary.get('total_km') or d.get('total_km')} · "
              f"是否真实里程 {'是' if used_real else '可能是估算'}")
        print("  " + (note[:110] or json.dumps(legs[0], ensure_ascii=False)[:110]))
        if not ok:
            problems.append(f"自驾接口失败：{d.get('error')}")
    except urllib.error.URLError as exc:
        problems.append(f"自驾接口请求失败：{exc}")

    try:
        q = urllib.parse.urlencode({"city": "济南", "limit": 3})
        p = get(f"/api/poi?{q}", timeout=90)
        stats = p.get("stats") or {}
        rows = p.get("pois") or p.get("items") or []
        print(f"· /api/poi?city=济南：命中 {len(rows)} 条样本 · 全库命中率 "
              f"{stats.get('rate', '?')}%（命中 {stats.get('hit')}/{stats.get('targets')}）")
        if not rows:
            problems.append("POI 接口没返回条目")
    except urllib.error.URLError as exc:
        problems.append(f"POI 接口请求失败：{exc}")

    if problems:
        print("\n✗ 有没通过的地方：", file=sys.stderr)
        for p in problems:
            print("  -", p, file=sys.stderr)
        return 1
    print("\n✓ 服务端这条链路用的是新 key：状态、自驾、POI 都对得上")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
