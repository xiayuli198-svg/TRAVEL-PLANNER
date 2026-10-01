"""数一数每个测试文件里有多少条用例（排查「总数突然跳了一大截」这种事）。

用法：python -X utf8 scripts/count_tests.py
"""
from __future__ import annotations

import collections
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def walk(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from walk(item)
        else:
            yield item


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    counts: collections.Counter = collections.Counter()
    names: dict[str, set[str]] = collections.defaultdict(set)
    for case in walk(suite):
        mod = case.__class__.__module__
        counts[mod] += 1
        names[mod].add(case.__class__.__name__)
    total = sum(counts.values())
    print(f"共 {total} 条用例，{len(counts)} 个模块")
    for mod, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4d}  {mod}（{len(names[mod])} 个类）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
