"""开发探针：「取巧」能不能把散文攻略自动结构化成带坐标的动线。

思路：不做人工标注，而是拿一份**城市 POI 池**（高德分类检索几页 + 可选 OSM），
然后用名字去城市 guide 的散文里做匹配 ——
    匹配上的点位就有坐标、地址、评分，动线顺序直接沿用散文里的先后，
    匹配不上的就保留纯文字（宁缺勿错）。
本脚本就是量这件事的命中率：几页 POI 能把一座城的攻略覆盖到什么程度。

用法：
    python scripts/dev_probe_guide_match.py 哈尔滨 3      # 城市 页数
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from travel_planner import catalog, db  # noqa: E402
from travel_planner.ingest import amap  # noqa: E402

#: 这些词太泛，出现在散文里也不能当点位（否则「博物馆」会匹配到一堆）
NOISE = {
    "博物馆", "公园", "广场", "古城", "古镇", "寺", "庙", "塔", "山", "湖", "街", "路",
    "机场", "火车站", "高铁站", "地铁站", "景区", "风景区", "游客中心", "步行街", "老街",
    "夜市", "菜市场", "市场", "大桥", "索道", "温泉", "滑雪场", "观景台", "湿地", "草原",
}
#: 归一化时去掉的尾巴（「龙门石窟景区」=「龙门石窟」）
TAILS = ("景区", "风景区", "景点", "博物馆", "纪念馆", "公园", "广场", "步行街",
         "历史文化街区", "街区", "古镇", "古城", "寺", "庙", "宫", "塔", "站")


def norm(name: str) -> str:
    text = re.sub(r"[（(].*?[)）]", "", str(name or "")).strip()
    text = text.replace("·", "").replace(" ", "")
    for tail in TAILS:
        if text.endswith(tail) and len(text) > len(tail) + 1:
            text = text[: -len(tail)]
    return text


def amap_pages(city: str, pages: int, keywords: str = "景点",
               types: str = "110000|140000") -> list[dict]:
    conn = db.connect()
    try:
        key = amap.get_key(conn)
    finally:
        conn.close()
    if not key:
        print("✗ 没有高德 key（数据台可配）")
        return []
    out: list[dict] = []
    for page in range(1, pages + 1):
        params = urllib.parse.urlencode({
            "key": key, "keywords": keywords, "types": types, "city": city,
            "citylimit": "true", "offset": 25, "page": page,
            "extensions": "all", "output": "json",
        })
        url = f"https://restapi.amap.com/v3/place/text?{params}"
        with urllib.request.urlopen(url, timeout=30) as resp:
            raw = json.loads(resp.read())
        pois = raw.get("pois") or []
        print(f"  高德第 {page} 页：{len(pois)} 条（累计 {len(out) + len(pois)}）")
        for item in pois:
            loc = (item.get("location") or "").split(",")
            out.append({
                "name": item.get("name") or "",
                "type": item.get("type") or "",
                "address": item.get("address") or "",
                "lon": float(loc[0]) if len(loc) == 2 else None,
                "lat": float(loc[1]) if len(loc) == 2 else None,
                "rating": ((item.get("biz_ext") or {}) or {}).get("rating") or "",
            })
        if len(pois) < 25:
            break
        time.sleep(0.3)
    return out


def prose_of(city: dict) -> list[tuple[str, str]]:
    """(哪一天, 那天的散文) 列表。"""
    guide = city.get("guide") or {}
    out = []
    for day in guide.get("itinerary") or []:
        out.append((str(day.get("day") or ""), str(day.get("detail") or "")))
    for key, label in (("transport", "交通"), ("stay", "住"), ("eat", "吃")):
        if guide.get(key):
            out.append((label, str(guide[key])))
    return out


def _one_query(city: str, **kw) -> dict:
    conn = db.connect()
    try:
        key = amap.get_key(conn)
    finally:
        conn.close()
    params = urllib.parse.urlencode({"key": key, "city": city, "citylimit": "true",
                                     "offset": 25, "page": 1, "extensions": "all",
                                     "output": "json", **kw})
    with urllib.request.urlopen(
            f"https://restapi.amap.com/v3/place/text?{params}", timeout=30) as resp:
        return json.loads(resp.read())


#: 分类检索的候选类型组（高德的 POI 分类编码）
TYPE_GROUPS = {
    "风景名胜": "110000", "科教文化": "140000", "购物服务": "060000",
    "体育休闲": "080000", "住宿服务": "100000", "餐饮服务": "050000",
    "医疗保健": "090000", "交通设施": "150000",
}


def type_probe(city: str) -> None:
    """看看「按类型分组抓」能不能把步行街/商圈/乐园这些捞进来。

    教训：只抓「风景名胜+科教文化」会漏掉中央大街（购物服务）、冰雪大世界（体育休闲），
    而这两个词在攻略散文里出现得最多 —— 所以漏的不是页数，是类型没选对。
    """
    print("\n-- 类型分组探测（每行 1 次请求）--")
    for label, code in TYPE_GROUPS.items():
        try:
            raw = _one_query(city, keywords="", types=code)
        except Exception as exc:                          # noqa: BLE001
            print(f"  {label}: 失败 {exc}")
            continue
        pois = raw.get("pois") or []
        sample = "、".join((p.get("name") or "")[:12] for p in pois[:5])
        print(f"  {label}({code}): count={raw.get('count')} 本页 {len(pois)} → {sample}")
    print("\n-- 关键词直查（用来补池子里没有的点，1 次/点）--")
    for word in ("中央大街", "冰雪大世界", "防洪纪念塔"):
        try:
            raw = _one_query(city, keywords=word)
        except Exception as exc:                          # noqa: BLE001
            print(f"  {word}: 失败 {exc}")
            continue
        pois = raw.get("pois") or []
        first = pois[0] if pois else {}
        print(f"  {word}: count={raw.get('count')} → "
              f"{(first.get('name') or '（没查到）')} | {(first.get('type') or '')[:24]} | "
              f"{first.get('location') or ''}")


def transit_probe(city: str, pairs: list[tuple[str, str]]) -> None:
    """市内一段到底能不能给出「几号线、在哪上、在哪下」——这决定能不能画市内动线。

    用的是项目里已有的 ``amap.transit_guide``（带 transit_cache），不是新接口：
    它返回的 lines 里就有线路名与上/下车站，正好是市内动线要的东西。
    """
    conn = db.connect()
    try:
        key = amap.get_key(conn)
        if not key:
            print("✗ 没有高德 key")
            return
        print("\n-- 市内公交/地铁段（1 次请求/段，落 transit_cache）--")
        for frm, to in pairs:
            guide = amap.transit_guide(conn, key, frm, city, to, city)
            if not guide:
                print(f"  {frm} → {to}: 查不到")
                continue
            mins = max(1, round(guide["duration_sec"] / 60))
            body = " → ".join(f"{ln['line']} {ln['from']}→{ln['to']}"
                             for ln in guide["lines"]) or "步行"
            print(f"  {frm} → {to}: {body} | 约 {mins} 分钟 · 步行 {guide['walk_m']} 米"
                  + ("（含地铁）" if guide.get("has_subway") else ""))
    finally:
        conn.close()


def main(argv=None) -> int:
    name = argv[1] if len(argv) > 1 else "哈尔滨"
    pages = int(argv[2]) if len(argv) > 2 else 3
    city = catalog.city(name)
    if not city:
        print(f"✗ 城市目录里没有「{name}」")
        return 1
    if "--types" in argv:
        type_probe(name)
        return 0
    if "--transit" in argv:
        transit_probe(name, [("中央大街", "太阳岛风景区"),
                             ("中央大街", "哈尔滨冰雪大世界"),
                             ("哈尔滨站", "圣·索菲亚教堂")])
        return 0
    prose = prose_of(city)
    print(f"== {name}：guide 散文 {len(prose)} 段 ==")
    pois = amap_pages(name, pages)
    pool = [p for p in pois if p["name"] and p["lat"]]
    print(f"  POI 池 {len(pool)} 个（高德 {pages} 页，1 请求/页）")

    # 名字匹配：POI 名出现在散文里就算命中（双向包含，先归一化）
    hits: dict[str, dict] = {}
    for poi in pool:
        key = norm(poi["name"])
        if len(key) < 2 or key in NOISE:
            continue
        for _, text in prose:
            if key and key in text.replace(" ", ""):
                hits[poi["name"]] = poi
                break

    print(f"\n  命中 {len(hits)} 个点位：")
    for item in list(hits.values())[:20]:
        star = f" ★{item['rating']}" if item["rating"] else ""
        print(f"    · {item['name']}{star}  {item['lat']:.4f},{item['lon']:.4f}  "
              f"{(item['type'].split(';')[-1] if item['type'] else '')}")

    # 逐天看：这一天的散文里能对上几个点（决定动线能不能画出来）
    print("\n  逐天命中（≥2 个点才画得出一条线）：")
    for label, text in prose:
        flat = text.replace(" ", "")
        day_hits = [n for n in hits if norm(n) in flat]
        mark = "✓" if len(day_hits) >= 2 else "—"
        print(f"    {mark} {label}: {len(day_hits)} 个 → {'、'.join(day_hits[:6])}")

    # 没命中的「像地名」片段：看看缺口在哪（长度≥3、不在白名单里的中文串）
    print("\n  散文里出现过、但 POI 池里没有的片段（抽样）：")
    found = set()
    for _, text in prose:
        for token in re.findall(r"[\u4e00-\u9fa5]{3,10}", text.replace(" ", "")):
            if token in NOISE or any(norm(n) and norm(n) in token for n in hits):
                continue
            if any(k in token for k in ("小时", "分钟", "建议", "可以", "然后", "如果", "记得")):
                continue
            found.add(token)
    print("    " + "、".join(list(found)[:18]))
    print(f"\n== 结论 ==")
    for label, text in prose:
        flat = text.replace(" ", "")
        n = len([1 for n in hits if norm(n) in flat])
        if n:
            print(f"  用 {len(pool)} 个 POI（{pages} 次请求）就能给「{label}」这天挂上 {n} 个带坐标的点")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
