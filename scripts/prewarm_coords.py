"""一次性预取全国车站/机场坐标到 geo_cache（供票价里程估算使用）。

用法: python scripts/prewarm_coords.py [--force]
后台运行约 10 分钟；成功后后续查询即时命中缓存。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from travel_planner import db  # noqa: E402
from travel_planner.ingest import amap  # noqa: E402


def main() -> None:
    force = "--force" in sys.argv
    conn = db.connect()
    key = amap.get_key(conn)
    if not key:
        print("未配置高德 key：python -m travel_planner.cli set-amap-key KEY")
        return

    targets = []
    for r in conn.execute("SELECT code,name,city FROM stations ORDER BY ordinal"):
        targets.append((r["name"] + "站", r["city"]))
    for r in conn.execute("SELECT code,name,city FROM airports"):
        targets.append((r["name"], r["city"]))

    done = skipped = failed = 0
    t0 = time.time()
    print(f"开始：共 {len(targets)} 个目标", flush=True)
    for idx, (name, city) in enumerate(targets, start=1):
        try:
            row = conn.execute(
                "SELECT failed FROM geo_cache WHERE name=? AND city=?",
                (name, city)).fetchone()
            if row is not None and not row["failed"] and not force:
                skipped += 1
                if idx % 200 == 0:
                    print(f"[{idx}/{len(targets)}] 跳过中 {skipped}…", flush=True)
                continue
            # 高德个人 QPS≈3：限速 + 失败重试（防限流误判）
            ok = False
            for attempt in range(3):
                if amap.geocode(conn, key, name, city) is not None:
                    ok = True
                    break
                time.sleep(0.6 * (attempt + 1))
            time.sleep(0.3)
            if ok:
                done += 1
            else:
                failed += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"异常 {name}/{city}: {type(e).__name__} {str(e)[:100]}", flush=True)
        if idx % 25 == 0 and (done or failed):
            print(f"[{idx}/{len(targets)}] 新增成功 {done} 失败 {failed} 跳过 {skipped} "
                  f"用时 {time.time() - t0:.0f}s", flush=True)

    print(f"完成：新增/更新 {done}，失败 {failed}，已有 {skipped}，"
          f"用时 {(time.time() - t0) / 60:.1f} 分钟", flush=True)


if __name__ == "__main__":
    main()
