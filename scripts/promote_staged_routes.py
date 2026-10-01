"""把暂存区（子代理产出）正式化成一个正式素材文件，并归档原始文件。

流程是「先审后收」：
1. 子代理只写 ``data/route_content/_staging/*.json``；
2. ``build_route_catalog.py --check --include-staging`` 校验（缺来源/缺坐标/巧思高分没写理由都会拦）；
3. **本脚本**把审过的内容合并成 ``data/route_content/smart_routes.json``（正式素材），
   原始文件移到 ``data/route_content/_staging/_archive/`` 留底；
4. 再跑 ``build_route_catalog.py``（不带 --include-staging）写入运行时目录。

用法：
    python scripts/promote_staged_routes.py --dry-run     # 只报告会合并什么
    python scripts/promote_staged_routes.py               # 合并 + 归档
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

STAGING = ROOT / "data" / "route_content" / "_staging"
ARCHIVE = STAGING / "_archive"
TARGET = ROOT / "data" / "route_content" / "smart_routes.json"


def load_staged() -> tuple[list[dict], list[str]]:
    routes: list[dict] = []
    collectors: list[str] = []
    seen: set[str] = set()
    for path in sorted(STAGING.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  ! 跳过 {path.name}: {exc}", file=sys.stderr)
            continue
        if not isinstance(raw, dict) or "routes" not in raw:
            # 子代理/手工留下的临时草稿（比如一个数组）不该把整条流程卡住
            print(f"  · 跳过 {path.name}（不是素材文件，没有 routes 字段）")
            continue
        collector = str(raw.get("collector") or path.stem)
        collectors.append(collector)
        for item in raw.get("routes") or []:
            rid = str(item.get("id") or "")
            if not rid or rid in seen:
                print(f"  ! 跳过重复/无 id 的条目：{rid or '(无)'}", file=sys.stderr)
                continue
            seen.add(rid)
            item["collected_by"] = collector
            item["collected_at"] = str(raw.get("updated") or "")
            item.pop("_file", None)
            item.pop("_origin", None)
            routes.append(item)
    return routes, collectors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把暂存区路线正式化")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写不搬")
    ap.add_argument("--keep-staging", action="store_true", help="合并后保留暂存区原文件（不归档）")
    args = ap.parse_args(argv)

    if not STAGING.exists():
        print("没有暂存区，无需处理")
        return 0
    routes, collectors = load_staged()
    if not routes:
        print("暂存区里没有可合并的路线")
        return 0
    print(f"待正式化：{len(routes)} 条（来自 {len(collectors)} 个来源）")
    for name in collectors:
        print("  ·", name)
    cats: dict[str, int] = {}
    for item in routes:
        cats[item.get("category") or "其他"] = cats.get(item.get("category") or "其他", 0) + 1
    print("  分类：", "、".join(f"{k} {v}" for k, v in cats.items()))
    if args.dry_run:
        return 0

    # 合并进正式文件（若已存在则按 id 覆盖，便于二次收集）
    existing: dict[str, dict] = {}
    if TARGET.exists():
        try:
            old = json.loads(TARGET.read_text(encoding="utf-8"))
            for item in old.get("routes") or []:
                existing[str(item.get("id"))] = item
        except (OSError, json.JSONDecodeError):
            pass
    added = 0
    for item in routes:
        if str(item.get("id")) not in existing:
            added += 1
        existing[str(item.get("id"))] = item
    data = {
        "updated": dt.date.today().isoformat(),
        "note": ("子代理收集的巧思路线（顺路多玩 / 夜车 / 窗口期 / 同价升级 / 本地经验 / 错峰环线）。"
                 "价格是公开攻略与经验区间、不是实时票价，时刻与票价以 12306 / 航司 / 官方渠道为准；"
                 "每条都带 source 与 confidence，低置信度的写明了「以实际查询为准」。"),
        "collectors": collectors,
        "routes": sorted(existing.values(), key=lambda r: str(r.get("id"))),
    }
    TARGET.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 写入 {TARGET.relative_to(ROOT)}：共 {len(data['routes'])} 条（本次新增 {added} 条）")

    if not args.keep_staging:
        ARCHIVE.mkdir(parents=True, exist_ok=True)
        moved, kept = [], []
        for path in sorted(STAGING.glob("*.json")):
            raw = None
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
            # 只归档**本次真正合并过的素材**：子代理的临时草稿（没有 routes 字段，
            # 或者 `_` 开头的中间文件）留在原地 —— 曾经把别人正在写的文件搬走，
            # 差点让一个还在跑的子代理写不回去。
            if not isinstance(raw, dict) or "routes" not in raw or path.name.startswith("_"):
                kept.append(path.name)
                continue
            shutil.move(str(path), str(ARCHIVE / path.name))
            moved.append(path.name)
        print(f"✓ 已归档 {len(moved)} 个暂存文件到 {ARCHIVE.relative_to(ROOT)}")
        if kept:
            print(f"  · 留在暂存区（不是素材文件）：{'、'.join(kept)}")
    print("下一步：python scripts/build_route_catalog.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
