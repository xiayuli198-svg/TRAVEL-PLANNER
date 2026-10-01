"""打印报告书里「爬取状态」那一段的纯文本（核对报告有没有如实写出进度）。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "项目报告书.html"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main() -> int:
    if not REPORT.exists():
        print("还没有 项目报告书.html", file=sys.stderr)
        return 1
    text = REPORT.read_text(encoding="utf-8")
    for title in ("爬取状态", "数据规模与口径"):
        m = re.search(r"<h2>" + title + r"</h2>(.*?)(?:</section>|$)", text, re.S)
        if not m:
            print(f"（没找到小节：{title}）")
            continue
        plain = re.sub(r"<[^>]+>", " ", m.group(1))
        plain = re.sub(r"\s+", " ", plain).strip()
        print(f"== {title} ==")
        print(plain[:900])
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
