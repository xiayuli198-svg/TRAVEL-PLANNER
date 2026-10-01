"""给地球模式抓照片：世界城市（白天/夜景）+ 世界之最。

和国内城市那套（scripts/fetch_city_photos.py）同一套逻辑与图源：
    1. 360 图片搜索 JSON 接口（国内可达）
    2. Bing 图片异步接口兜底
统一裁成 960×640、JPEG q86。

产出：
    web/img/world/<城市名>.jpg            白天
    web/img/world/<城市名>@night.jpg      夜景（标记为夜游的城市）
    web/img/world/record_<记录id>.jpg     世界之最
    web/img/world/photos.json / credits.json

用法：
    python scripts/fetch_world_photos.py --only 东京 巴黎 珠穆朗玛峰
    python scripts/fetch_world_photos.py --limit 10
    python scripts/fetch_world_photos.py                 # 全量（几十张，几分钟）
    python scripts/fetch_world_photos.py --records-only
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import io
import json
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from travel_planner import worldmap  # noqa: E402

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

OUT_DIR = PROJECT_ROOT / "web" / "img" / "world"
CREDITS = OUT_DIR / "credits.json"
MANIFEST = OUT_DIR / "photos.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
CARD_W, CARD_H = 960, 640

BAD_TITLE = re.compile(
    r"地图|门票|酒店|民宿|机票|航班|天气|攻略|行程|跟团|报价|价格|多少钱|招聘|广告|"
    r"视频|截图|图表|统计|数据|平面图|户型|卫星|线路图|示意图|图标|logo|矢量|模板|"
    r"wallpaper|hotel|ticket|poster|banner|flag|map")

_lock = threading.Lock()
_credits_lock = threading.Lock()


def log(msg: str) -> None:
    with _lock:
        print(msg, flush=True)


def http_get(url: str, timeout: int = 25, referer: str = "https://image.so.com/") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def search_360(query: str, count: int = 12) -> List[dict]:
    url = f"https://image.so.com/j?q={urllib.parse.quote(query)}&src=srp&pn={count}"
    try:
        data = json.loads(http_get(url).decode("utf-8", "ignore"))
    except Exception:
        return []
    out = []
    for item in data.get("list") or []:
        img = item.get("img") or item.get("thumb")
        if not img:
            continue
        try:
            w, h = int(item.get("width") or 0), int(item.get("height") or 0)
        except (TypeError, ValueError):
            w = h = 0
        out.append({"url": img, "thumb": item.get("thumb") or "",
                    "title": (item.get("title") or "").strip(),
                    "w": w, "h": h, "source": "360图片"})
    return out


def search_bing(query: str, count: int = 20) -> List[dict]:
    url = ("https://cn.bing.com/images/async?q=" + urllib.parse.quote(query) +
           f"&first=1&count={count}&mmasync=1")
    try:
        html = http_get(url, referer="https://cn.bing.com/").decode("utf-8", "ignore")
    except Exception:
        return []
    out = []
    for m in re.finditer(r'murl&quot;:&quot;(.*?)&quot;', html):
        raw = m.group(1).replace("\\/", "/")
        if raw.startswith("http"):
            out.append({"url": raw, "thumb": "", "title": "", "w": 0, "h": 0, "source": "Bing图片"})
    return out


def candidates_multi(queries: List[str], want: int = 6) -> List[dict]:
    out: List[dict] = []
    seen = set()
    for q in queries:
        items = search_360(q)
        if len(items) < 3:
            items += search_bing(q)
        for it in items:
            if BAD_TITLE.search(it["title"] or ""):
                continue
            if it["w"] and it["w"] < 900:
                continue
            if it["w"] and it["h"] and not (0.55 <= it["w"] / it["h"] <= 2.4):
                continue
            if it["url"] in seen:
                continue
            seen.add(it["url"])
            out.append(it)
        if len(out) >= want:
            break
    return out


def normalize(blob: bytes, min_side: int = 640) -> Optional[bytes]:
    if Image is None:
        return blob if len(blob) > 15_000 else None
    try:
        im = Image.open(io.BytesIO(blob))
        im.load()
    except Exception:
        return None
    if min(im.width, im.height) < min_side:
        return None
    im = im.convert("RGB")
    target = CARD_W / CARD_H
    w, h = im.size
    if w / h > target:
        new_w = int(round(h * target))
        left = (w - new_w) // 2
        im = im.crop((left, 0, left + new_w, h))
    else:
        new_h = int(round(w / target))
        top = int((h - new_h) * 0.34)
        im = im.crop((0, top, w, top + new_h))
    im = im.resize((CARD_W, CARD_H), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=86, optimize=True, progressive=True)
    return buf.getvalue()


def download_image(item: dict) -> Optional[bytes]:
    for url, min_side in ((item.get("url"), 700), (item.get("thumb"), 420)):
        if not url:
            continue
        try:
            blob = http_get(url, timeout=30)
        except Exception:
            continue
        if not blob or len(blob) < 12_000 or len(blob) > 12_000_000:
            continue
        head = blob[:4]
        if head[:2] != b"\xff\xd8" and head[:4] != b"\x89PNG" and blob[:4] != b"RIFF":
            continue
        out = normalize(blob, min_side=min_side)
        if out:
            item["used_url"] = url
            return out
    return None


def download_retry(items: List[dict], tries: int) -> Optional[bytes]:
    for item in items[:tries]:
        blob = download_image(item)
        if blob:
            return blob
        time.sleep(0.2)
    return None


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return default


def save_credits(entry: Dict) -> None:
    with _credits_lock:
        data = load_json(CREDITS, {})
        data.update(entry)
        tmp = CREDITS.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(CREDITS)


def city_queries(city: dict, night: bool) -> List[str]:
    name = city["name"]
    en = city.get("name_en") or ""
    if night:
        return [f"{name} 夜景", f"{name} 夜景 城市灯光", f"{en} night skyline"] if en else [f"{name} 夜景"]
    scenic = [h["name"] for h in (city.get("highlights") or []) if h.get("kind") != "博物馆"]
    landmark = scenic[0] if scenic else ""
    qs = [f"{name} {landmark}".strip(), f"{name} 风景", f"{name} 城市"]
    if en:
        qs.append(f"{en} city")
    return qs


def record_queries(rec: dict) -> List[str]:
    name = rec["name"]
    return [name, f"{name} 风景", f"{rec.get('country', '')} {name}".strip()]


def fetch_city(city: dict, opts) -> Tuple[str, str]:
    name = city["name"]
    day_path = OUT_DIR / f"{name}.jpg"
    night_path = OUT_DIR / f"{name}@night.jpg"
    night_city = bool(city.get("night"))
    done = []
    credits = {}
    if not opts.night_only and (opts.force or not day_path.exists()):
        items = candidates_multi(city_queries(city, False))
        blob = download_retry(items, opts.tries)
        if blob:
            day_path.write_bytes(blob)
            used = next((i for i in items if i.get("used_url")), {})
            credits[name] = dict(credits.get(name) or {}, day={
                "file": day_path.name, "url": used.get("used_url", ""),
                "title": used.get("title", ""), "source": used.get("source", "")})
            done.append("白天✓")
        else:
            done.append("白天✗")
    if night_city and not opts.day_only and (opts.force or not night_path.exists()):
        items = candidates_multi(city_queries(city, True))
        blob = download_retry(items, opts.tries)
        if blob:
            night_path.write_bytes(blob)
            used = next((i for i in items if i.get("used_url")), {})
            credits[name] = dict(credits.get(name) or {}, night={
                "file": night_path.name, "url": used.get("used_url", ""),
                "title": used.get("title", ""), "source": used.get("source", "")})
            done.append("夜景✓")
        else:
            done.append("夜景✗")
    if credits:
        save_credits(credits)
    return name, " ".join(done) if done else "已存在（跳过）"


def fetch_record(rec: dict, opts) -> Tuple[str, str]:
    rid = rec.get("id") or rec["name"]
    path = OUT_DIR / f"record_{rid}.jpg"
    if not opts.force and path.exists():
        return rec["name"], "已存在（跳过）"
    items = candidates_multi(record_queries(rec))
    blob = download_retry(items, opts.tries)
    if not blob:
        return rec["name"], "✗"
    path.write_bytes(blob)
    used = next((i for i in items if i.get("used_url")), {})
    save_credits({rid: {"record": {"file": path.name, "url": used.get("used_url", ""),
                                   "title": used.get("title", ""), "source": used.get("source", "")}}})
    return rec["name"], "✓"


def write_manifest() -> dict:
    manifest = {}
    for c in worldmap.cities():
        day = (OUT_DIR / f"{c['name']}.jpg").exists()
        night = (OUT_DIR / f"{c['name']}@night.jpg").exists()
        if day or night:
            manifest[c["name"]] = {"day": day, "night": night, "night_city": bool(c.get("night"))}
    for r in worldmap.records():
        rid = r.get("id") or r.get("name")
        if (OUT_DIR / f"record_{rid}.jpg").exists():
            manifest[rid] = {"record": True, "name": r["name"]}
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取世界城市与世界之最的照片")
    parser.add_argument("--only", nargs="*", help="只抓这些城市/世界之最")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--day-only", action="store_true")
    parser.add_argument("--night-only", action="store_true")
    parser.add_argument("--records-only", action="store_true", help="只抓世界之最")
    parser.add_argument("--cities-only", action="store_true", help="只抓城市")
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--delay", type=float, default=0.3)
    parser.add_argument("--tries", type=int, default=6)
    args = parser.parse_args()

    cities = worldmap.cities()
    records = worldmap.records()
    if args.only:
        wanted = set(args.only)
        cities = [c for c in cities if c["name"] in wanted]
        records = [r for r in records if r["name"] in wanted or r.get("id") in wanted]
        if not cities and not records:
            print("清单里没有这些名字：" + "、".join(sorted(wanted)))
            return 1
    if args.records_only:
        cities = []
    if args.cities_only:
        records = []
    if args.limit:
        cities = cities[: args.limit]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if not worldmap.cities():
        print("world_catalog.json 还没生成，先跑 python scripts/build_world_catalog.py", file=sys.stderr)
        return 1

    print(f"准备处理 {len(cities)} 座城市 + {len(records)} 条世界之最 → {OUT_DIR.relative_to(PROJECT_ROOT)}")

    def worker_city(c):
        time.sleep(args.delay)
        try:
            return fetch_city(c, args)
        except Exception as exc:                                  # noqa: BLE001
            return c["name"], f"异常 {type(exc).__name__}: {exc}"

    def worker_record(r):
        time.sleep(args.delay)
        try:
            return fetch_record(r, args)
        except Exception as exc:                                  # noqa: BLE001
            return r["name"], f"异常 {type(exc).__name__}: {exc}"

    fails: List[str] = []
    ok = skip = 0
    with futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for name, note in pool.map(worker_city, cities) if cities else []:
            if note.startswith("已存在"):
                skip += 1
            elif "✗" in note:
                fails.append(f"{name}（{note.strip()}）")
                log(f"  ✗  {name}: {note}")
            else:
                ok += 1
                log(f"  ✓  {name}: {note}")
        if records:
            log("— 世界之最 —")
        for name, note in pool.map(worker_record, records) if records else []:
            if note.startswith("已存在"):
                skip += 1
            elif "✓" in note:
                ok += 1
                log(f"  ✓  {name}")
            else:
                fails.append(name)
                log(f"  ✗  {name}")

    manifest = write_manifest()
    total_city = sum(1 for v in manifest.values() if v.get("day"))
    total_night = sum(1 for v in manifest.values() if v.get("night"))
    total_record = sum(1 for v in manifest.values() if v.get("record"))
    print(f"\n结果：城市白天照 {total_city} · 城市夜景照 {total_night} · 世界之最 {total_record}"
          f" · 本次成功 {ok} / 跳过 {skip} / 失败 {len(fails)}")
    if fails:
        print("失败清单（可重跑补齐）：\n  " + "\n  ".join(fails[:30]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
