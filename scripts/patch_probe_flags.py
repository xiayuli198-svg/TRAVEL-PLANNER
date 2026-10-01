"""给所有浏览器探针的 Edge 启动参数补齐「别节流后台标签页」的那几个开关。

**踩过的坑**：无头页面跑了几分钟之后会被 Chrome 判定为「后台/被遮挡」，于是
`setTimeout` 被节流到 1 次/秒甚至 1 次/分钟、`requestAnimationFrame` 近乎停摆。
表现很迷惑：探针**前面**的断言全过，**最后**几段（平滑滚动 + 轮询等待）开始报
「卡片距顶 13389px」「点了没反应」，甚至 `Runtime.evaluate` 60s 超时 ——
看着像页面坏了，其实是浏览器把计时器掐了。标准做法是加这几个启动开关
（Playwright/Puppeteer 默认都带）：

    --disable-background-timer-throttling
    --disable-backgrounding-occluded-windows
    --disable-renderer-backgrounding
    --disable-features=CalculateNativeWinOcclusion

本脚本**幂等**：先删掉之前插错/插过的同名字符串行，再按括号配对把开关插进
`spawn(EDGE, [ ... ])` 数组里的最后一个元素之前（以前用正则 `[^\\]]*` 匹配，
遇到数组里嵌套的 `]` 就会插到数组外面，把文件写成语法错误 —— 修过一次）。

用法：
    python -X utf8 scripts/patch_probe_flags.py           # 修补（可重复跑）
    python -X utf8 scripts/patch_probe_flags.py --check   # 只检查
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

FLAGS = [
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
]
FLAG_RE = re.compile(r'\s*"--disable-(?:background|renderer|features=Calculate)[^"]*",?')


def patch(path: Path, *, check: bool) -> str:
    text = path.read_text(encoding="utf-8")
    if "spawn(EDGE" not in text:
        return "skip"
    at = text.find("spawn(EDGE")
    lb = text.find("[", at)
    bounds = array_bounds(text, lb)
    if not bounds:
        return "nosplit"
    _, close = bounds
    body = text[lb + 1:close]
    # 先把数组里所有反节流开关（含历史插错位置的）清掉，再规范化重建，保证幂等
    stripped = FLAG_RE.sub("", body).strip()
    stripped = re.sub(r",\s*,", ",", stripped)
    if stripped and not stripped.endswith(","):
        stripped += ","
    flags = "".join(f'\n  "{f}",' for f in FLAGS)
    new_array = "[" + ("\n  " + stripped if stripped else "") + flags + "\n]"
    fixed = text[:lb] + new_array + text[close + 1:]
    if fixed == text:
        return "already"
    if check:
        return "missing"
    path.write_text(fixed, encoding="utf-8", newline="")
    return "patched"


def array_bounds(text: str, start: int) -> tuple[int, int] | None:
    """从 `[` 的位置出发，按括号配对找数组结束的 `]`（忽略字符串里的括号）。"""
    depth = 0
    i = start
    in_str = None
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in "\"'`":
            in_str = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return (start, i)
        i += 1
    return None


def patch_old(path: Path, *, check: bool) -> str:
    text = path.read_text(encoding="utf-8")
    if "spawn(EDGE" not in text:
        return "skip"
    cleaned = FLAG_RE.sub("", text)                        # 幂等：先清掉旧插入
    at = cleaned.find("spawn(EDGE")
    lb = cleaned.find("[", at)
    bounds = array_bounds(cleaned, lb)
    if not bounds:
        return "nosplit"
    _, close = bounds
    insert = "".join(f'\n  "{f}",' for f in FLAGS)
    fixed = cleaned[:close] + insert + cleaned[close:]
    if fixed == text:
        return "already"
    if check:
        return "missing"
    path.write_text(fixed, encoding="utf-8", newline="")
    return "patched"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="给探针补 Edge 反节流开关（幂等）")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)

    results: dict[str, list[str]] = {}
    for path in sorted(SCRIPTS.glob("*.mjs")):
        outcome = patch(path, check=args.check)
        results.setdefault(outcome, []).append(path.name)
    for outcome, names in sorted(results.items()):
        label = {"patched": "已修补", "already": "本来就对", "missing": "缺开关",
                 "skip": "不是探针（跳过）", "nosplit": "找不到 spawn 数组"}[outcome]
        print(f"  {label} {len(names)} 个：{'、'.join(names)}")
    if args.check and results.get("missing"):
        print("✗ 有探针缺反节流开关，跑 python scripts/patch_probe_flags.py 补上", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
