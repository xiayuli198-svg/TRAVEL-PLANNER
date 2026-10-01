"""统一的静态资源破缓存：把 web/index.html 里所有 `?v=` 归一到同一个新号。

为什么要有这个脚本：手工「每个 +1」在版本号已经不一致时会把不一致固化下来
（本项目就出现过 style.css?v=57 与 app.js?v=58 并存），而浏览器是按 URL 缓存的，
哪个文件没提版本，改完就还是旧的。所以规则定成：**取最大值 +1，四个一起写同一个号**。

用法：
    python -X utf8 scripts/bump_assets.py            # 归一 + 提一档
    python -X utf8 scripts/bump_assets.py --check    # 只检查现在是否一致
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "web" / "index.html"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def versions(text: str) -> list[int]:
    return sorted({int(v) for v in re.findall(r"\?v=(\d+)", text)})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="统一 ?v= 版本号")
    ap.add_argument("--check", action="store_true", help="只检查一致性")
    args = ap.parse_args(argv)

    text = INDEX.read_text(encoding="utf-8")
    found = versions(text)
    if not found:
        print("✗ index.html 里没有 ?v= 版本号", file=sys.stderr)
        return 1
    if args.check:
        if len(found) == 1:
            print(f"✓ 版本号一致：?v={found[0]}")
            return 0
        print(f"✗ 版本号不一致：{found} —— 跑 python scripts/bump_assets.py 归一", file=sys.stderr)
        return 1

    new_v = max(found) + 1
    updated = re.sub(r"\?v=\d+", f"?v={new_v}", text)
    INDEX.write_text(updated, encoding="utf-8", newline="")
    print(f"✓ ?v= {found} → {new_v}（{len(re.findall(r'[?]v=' + str(new_v), updated))} 处）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
