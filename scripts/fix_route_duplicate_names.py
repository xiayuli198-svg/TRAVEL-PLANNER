"""修掉路线库里的一条重名：两条都叫「京沪夜车·睡一觉到上海」。

`collect_evidence.py` 的报告体检顺带查了数据，发现路线库里有一条重名：
`classic_routes.json` 里的 `beijing-shanghai-night`（讲「动卧停运后普速 vs 白天高铁怎么选」）
和后来的 `smart-beijing-shanghai-night`（写死了 Z284 的具体走法）**显示名一模一样**，
在页面上并排出现像重复条目。

两条内容其实互补（一条讲怎么选、一条讲具体怎么走），所以不删，改成各自说清自己是什么。

用法：
    python -X utf8 scripts/fix_route_duplicate_names.py --dry-run
    python -X utf8 scripts/fix_route_duplicate_names.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "data" / "route_content" / "classic_routes.json"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

#: id → 新名字（保持「主题·起终点」形式，校验器要求 name 里必须有「·」）
RENAMES = {
    "beijing-shanghai-night": "京沪夜车怎么选·北京—上海",
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="给重名的路线改名")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    data = json.loads(TARGET.read_text(encoding="utf-8"))
    routes = data.get("routes") or []
    changed = []
    for item in routes:
        rid = str(item.get("id") or "")
        if rid in RENAMES and item.get("name") != RENAMES[rid]:
            changed.append((rid, item.get("name"), RENAMES[rid]))
            item["name"] = RENAMES[rid]
    if not changed:
        print("没有需要改名的条目")
        return 0
    for rid, old, new in changed:
        print(f"· {rid}：「{old}」→「{new}」")
    if args.dry_run:
        print("（dry-run：没写文件）")
        return 0
    TARGET.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"✓ 已写入 {TARGET.relative_to(ROOT)}；下一步：python scripts/build_route_catalog.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
