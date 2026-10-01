"""开发探针：维基/维基数据这几个接口在这台机器上「多快会被限流」。

用来给 verify_poi_coords.py 定节奏（别再撞 429）：
* www.wikidata.org  wbsearchentities（名字 → 实体）
* www.wikidata.org  wbgetentities（实体 → P625 坐标，可批量）
* zh.wikipedia.org  list=search（名字 → 条目）
* zh.wikipedia.org  prop=coordinates（条目 → 坐标，可批量 50）

用法：python scripts/dev_probe_apis.py [每个接口试几次] [间隔秒]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

TERMS = ["外滩", "龙门石窟", "中央大街", "日月潭", "九份老街", "莫高窟", "兵马俑", "布达拉宫",
         "鼓浪屿", "西湖", "泰山", "故宫博物院"]
UA = {"User-Agent": "travel-planner-coord-check/1.0 (personal research; contact: local-user)"}


def call(url: str, timeout: int = 25) -> tuple:
    """返回 (状态码, 说明)。"""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return resp.status, "返回不是 JSON"
        if data.get("error"):
            return resp.status, "API 错误：" + str(data["error"].get("code"))
        return resp.status, f"{len(body)} 字节"
    except urllib.error.HTTPError as exc:                 # noqa: PERF203
        retry = exc.headers.get("Retry-After") if exc.headers else None
        return exc.code, f"HTTP {exc.code}" + (f"（Retry-After={retry}）" if retry else "")
    except Exception as exc:                              # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def endpoints(term: str) -> list:
    enc = urllib.parse.quote(term)
    return [
        ("wikidata 搜索", "https://www.wikidata.org/w/api.php?action=wbsearchentities"
                        f"&search={enc}&language=zh&uselang=zh&format=json&limit=5"),
        ("wikidata 搜索(带 origin)", "https://www.wikidata.org/w/api.php?action=wbsearchentities"
                                 f"&search={enc}&language=zh&format=json&limit=5&origin=*"),
        ("wikipedia 搜索", "https://zh.wikipedia.org/w/api.php?action=query&list=search"
                         f"&srsearch={enc}&srlimit=3&format=json"),
        ("wikipedia 坐标", "https://zh.wikipedia.org/w/api.php?action=query&prop=coordinates"
                         f"&titles={enc}&format=json"),
    ]


def main(argv=None) -> int:
    rounds = int(argv[1]) if len(argv) > 1 else 4
    delay = float(argv[2]) if len(argv) > 2 else 0.8
    print(f"每个接口试 {rounds} 次，间隔 {delay}s")
    stats: dict = {}
    for i in range(rounds):
        term = TERMS[i % len(TERMS)]
        for label, url in endpoints(term):
            code, note = call(url)
            bucket = stats.setdefault(label, {"ok": 0, "fail": 0, "codes": set()})
            if code == 200:
                bucket["ok"] += 1
            else:
                bucket["fail"] += 1
                bucket["codes"].add(str(code))
            print(f"  [{i + 1}/{rounds}] {label:22s} {term:6s} → {code} {note}")
            time.sleep(delay)
    print("\n== 汇总 ==")
    for label, bucket in stats.items():
        print(f"  {label:22s} 成功 {bucket['ok']} · 失败 {bucket['fail']}"
              + (f" · 状态码 {sorted(bucket['codes'])}" if bucket["codes"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
