"""把「环线 Ultra」子代理产出的暂存素材校验、合并成一份正式数据。

输入：``data/route_content/_staging/ultra_loops_*.json``（顶层 {loops: [...]}）
输出：``data/route_content/ultra_loops.json``（合并、按 id 去重、校验后写回）

校验项（都是这类攻略最容易注水/出错的地方）：
* 必填字段齐全、id 唯一、出发地属于允许的集合（北京 / 河北各市）
* 环线形状：`cities` 首尾一致（或明确写了回程路线）
* 天数与 itinerary 条数一致、预算 low<=high、每天要有点名的地方（否则画不出动线）
* 不许出现具体车次号与精确时刻（车次号会误导人，行程里该写「每天多班、以 12306 为准」）

用法：
    python scripts/build_ultra_loops.py --check     # 只校验
    python scripts/build_ultra_loops.py             # 校验并合并（原文件归档）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "data" / "route_content" / "_staging"
ARCHIVE = STAGING / "_archive"
OUT = ROOT / "data" / "route_content" / "ultra_loops.json"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

REQUIRED = ("id", "name", "origin", "days", "budget_low", "budget_high", "cities",
            "summary", "itinerary", "highlights", "save_tips", "transport", "source",
            "confidence")
HEBEI = {"石家庄", "唐山", "保定", "邯郸", "邢台", "张家口", "承德", "沧州", "廊坊",
         "衡水", "秦皇岛", "定州", "辛集"}
ALLOWED_ORIGINS = HEBEI | {"北京"}
#: 小环线 = 省内/周边几站（3-10 天）；大环线 = 跨省沿大动脉一路串下去（≥8 天、≥6 城）
SCALES = {"small", "large"}
LARGE_MIN_DAYS, LARGE_MIN_CITIES = 8, 6
MAX_DAYS = 30          # 大环线可以很长（如京广—沪昆 20 天），上限比原来放开
#: 明确禁止写进素材的东西（会让人以为是查得到的实时信息）
FORBIDDEN = (
    (re.compile(r"[GDCZTK]\d{1,4}次?"), "出现了具体车次号"),
    (re.compile(r"\d{1,2}:\d{2}\s*(发车|开车|到站|抵达)"), "出现了精确时刻"),
)


def load_all() -> tuple[list[dict], list[str]]:
    loops: list[dict] = []
    problems: list[str] = []
    seen: set[str] = set()
    # 先读**已合并**的 ultra_loops.json：这份脚本是「增量合并」，不是「重新生成」。
    # 只读暂存区会把上几批素材直接冲掉（34 条 → 30 条那种事故），所以已收录的必须有位置。
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{OUT.name}: 读不了（{exc}）")
            old = {}
        for item in (old or {}).get("loops") or []:
            entry = dict(item)
            entry["_file"] = OUT.name
            entry["_merged"] = True
            rid = str(entry.get("id") or "")
            if not rid:
                problems.append(f"{OUT.name}: 有一条已收录环线没有 id")
                continue
            seen.add(rid)
            loops.append(entry)

    files = sorted(STAGING.glob("ultra_loops_*.json"))
    # 暂存区空是**正常状态**（都合并归档了）。此时 --check 的作用变成「体检已收录的那批」，
    # 所以只提示、不算问题；否则每次合并完再跑一次都会红。
    if not files:
        print(f"· 暂存区没有新素材（只校验已收录的 {len(loops)} 条）")
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{path.name}: 读不了（{exc}）")
            continue
        items = (raw or {}).get("loops") or []
        if not items:
            problems.append(f"{path.name}: 没有 loops")
        for item in items:
            item = dict(item)
            item["_file"] = path.name
            rid = str(item.get("id") or "")
            if rid in seen:
                problems.append(f"{path.name}: id 重复 {rid}（暂存区不能覆盖已收录的环线）")
                continue
            seen.add(rid)
            loops.append(item)
    return loops, problems


def _blob_for_scan(item: dict) -> str:
    """拼出待扫描文本：摘掉天标签（D1）、来源字段与所有 URL。

    否则会误报：`"day": "D1"` 被当成 D 字头列车，`/article/G0VMSD…html` 这种
    链接尾巴被当成 G0 次（第一版就误报了 34 条）。来源里出现车次号是**出处**，不算误导。
    """
    copy = {k: v for k, v in item.items()
            if k not in ("_file", "source", "source_url")}
    copy["itinerary"] = [{k: v for k, v in (day or {}).items() if k != "day"}
                         for day in item.get("itinerary") or []]
    text = json.dumps(copy, ensure_ascii=False)
    return re.sub(r"https?://\S+", "", text)


def validate(loops: list[dict]) -> list[str]:
    problems: list[str] = []
    for item in loops:
        where = f"{item.get('_file', '?')}:{item.get('id') or '(无 id)'}"
        for field in REQUIRED:
            if item.get(field) in (None, "", []):
                problems.append(f"{where}: 缺 {field}")
        origin = str(item.get("origin") or "")
        if origin and origin not in ALLOWED_ORIGINS:
            problems.append(f"{where}: 出发地 {origin} 不在允许范围（北京 / 河北各市）")
        cities = [str(c) for c in (item.get("cities") or [])]
        if len(cities) >= 3 and cities[0] != cities[-1]:
            text = f"{item.get('summary', '')}{item.get('name', '')}"
            text += "".join(str(x.get("detail") or "") for x in item.get("itinerary") or [])
            if not re.search(r"(回程|原路返回|回来|返程)", text):
                problems.append(f"{where}: 首尾城市不一致，也没写清回程路线")
        days = item.get("days")
        if not isinstance(days, int) or not (3 <= days <= MAX_DAYS):
            problems.append(f"{where}: days={days} 不合理（应在 3-{MAX_DAYS} 之间）")
        itin = item.get("itinerary") or []
        if days and len(itin) != days:
            problems.append(f"{where}: itinerary {len(itin)} 条与 days={days} 不一致")
        # 大小环线：老素材没写 scale 就按小环线算；写了大环线就得真够大
        scale = str(item.get("scale") or "small")
        item["scale"] = scale
        if scale not in SCALES:
            problems.append(f"{where}: scale={scale!r} 应为 small 或 large")
        elif scale == "large":
            uniq = {c for c in cities if c != cities[0]} if cities else set()
            if isinstance(days, int) and days < LARGE_MIN_DAYS:
                problems.append(f"{where}: 大环线只有 {days} 天（要求 ≥{LARGE_MIN_DAYS} 天）")
            if len(uniq) < LARGE_MIN_CITIES - 1:
                problems.append(f"{where}: 大环线只有 {len(uniq)} 个中途城市"
                                f"（要求整条线 ≥{LARGE_MIN_CITIES} 城）")
        low, high = item.get("budget_low"), item.get("budget_high")
        if isinstance(low, (int, float)) and isinstance(high, (int, float)) and low > high:
            problems.append(f"{where}: budget_low({low}) > budget_high({high})")
        if item.get("confidence") not in ("high", "medium", "low"):
            problems.append(f"{where}: confidence 应为 high/medium/low")
        for day in itin:
            detail = str(day.get("detail") or "")
            if len(detail) < 40:
                problems.append(f"{where}: {day.get('day')} 的 detail 太短，写不出可执行安排")
        blob = _blob_for_scan(item)
        for pattern, why in FORBIDDEN:
            hit = pattern.search(blob)
            if hit:
                problems.append(f"{where}: {why}（{hit.group(0)}）——改成「每天多班 / 以 12306 为准」")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="合并校验环线 Ultra 素材")
    ap.add_argument("--check", action="store_true", help="只校验，不写不归档")
    args = ap.parse_args(argv)

    loops, problems = load_all()
    problems += validate(loops)
    if problems:
        print("✗ 校验不通过：", file=sys.stderr)
        for line in problems[:40]:
            print("  -", line, file=sys.stderr)
        return 1
    origins: dict[str, int] = {}
    for item in loops:
        origins[item["origin"]] = origins.get(item["origin"], 0) + 1
    fresh = [i for i in loops if not i.get("_merged")]
    print(f"✓ 校验通过：{len(loops)} 条环线 Ultra"
          f"（已收录 {len(loops) - len(fresh)} + 本次新增 {len(fresh)}）")
    print("  出发地：" + "、".join(f"{k} {v}" for k, v in sorted(origins.items())))
    print(f"  天数：{min(i['days'] for i in loops)}-{max(i['days'] for i in loops)} 天 · "
          f"预算：{min(i['budget_low'] for i in loops)}-{max(i['budget_high'] for i in loops)} 元")
    large = [i for i in loops if i.get("scale") == "large"]
    print(f"  大环线 {len(large)} 条 / 小环线 {len(loops) - len(large)} 条"
          + (f"（大环线 {min(i['days'] for i in large)}-{max(i['days'] for i in large)} 天，"
             f"最长串 {max(len(i['cities']) for i in large)} 城）" if large else ""))
    if args.check:
        return 0
    for item in loops:
        item.pop("_file", None)
        item.pop("_merged", None)
    payload = {
        "updated": dt.date.today().isoformat(),
        "note": ("环线 Ultra：北京出发与河北出发的穷游环线（子代理收集、脚本校验合并）。"
                 "scale=large 是大环线（跨省、≥8 天、≥6 城），small 是省内/周边几站。"
                 "价格为公开攻略区间、不是实时票价；时刻与票价以 12306 / 官方渠道为准；"
                 "每条带 source 与 confidence，低置信度的写明了不确定在哪。"),
        "origins": sorted(origins),
        "loops": sorted(loops, key=lambda x: (str(x.get("origin")), str(x.get("id")))),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"✓ 已写入 {OUT.relative_to(ROOT)}")
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    moved = []
    for path in sorted(STAGING.glob("ultra_loops_*.json")):
        shutil.move(str(path), str(ARCHIVE / path.name))
        moved.append(path.name)
    print(f"✓ 已归档 {len(moved)} 个暂存文件到 {ARCHIVE.relative_to(ROOT)}：{'、'.join(moved)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
