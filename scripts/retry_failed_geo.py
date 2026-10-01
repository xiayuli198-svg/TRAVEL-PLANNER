"""看看换了 key 之后，之前那批「地理编码失败」的负缓存还能不能救回来。

`geo_cache` 会把查不到的名字记成 `failed=1`（负缓存，避免反复撞），所以**换 key 之后
旧的失败记录仍然拦着**——这些名字要等重试才会重新查。本脚本抽样几条直接试一次，
用真实命中率回答「值不值得跑一遍全量重试」。

用法：
    python -X utf8 scripts/retry_failed_geo.py --sample 8          # 只抽样试，不写库
    python -X utf8 scripts/retry_failed_geo.py --all --limit 50    # 真重试并写回缓存
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.parse
import urllib.request
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from travel_planner import db                          # noqa: E402
from travel_planner.ingest import amap                 # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def geocode_once(key: str, name: str, city: str) -> tuple[bool, str]:
    params = urllib.parse.urlencode({"address": name, "city": city, "key": key})
    try:
        with urllib.request.urlopen(f"{amap.BASE}/geocode/geo?{params}", timeout=15) as r:
            rj = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:                             # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if rj.get("status") != "1":
        return False, f"{rj.get('infocode')} {rj.get('info')}"
    codes = rj.get("geocodes") or []
    if not codes:
        return False, "没有匹配地址"
    return True, codes[0].get("location", "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="重试失败的坐标查询")
    ap.add_argument("--sample", type=int, default=8, help="抽样条数（默认 8，不写库）")
    ap.add_argument("--all", action="store_true", help="真重试并写回 geo_cache")
    ap.add_argument("--limit", type=int, default=0, help="--all 时最多重试多少条")
    ap.add_argument("--delay", type=float, default=0.2, help="每次请求间隔秒")
    args = ap.parse_args(argv)

    conn = db.connect()
    try:
        key = amap.get_key(conn) or ""
        if not key:
            print("✗ 没有高德 key", file=sys.stderr)
            return 2
        total = conn.execute("SELECT COUNT(*) c FROM geo_cache WHERE failed=1").fetchone()["c"]
        print(f"geo_cache 里的失败记录：{total} 条 · 用 key {key[:6]}…{key[-4:]} 试")

        want = args.limit if (args.all and args.limit) else args.sample
        rows = conn.execute(
            "SELECT name, city, lon, lat, failed, fetched_at FROM geo_cache WHERE failed=1 "
            "ORDER BY fetched_at DESC LIMIT ?", (int(want),)).fetchall()
        ok = 0
        for r in rows:
            # 走 amap.geocode 的 retry_failed：绕过负缓存，成功时会把该行改回 failed=0
            got = amap.geocode(conn, key, r["name"], r["city"], retry_failed=True)
            good = got is not None
            ok += 1 if good else 0
            detail = f"{got[0]:.6f},{got[1]:.6f}" if good else "仍然查不到"
            if not args.all:
                # 抽样模式不留副作用：把这一行**还原成原样**（含原来的失败标记）。
                # 注意别直接 DELETE —— 那会把原本存在的负缓存整条抹掉，抽样也会改数据。
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO geo_cache(name,city,lon,lat,fetched_at,failed) "
                        "VALUES(?,?,?,?,?,?)",
                        (r["name"], r["city"], r["lon"], r["lat"], r["fetched_at"], r["failed"]))
            print(f"  {'✓' if good else '✗'} {r['name']}（{r['city']}）{detail}")
            time.sleep(args.delay)

        print(f"\n抽样 {len(rows)} 条：成功 {ok} 条")
        if not args.all:
            print("（这是抽样，没写库。要在全量上重试并按结果更新缓存："
                  "python -X utf8 scripts/retry_failed_geo.py --all）")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
