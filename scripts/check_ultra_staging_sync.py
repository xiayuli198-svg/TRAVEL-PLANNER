"""核对：暂存区里没归档的那份 dmy_south 到底和正式文件一致不一致。

子代理在合并之后又改了一版暂存文件，并直接替换了正式文件里的 10 行 —— 于是
`build_ultra_loops.py --check` 会一直报「id 重复（暂存区不能覆盖已收录的环线）」。
这里逐条比对，确认「暂存区那版 == 已收录那版」，然后才敢归档（否则就是拿旧版覆盖新版）。

用法：python scripts/check_ultra_staging_sync.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "route_content" / "ultra_loops.json"
STAGING = ROOT / "data" / "route_content" / "_staging"
ARCHIVE = STAGING / "_archive"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def norm(item: dict) -> str:
    """比对时忽略键顺序与内部标记（_file/_merged）。"""
    clean = {k: v for k, v in item.items() if not k.startswith("_")}
    return json.dumps(clean, ensure_ascii=False, sort_keys=True)


def main() -> int:
    merged = {x["id"]: x for x in json.loads(OUT.read_text(encoding="utf-8"))["loops"]}
    files = sorted(STAGING.glob("ultra_loops_*.json"))
    if not files:
        print("暂存区里没有 ultra_loops_*.json，无需处理")
        return 0

    print(f"正式文件 {len(merged)} 条；暂存区待核对 {len(files)} 个文件")
    dup_same, dup_diff, fresh = [], [], []
    for path in files:
        items = json.loads(path.read_text(encoding="utf-8")).get("loops") or []
        for item in items:
            rid = str(item.get("id") or "")
            if rid not in merged:
                fresh.append((path.name, rid))
            elif norm(item) == norm(merged[rid]):
                dup_same.append((path.name, rid))
            else:
                dup_diff.append((path.name, rid))

    print(f"  已收录且内容一致（可以直接归档）：{len(dup_same)} 条")
    print(f"  已收录但内容不同（要先决定留哪版）：{len(dup_diff)} 条"
          + (f" → {'、'.join(r for _, r in dup_diff)}" if dup_diff else ""))
    print(f"  尚未收录（需要合并）：{len(fresh)} 条"
          + (f" → {'、'.join(r for _, r in fresh)}" if fresh else ""))

    if dup_diff:
        print("\n✗ 有不一致的条目，先别归档：逐条看清哪版是新的", file=sys.stderr)
        for name, rid in dup_diff:
            a, b = merged[rid], None
            for path in files:
                if path.name != name:
                    continue
                for item in json.loads(path.read_text(encoding="utf-8")).get("loops") or []:
                    if item.get("id") == rid:
                        b = item
            if b:
                print(f"  · {rid}: 正式 {a.get('days')} 天 / 暂存 {b.get('days')} 天；"
                      f"正式城市 {a.get('cities')}；暂存城市 {b.get('cities')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
