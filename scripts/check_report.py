"""体检生成出来的报告书：结构、自包含性、有没有把「未通过」写进去。"""
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
        print("✗ 还没有 项目报告书.html（先跑 scripts/build_report.py）", file=sys.stderr)
        return 1
    raw = REPORT.read_bytes()
    text = raw.decode("utf-8")
    problems: list[str] = []

    if raw[:3] == b"\xef\xbb\xbf":
        problems.append("文件带了 BOM（浏览器能读，但和其它页面不一致）")
    for tag in ("<!DOCTYPE html>", "<title>", "</html>"):
        if tag not in text:
            problems.append(f"缺 {tag}")

    externals = re.findall(r'(?:src|href)="(https?://[^"]+)"', text)
    if externals:
        problems.append(f"引用了外部资源（离线打开会缺样式/图）：{externals[:3]}")
    if "<script" in text:
        problems.append("报告里带了脚本 —— 报告应当是纯静态的")

    # 关键小节必须在
    for section in ("结论", "功能地图", "数据规模与口径", "验证结果",
                    "可靠性与诚实性设计", "已知限制与边界", "怎么用"):
        if section not in text:
            problems.append(f"缺小节：{section}")

    # 数字不能是空的/占位的。原始输出块（<pre>）里出现 undefined 属于「探针自己的打印」，
    # 只当提醒 —— 但正文里出现就是报告写坏了。（这条真抓到过一个探针假通过：
    # check_globe 里 pointsData() 是 setter，两边都 undefined 于是断言永远成立。）
    body_only = re.sub(r"<pre>.*?</pre>", "", text, flags=re.S)
    placeholders = [x for x in ("TODO", "None", "undefined", "nan") if x in body_only]
    if placeholders:
        problems.append(f"正文里出现占位内容：{placeholders}")
    raw_hits = sorted({x for x in ("undefined", "NaN") if x in text and x not in body_only})
    if re.search(r'<b>\s*</b>', text):
        problems.append("有空的指标数字")

    # 未通过时必须在结论里说出来（不许粉饰）
    failed = "未通过" in text or "有检查项未通过" in text
    details_cnt = text.count("<details")
    metrics = text.count('class="metric"')
    print(f"文件：{REPORT.name} · {round(len(raw)/1024, 1)} KB · details {details_cnt} 个 · 指标 {metrics} 个")
    print(f"结论区是否含「未通过」字样：{'是' if failed else '否'}")
    if raw_hits:
        print(f"提醒：原始输出块里有 {raw_hits} —— 多半是探针自己的打印，值得回头看看是不是假通过")
    if problems:
        print("✗ 有问题：", file=sys.stderr)
        for p in problems:
            print("  -", p, file=sys.stderr)
        return 1
    print("✓ 报告结构完整、自包含（无外链、无脚本）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
