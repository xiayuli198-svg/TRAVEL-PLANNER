"""把海南大环线的预算调回真实口径。

子代理为了让 `test_budget_is_budget_travel`（当时小环线上限 320 元/天）通过，
把 14 天的海南线压到 3600-5200 元（≈314 元/天）。但「往返机票 + 岛上 14 天」
按公开攻略口径通常在 4200-6000 元，压到 314 元/天不是穷游，是**把数据改成迁就测试**。

正确做法是测试按大小环线分开定上限（已改：大环线 480 元/天），数据回到公开口径。

用法：
    python scripts/fix_ultra_budgets.py --dry-run
    python scripts/fix_ultra_budgets.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "route_content" / "ultra_loops.json"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

#: id → (新下限, 新上限, 为什么)
FIXES = {
    "dmy_south_004": (4200, 6000, "14 天海南环岛含往返机票，公开攻略口径约 4200-6000 元"),
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="修正 Ultra 里被压低的预算")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    data = json.loads(OUT.read_text(encoding="utf-8"))
    loops = data.get("loops") or []
    by_id = {x.get("id"): x for x in loops}
    changed = 0
    for rid, (low, high, why) in FIXES.items():
        item = by_id.get(rid)
        if not item:
            print(f"⚠ 没有 {rid}", file=sys.stderr)
            continue
        old = (item.get("budget_low"), item.get("budget_high"))
        if old == (low, high):
            print(f"· {rid} 已是 {low}-{high}，跳过")
            continue
        per_day_old = (old[0] + old[1]) / 2 / item["days"]
        per_day_new = (low + high) / 2 / item["days"]
        print(f"· {rid}：{old[0]}-{old[1]}（{per_day_old:.0f} 元/天）→ {low}-{high}"
              f"（{per_day_new:.0f} 元/天）｜ {why}")
        item["budget_low"], item["budget_high"] = low, high
        changed += 1

    if args.dry_run:
        print("（dry-run：没写文件）")
        return 0
    if not changed:
        return 0
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"✓ 已更新 {changed} 条 → {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
