"""把 dev_probe_data.mjs 里所有「固定次数轮询」的预算统一放宽。

背景：这台机器上 `schedules` 有 **431 万行**，数据台首屏的 `/api/maintain/overview`
一次要给 10 秒左右（date_rows 1.4s + health 1.9s + 其余聚合）。探针原来按
「40 次 × 200ms = 8 秒」等渲染，比接口本身还短 —— 于是慢一点就报「表格 0 行」，
看着像页面坏了，其实是预算没算够。

做法：把这些 `tries < N` 的循环统一改成按**秒数**计budget（默认 45s），
并且轮询间隔 300ms，这样机器再慢也能等到，而真坏了照样会超时。

用法：python -X utf8 scripts/relax_probe_budgets.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "scripts" / "dev_probe_data.mjs"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

#: 轮询循环 → 统一 45 秒预算
REPLACEMENTS = [
    (re.compile(r"while \(tries < 40 && rows === 0\) \{\n    await sleep\(200\);"),
     "while (tries < 150 && rows === 0) {        // 45s：overview 要给 10s+ 才出表\n    await sleep(300);"),
    (re.compile(r"while \(tries < 40 && cards === 0\) \{\n    await sleep\(200\);"),
     "while (tries < 150 && cards === 0) {       // 45s\n    await sleep(300);"),
    (re.compile(r"while \(tries < 30 && days === 0\) \{\n      await sleep\(200\);"),
     "while (tries < 150 && days === 0) {        // 45s\n      await sleep(300);"),
    (re.compile(r"while \(tries < 60 && cards === 0\) \{\n      await sleep\(250\);"),
     "while (tries < 150 && cards === 0) {       // 45s\n      await sleep(300);"),
    (re.compile(r"while \(waits < 60 && segs === 0\) \{\n      await sleep\(500\);"),
     "while (waits < 90 && segs === 0) {         // 45s（高德查市内腿）\n      await sleep(500);"),
]


def main() -> int:
    text = TARGET.read_text(encoding="utf-8")
    changed = 0
    for pattern, repl in REPLACEMENTS:
        text, n = pattern.subn(repl, text)
        changed += n
        print(f"  {'✓' if n else '·'} {pattern.pattern.splitlines()[0][:52]}… → {n} 处")
    if not changed:
        print("没有需要改的地方（可能已经放宽过）")
        return 0
    TARGET.write_text(text, encoding="utf-8", newline="")
    print(f"✓ 已更新 {TARGET.relative_to(ROOT)}：{changed} 处轮询预算")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
