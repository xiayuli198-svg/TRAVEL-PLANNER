"""给所有浏览器探针加上「把无头页面当成聚焦的前台页」。

**真凶记一下**：无头 Chrome 里 `document.visibilityState` 是 `"hidden"`，而 Chrome
在隐藏页面上**不提交滚动** —— `scrollIntoView()` 与 `el.scrollTop = n` 都不会让
`window.scrollY` 变化；同时 `setTimeout` 也会被节流。表现是探针跑到**后半段**才开始
报「点了没反应 / 卡片距顶 13389px / CDP 超时」，前面全过，非常像页面坏了。

修法是 CDP 的 `Emulation.setFocusEmulationEnabled({enabled: true})`
（等价于 DevTools 里的 "Emulate a focused page"）：页面变成 `visible`，滚动立刻正常
（实测点击跳转后 scrollY 由 0 → 2008）。

本脚本**幂等**：已经有这一行的文件跳过。

用法：
    python -X utf8 scripts/patch_probe_focus.py
    python -X utf8 scripts/patch_probe_focus.py --check
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

ANCHOR = 'await cdp.send("Runtime.enable");'
LINE = ('// 无头页面默认 visibilityState="hidden"，Chrome 不提交滚动（scrollIntoView 无效）——\n'
        '// 这个开关等价于 DevTools 的 "Emulate a focused page"，让滚动与计时器正常。\n'
        'try { await cdp.send("Emulation.setFocusEmulationEnabled", { enabled: true }); } catch (e) {}\n')

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="给探针加聚焦模拟（让滚动生效）")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)

    patched, already, missing = [], [], []
    for path in sorted(SCRIPTS.glob("*.mjs")):
        text = path.read_text(encoding="utf-8")
        if ANCHOR not in text:
            continue
        if "setFocusEmulationEnabled" in text:
            already.append(path.name)
            continue
        if args.check:
            missing.append(path.name)
            continue
        text = text.replace(ANCHOR, ANCHOR + "\n" + LINE, 1)
        path.write_text(text, encoding="utf-8", newline="")
        patched.append(path.name)

    if args.check:
        if missing:
            print(f"✗ 缺聚焦模拟的探针 {len(missing)} 个：{'、'.join(missing)}")
            return 1
        print(f"✓ 全部 {len(already)} 个探针都有聚焦模拟")
        return 0
    print(f"✓ 已加 {len(patched)} 个：{'、'.join(patched) or '（无）'}")
    print(f"  本来就有 {len(already)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
