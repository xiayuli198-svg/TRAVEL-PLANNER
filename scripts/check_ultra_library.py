"""体检已收录的 64 条 Ultra 环线：逐日条数、闭环、重复收口日、日均预算。

子代理在并发迭代时踩过两个坑，这里都盯住：
1. **补足天落到「回出发地那天」之后** → 末尾出现重复的收口日（例如连写两条「武汉→邯郸」）；
2. 为了过测试把预算压到不真实（海南线 14 天压到 314 元/天）—— 测试的上限该按大小环线分开，
   不该反过来改数据，所以这里会把日均超出常见口径的条目点名。

用法：python scripts/check_ultra_library.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "route_content" / "ultra_loops.json"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BACK_HOME = re.compile(r"(回到|返回|返程|回程|回京|回冀|返京)")
TRAIN_CODE = re.compile(r"[GDCZTK]\d{1,4}次?")
CLOCK = re.compile(r"\d{1,2}:\d{2}\s*(发车|开车|到站|抵达)")


def main() -> int:
    data = json.loads(OUT.read_text(encoding="utf-8"))
    loops = data["loops"]
    problems: list[str] = []
    warn: list[str] = []

    ids = [x.get("id") for x in loops]
    dup_ids = [k for k, n in Counter(ids).items() if n > 1]
    if dup_ids:
        problems.append(f"id 重复：{dup_ids}")

    for item in loops:
        rid = item.get("id")
        cities, days = item.get("cities") or [], item.get("days")
        itin = item.get("itinerary") or []
        if cities[:1] != cities[-1:]:
            problems.append(f"{rid}: 首尾城市不一致（不是环线）")
        if cities[:1] != [item.get("origin")]:
            problems.append(f"{rid}: 起点 {cities[:1]} 与出发地 {item.get('origin')} 不一致")
        if len(itin) != days:
            problems.append(f"{rid}: 逐日 {len(itin)} 条 ≠ {days} 天")
        # 重复的收口日：**内容几乎一样**才算错。
        # 注意「返程分两天写」是正常的（D17 桂林→保定在车上发车、D18 抵达保定），
        # 所以判据是同城 + 同标题，或者整条线里出现一模一样的 detail。
        tail = itin[-2:]
        if len(tail) == 2:
            same_city = (tail[0].get("city") or "") == (tail[1].get("city") or "")
            same_title = (tail[0].get("title") or "") == (tail[1].get("title") or "")
            if same_city and same_title and tail[0].get("title"):
                problems.append(f"{rid}: 末尾两条收口日同城同题「{tail[0].get('title')}」，像是补足天写重了")
        details = [str(d.get("detail") or "") for d in itin]
        repeated = [k for k, n in Counter(details).items() if n > 1 and len(k) > 40]
        if repeated:
            problems.append(f"{rid}: 有 {len(repeated)} 段逐日正文完全重复（复制粘贴没改）")
        blob = json.dumps(item, ensure_ascii=False)
        blob = re.sub(r"https?://\S+", "", blob)
        blob = re.sub(r'"day":\s*"D\d+"', "", blob)
        if TRAIN_CODE.search(blob):
            problems.append(f"{rid}: 出现车次号 {TRAIN_CODE.search(blob).group(0)}")
        if CLOCK.search(blob):
            problems.append(f"{rid}: 出现精确时刻 {CLOCK.search(blob).group(0)}")
        low, high = item.get("budget_low") or 0, item.get("budget_high") or 0
        per_day = (low + high) / 2 / days if days else 0
        cap = 480 if item.get("scale") == "large" else 320
        if per_day > cap:
            problems.append(f"{rid}: 日均 {per_day:.0f} 元 超过 {cap}")
        elif item.get("scale") == "large" and per_day < 150:
            warn.append(f"{rid}: 大环线日均只有 {per_day:.0f} 元，跨省长途不太可能，检查是不是为过测试压低了")
        if item.get("scale") == "large" and not item.get("hub_line"):
            problems.append(f"{rid}: 大环线没写 hub_line")

    large = [x for x in loops if x.get("scale") == "large"]
    print(f"共 {len(loops)} 条（大环线 {len(large)} / 小环线 {len(loops) - len(large)}）"
          f" · 出发地 {len({x['origin'] for x in loops})} 个"
          f" · 天数 {min(x['days'] for x in loops)}-{max(x['days'] for x in loops)}")
    if warn:
        print("\n留意：")
        for w in warn:
            print("  ·", w)
    if problems:
        print(f"\n✗ {len(problems)} 个问题：", file=sys.stderr)
        for p in problems[:20]:
            print("  -", p, file=sys.stderr)
        return 1
    print("\n✓ 64 条全部通过：闭环、逐日条数、无重复收口日、无车次号/时刻、预算口径正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
