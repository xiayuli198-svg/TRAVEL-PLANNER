"""一次性预取全国车站/机场坐标到 geo_cache（供票价里程估算使用）。

用法:
    python scripts/prewarm_coords.py                 # 只补没查过的（负缓存里的也会重试）
    python scripts/prewarm_coords.py --force         # 连已成功的也重查
    python scripts/prewarm_coords.py --no-retry-failed   # 失败过的这次不碰

后台运行约 5-20 分钟（取决于要补多少条）；成功后后续查询即时命中缓存。

**踩过的坑**：`amap.geocode()` 对 `failed=1` 的负缓存是**直接返回 None** 的（不查网络），
所以第一版本脚本「重试失败项」时其实是秒回失败 —— 日志显示「新增成功 0 失败 685」，
看着像新 key 也不能用，其实一个请求都没发出去。现在显式传 `retry_failed=True` 绕过负缓存。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from travel_planner import db  # noqa: E402
from travel_planner.ingest import amap  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main() -> None:
    argv = sys.argv[1:]
    force = "--force" in argv
    retry_failed = "--no-retry-failed" not in argv
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

    pending = conn.execute("SELECT COUNT(*) c FROM geo_cache WHERE failed=1").fetchone()["c"]
    done = skipped = failed = 0
    fixed = 0
    t0 = time.time()
    print(f"开始：共 {len(targets)} 个目标 · 负缓存里待重试 {pending} 条"
          f"（force={force} retry_failed={retry_failed}）", flush=True)
    for idx, (name, city) in enumerate(targets, start=1):
        try:
            row = conn.execute(
                "SELECT failed FROM geo_cache WHERE name=? AND city=?",
                (name, city)).fetchone()
            was_failed = bool(row and row["failed"])
            if row is not None and not was_failed and not force:
                skipped += 1
                if idx % 200 == 0:
                    print(f"[{idx}/{len(targets)}] 跳过中 {skipped}…", flush=True)
                continue
            if was_failed and not (retry_failed or force):
                skipped += 1
                continue
            # 高德个人 QPS≈3：限速 + 失败重试（防限流误判）
            ok = False
            for attempt in range(3):
                if amap.geocode(conn, key, name, city,
                                retry_failed=retry_failed or force) is not None:
                    ok = True
                    break
                time.sleep(0.6 * (attempt + 1))
            time.sleep(0.3)
            if ok:
                done += 1
                fixed += 1 if was_failed else 0
            else:
                failed += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"异常 {name}/{city}: {type(e).__name__} {str(e)[:100]}", flush=True)
        if idx % 25 == 0 and (done or failed):
            print(f"[{idx}/{len(targets)}] 新增成功 {done}（其中救回旧失败 {fixed}）"
                  f" 失败 {failed} 跳过 {skipped} 用时 {time.time() - t0:.0f}s", flush=True)

    print(f"完成：新增/更新 {done}（救回旧失败 {fixed}），失败 {failed}，已有 {skipped}，"
          f"用时 {(time.time() - t0) / 60:.1f} 分钟", flush=True)


if __name__ == "__main__":
    main()
