"""跑一遍全部验证，把结果写成 `data/report_evidence.json`（报告书的数据来源）。

报告书里的「287 项测试通过 / 9 个浏览器探针全绿」这类数字**必须是跑出来的**，
不能手写 —— 手写的数字过两周就没人知道还算不算数。所以：

    python -X utf8 scripts/collect_evidence.py            # 全跑（约 6-10 分钟）
    python -X utf8 scripts/collect_evidence.py --quick    # 跳过浏览器探针（约 1 分钟）

然后 `python scripts/build_report.py` 会读这份 JSON 生成 `项目报告书.html`。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "report_evidence.json"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

#: 浏览器探针（真跑 Chromium，不需要人工看）
PROBES = [
    "check_plan.mjs", "check_ultra.mjs", "check_map.mjs", "check_globe.mjs",
    "check_globe_env.mjs", "dev_probe.mjs", "dev_probe_cards.mjs",
    "dev_probe_data.mjs", "check_contrast.mjs",
]

#: 数据体检（纯离线，不联网、不写库）
DATA_CHECKS = [
    ("Ultra 环线体检", [sys.executable, "-X", "utf8", "scripts/check_ultra_library.py"]),
    ("Ultra 库校验", [sys.executable, "-X", "utf8", "scripts/build_ultra_loops.py", "--check"]),
    ("路线库校验", [sys.executable, "-X", "utf8", "scripts/build_route_catalog.py", "--check"]),
    ("世界环线校验", [sys.executable, "-X", "utf8", "scripts/extend_world_loops.py", "--dry-run"]),
    ("世界城市攻略校验", [sys.executable, "-X", "utf8", "scripts/extend_world_guides.py", "--dry-run"]),
    ("数据体检", [sys.executable, "-X", "utf8", "scripts/data_health.py"]),
]


def run(cmd: list[str], timeout: int = 1800) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              shell=False)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"（超时 {timeout}s）"
    except FileNotFoundError as exc:
        return 127, f"（找不到命令：{exc}）"


def tail(text: str, lines: int = 14) -> str:
    keep = [ln for ln in text.strip().splitlines() if ln.strip()]
    return "\n".join(keep[-lines:])


def parse_tests(out: str) -> dict:
    ran = re.search(r"^Ran (\d+) tests?", out, re.M)
    failed = re.search(r"^FAILED \((.*)\)", out, re.M)
    errors = re.findall(r"^(?:FAIL|ERROR): (\S+)", out, re.M)
    dur = re.search(r"in ([\d.]+)s", out)
    return {
        "ran": int(ran.group(1)) if ran else 0,
        "ok": bool(ran) and not failed,
        "failed_detail": failed.group(1) if failed else "",
        "failures": errors[:10],
        "seconds": float(dur.group(1)) if dur else None,
        "tail": tail(out, 8),
    }


def parse_probe(name: str, code: int, out: str) -> dict:
    passed = len(re.findall(r"^\s*✓", out, re.M))
    failed = len(re.findall(r"^\s*✗", out, re.M))
    summary = ""
    for line in reversed(out.strip().splitlines()):
        if "✓ 全部" in line or "✗" in line and "项" in line:
            summary = line.strip()
            break
    return {"name": name, "ok": code == 0 and failed == 0, "exit": code,
            "passed": passed, "failed": failed, "summary": summary,
            "tail": tail(out, 10)}


def parse_check(name: str, code: int, out: str) -> dict:
    ok = code == 0 and "✗" not in out
    return {"name": name, "ok": ok, "exit": code, "tail": tail(out, 8)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="跑全部验证并写证据文件")
    ap.add_argument("--quick", action="store_true", help="跳过浏览器探针")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args(argv)

    print("跑单元测试 …")
    code, out = run([sys.executable, "-X", "utf8", "-m", "unittest", "discover",
                     "-s", "tests", "-t", "."], timeout=1800)
    tests = parse_tests(out)
    print(f"  {tests['ran']} 项 · {'OK' if tests['ok'] else 'FAILED'}")

    probes = []
    if not args.quick:
        for name in PROBES:
            print(f"跑浏览器探针 {name} …")
            pcode, pout = run(["node", str(ROOT / "scripts" / name)], timeout=1800)
            item = parse_probe(name, pcode, pout)
            probes.append(item)
            print(f"  {'✓' if item['ok'] else '✗'} {item['passed']} 项断言"
                  + (f" · {item['summary']}" if item["summary"] else ""))

    checks = []
    for name, cmd in DATA_CHECKS:
        print(f"跑数据体检 {name} …")
        ccode, cout = run(cmd, timeout=600)
        item = parse_check(name, ccode, cout)
        checks.append(item)
        print(f"  {'✓' if item['ok'] else '✗'}")

    node_ver = ""
    try:
        _c, nout = run(["node", "--version"], timeout=60)
        node_ver = nout.strip()
    except Exception:                     # noqa: BLE001
        pass

    evidence = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "quick": bool(args.quick),
        "host": {
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.release()}",
            "node": node_ver,
            "cwd": str(ROOT),
        },
        "tests": tests,
        "probes": probes,
        "data_checks": checks,
        "totals": {
            "probes_total": len(probes),
            "probes_ok": sum(1 for p in probes if p["ok"]),
            "probe_assertions": sum(p["passed"] for p in probes),
            "probe_failures": sum(p["failed"] for p in probes),
            "checks_total": len(checks),
            "checks_ok": sum(1 for c in checks if c["ok"]),
            "all_ok": tests["ok"] and all(p["ok"] for p in probes)
            and all(c["ok"] for c in checks),
        },
    }
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")
    t = evidence["totals"]
    print(f"\n{'✓ 全部通过' if t['all_ok'] else '✗ 有失败项'}："
          f"测试 {tests['ran']} 项 · 探针 {t['probes_ok']}/{t['probes_total']}"
          f"（{t['probe_assertions']} 条断言）· 数据体检 {t['checks_ok']}/{t['checks_total']}")
    print(f"已写入 {path.relative_to(ROOT)}")
    return 0 if t["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
