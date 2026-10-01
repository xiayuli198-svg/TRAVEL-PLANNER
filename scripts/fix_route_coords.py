"""把坐标文件里剩下的疑似查偏项定点修正（一次性）。

脚本 fill_route_coords.py 的自动地理编码对「景点名」会跨省张冠李戴，
已经加了省份包围盒过滤；但**过滤只能丢弃、不能纠正**，
所以对少数关键点在此人工核定坐标（数值取自公开的常见口径）。

用法：python scripts/_fix_coords.py --dry-run / 直接跑
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

P = Path(__file__).resolve().parents[1] / "src" / "travel_planner" / "data" / "city_coords_routes.json"

#: 人工核定：(纬度, 经度, 说明)
FIX = {
    "伊犁": (43.9167, 81.3167, "伊犁州伊宁市（素材写的是州名，落到州府）"),
    "伊宁": (43.9167, 81.3240, "伊宁市（伊犁州首府）"),
    "胡杨林景区": (41.9667, 101.0667, "内蒙古额济纳旗胡杨林"),
    "北极村": (53.4722, 122.3564, "黑龙江漠河北极村"),
    "青石嘴观花台": (37.3667, 101.4833, "青海门源青石嘴观花台"),
    "黄龙九寨": (33.2520, 104.2426, "九寨黄龙机场/九寨沟方向"),
    "莫高窟": (40.0400, 94.8100, "敦煌莫高窟"),
    "篁岭": (29.2520, 117.8620, "江西婺源篁岭"),
    "江岭": (29.1900, 117.9000, "江西婺源江岭"),
    "李坑": (29.2700, 117.9400, "江西婺源李坑"),
    "长白山": (42.0200, 128.0600, "吉林长白山保护开发区"),
    "东兴市": (21.5478, 107.9718, "广西东兴口岸"),
    # 高德对这些景点/小站名直接报 ENGINE_RESPONSE_DATA_ERROR，只能核定
    "亚龙湾": (18.2210, 109.6390, "海南三亚亚龙湾"),
    "鄱阳北": (29.0050, 116.6990, "江西鄱阳北站（昌景黄高铁）"),
    "防川": (42.4400, 130.6100, "吉林珲春防川风景区（一眼望三国）"),
    # 境外/口岸（高德是境内服务，查不到或会查偏）
    "琅勃拉邦": (19.8860, 102.1350, "老挝琅勃拉邦（中老铁路）"),
    "万象": (17.9757, 102.6331, "老挝万象"),
    "河内": (21.0278, 105.8342, "越南河内"),
    "乌兰巴托": (47.8864, 106.9057, "蒙古乌兰巴托"),
    "海参崴": (43.1155, 131.8855, "俄罗斯符拉迪沃斯托克"),
    "符拉迪沃斯托克": (43.1155, 131.8855, "俄罗斯符拉迪沃斯托克"),
    "阿拉木图": (43.2220, 76.8512, "哈萨克斯坦阿拉木图"),
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="定点修正路线坐标")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--prune", action="store_true",
                    help="同时删掉已不再被任何路线引用的条目（保持文件精简）")
    args = ap.parse_args(argv)
    data = json.loads(P.read_text(encoding="utf-8"))
    cities = {str(c.get("name")): c for c in data.get("cities") or []}
    if args.prune:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from travel_planner import routes as routes_mod
        root = Path(__file__).resolve().parents[1] / "data" / "route_content"
        # 注意要把 _staging/_archive 也算进来：正式化之后素材都在归档目录里，
        # 只扫顶层会把仍在用的坐标误判成「未被引用」删掉（踩过一次）。
        used: set[str] = set()
        for base in (root, root / "_staging", root / "_staging" / "_archive",
                     Path(__file__).resolve().parents[1] / "src" / "travel_planner" / "data"):
            for path in base.glob("*.json"):
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                items = raw.get("routes") if isinstance(raw, dict) else None
                if items is None and isinstance(raw, dict):
                    items = [raw] if raw.get("city") or raw.get("from_city") else []
                for item in items or []:
                    for key in ("from_city", "to_city", "city"):
                        used.add(routes_mod._clean_city(item.get(key)))
                    for seg in item.get("segments") or []:
                        for key in ("frm", "to"):
                            used.add(routes_mod._clean_city(seg.get(key)))
        dropped = [name for name in cities if name and name not in used]
        for name in dropped:
            cities.pop(name, None)
        print(f"  清理未被引用的坐标 {len(dropped)} 个")
    changed = []
    for name, (lat, lon, note) in FIX.items():
        cur = cities.get(name)
        if cur and abs(cur.get("lat", 0) - lat) < 0.01 and abs(cur.get("lon", 0) - lon) < 0.01:
            continue
        changed.append((name, cur, lat, lon, note))
        cities[name] = {"name": name, "lat": lat, "lon": lon, "source": "manual", "note": note}
    for name, cur, lat, lon, note in changed:
        old = f"{cur.get('lat')},{cur.get('lon')}" if cur else "（无）"
        print(f"  {name}: {old} → {lat},{lon}   {note}")
    if not changed:
        print("✓ 没有需要修正的项")
        return 0
    if args.dry_run:
        return 0
    data["cities"] = sorted(cities.values(), key=lambda c: str(c.get("name")))
    data["note"] = (data.get("note") or "") + " 少数景点名由人工核定（source=manual）。"
    P.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 已写入 {P.name}：修正 {len(changed)} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
