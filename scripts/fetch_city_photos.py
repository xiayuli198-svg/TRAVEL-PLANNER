"""给城市卡片抓真实照片（白天照 + 夜景照），统一裁切成卡片比例。

数据源（按顺序尝试）：
    1. 360 图片搜索 JSON 接口  https://image.so.com/j?q=...   （国内可达，图源多为国内 CDN）
    2. Bing 图片异步接口（HTML 里提取 murl）作为兜底

每个城市：
    web/img/cities/<城市名>.jpg          白天照片（按城市最代表性的看点搜索）
    web/img/cities/<城市名>@night.jpg    夜景照片（仅「适合晚上游玩」的城市）
    web/img/cities/photos.json           清单（哪些城市有白天/夜景图）
    web/img/cities/credits.json          来源记录（图片地址 + 标题）

用法：
    python scripts/fetch_city_photos.py --only 杭州 苏州 敦煌     # 先试几个
    python scripts/fetch_city_photos.py --limit 20                 # 只做前 20 个城市
    python scripts/fetch_city_photos.py                            # 全部（约 5-10 分钟）
    python scripts/fetch_city_photos.py --force --night-only       # 重抓夜景

想换成自己的照片：直接把图片按上面的文件名丢进 web/img/cities/ 即可，
脚本不会覆盖已存在的文件（除非 --force）。
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

from travel_planner import catalog  # noqa: E402

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

OUT_DIR = PROJECT_ROOT / "web" / "img" / "cities"
CREDITS = OUT_DIR / "credits.json"
MANIFEST = OUT_DIR / "photos.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
CARD_W, CARD_H = 960, 640          # 3:2 横幅，与项目里已有的照片风格一致（卡片与攻略大图都能用）

BAD_TITLE = re.compile(
    r"地图|门票|酒店|民宿|机票|航班|天气|攻略|行程|跟团|报价|价格|多少钱|招聘|广告|"
    r"视频|截图|图表|统计|数据|平面图|户型|卫星|线路图|示意图|图标|logo|矢量|模板|"
    r"wallpaper|hotel|ticket|poster|banner")

# 内容里没写「夜」但公认夜景好的城市，补在这里
NIGHT_EXTRA = {
    "上海", "重庆", "香港", "澳门", "广州", "深圳", "西安", "长沙", "成都", "武汉",
    "南京", "杭州", "苏州", "厦门", "青岛", "哈尔滨", "沈阳", "天津", "济南",
    "南昌", "福州", "南宁", "贵阳", "昆明", "兰州", "乌鲁木齐", "喀什", "西宁",
    "拉萨", "大理", "丽江", "湘西", "桂林", "三亚", "海口", "台北", "高雄",
    "酒泉", "大同", "洛阳", "开封", "晋中", "张家界", "黄山", "西双版纳", "延边",
    "丹东", "大连", "宁波", "温州", "潮州", "汕头", "泉州", "无锡", "扬州", "镇江",
    "绍兴", "嘉兴", "湖州", "景德镇", "上饶", "宜昌", "呼和浩特", "银川", "太原",
    "石家庄", "郑州", "合肥", "佛山", "珠海", "兰州", "乌鲁木齐",
}

_print_lock = threading.Lock()
_credits_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# ---------------------------------------------------------------- 网络
def http_get(url: str, timeout: int = 25, referer: str = "https://image.so.com/") -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": referer, "Accept": "*/*",
    })
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
        out.append({
            "url": img,
            "thumb": item.get("thumb") or "",
            "title": (item.get("title") or "").strip(),
            "w": w, "h": h, "source": "360图片",
        })
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
        if not raw.startswith("http"):
            continue
        out.append({"url": raw, "thumb": "", "title": "", "w": 0, "h": 0, "source": "Bing图片"})
    return out


def candidates(query: str) -> List[dict]:
    items = search_360(query)
    if len(items) < 3:
        items += search_bing(query)
    good = []
    for it in items:
        if BAD_TITLE.search(it["title"] or ""):
            continue
        if it["w"] and it["w"] < 900:
            continue
        if it["w"] and it["h"] and not (0.55 <= it["w"] / it["h"] <= 2.4):
            continue
        good.append(it)
    return good or items


def candidates_multi(queries: List[str], want: int = 6) -> List[dict]:
    """多个关键词依次搜，凑够候选再返回（小城市一次搜不到就换词）。"""
    out: List[dict] = []
    seen = set()
    for q in queries:
        for it in candidates(q):
            key = it["url"]
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
        if len(out) >= want:
            break
    return out


# ---------------------------------------------------------------- 图片处理
def normalize(blob: bytes, min_side: int = 640) -> Optional[bytes]:
    """裁成 3:2 横幅并压缩；失败返回 None。"""
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
    if w / h > target:                      # 太宽 -> 裁两侧
        new_w = int(round(h * target))
        left = (w - new_w) // 2
        im = im.crop((left, 0, left + new_w, h))
    else:                                    # 太高 -> 裁上下（偏上保留天空）
        new_h = int(round(w / target))
        top = int((h - new_h) * 0.34)
        im = im.crop((0, top, w, top + new_h))
    im = im.resize((CARD_W, CARD_H), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=86, optimize=True, progressive=True)
    return buf.getvalue()


def download_image(item: dict) -> Optional[bytes]:
    """先试原图（要求够大），不行再试 360 的缩略图缓存（放宽尺寸要求）。"""
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


def download_image_retry(items: List[dict], tries: int) -> Optional[bytes]:
    for item in items[:tries]:
        blob = download_image(item)
        if blob:
            return blob
        time.sleep(0.2)
    return None


# ---------------------------------------------------------------- 城市画像
def scenic_query(city: dict, night: bool) -> str:
    if night:
        return f"{city['name']} 夜景"
    names = [h["name"] for h in (city.get("highlights") or []) if h.get("kind") != "博物馆"]
    if not names:
        names = [h["name"] for h in (city.get("highlights") or [])]
    landmark = names[0] if names else ""
    return f"{city['name']} {landmark}".strip()


def is_night_city(city: dict) -> bool:
    if city.get("night") is not None:
        return bool(city["night"])
    if city["name"] in NIGHT_EXTRA:
        return True
    text = " ".join([
        " ".join(city.get("tags") or []),
        " ".join((h.get("name") or "") + (h.get("kind") or "") for h in (city.get("highlights") or [])),
        city.get("intro") or "", city.get("halfday") or "", city.get("note") or "",
    ])
    return "夜" in text


# ---------------------------------------------------------------- 抓取
def load_credits() -> Dict:
    if CREDITS.exists():
        try:
            return json.loads(CREDITS.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return {}
    return {}


def save_credits(credits: Dict) -> None:
    # 原子写 + 加锁：多线程并发时不能读到写了一半的文件
    tmp = CREDITS.with_suffix(".tmp")
    tmp.write_text(json.dumps(credits, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(CREDITS)


def record(city_name: str, kind: str, path: Path, items: List[dict]) -> None:
    used = next((i for i in items if i.get("used_url")), {})
    with _credits_lock:
        credits = load_credits()
        credits.setdefault(city_name, {})[kind] = {
            "file": path.name,
            "url": used.get("used_url", ""),
            "title": used.get("title", ""),
            "source": used.get("source", "360图片 / Bing图片"),
        }
        save_credits(credits)


def fetch_city(city: dict, opts) -> Tuple[str, str]:
    name = city["name"]
    day_path = OUT_DIR / f"{name}.jpg"
    night_path = OUT_DIR / f"{name}@night.jpg"
    night_city = is_night_city(city)
    done = []

    if not opts.night_only and (opts.force or not day_path.exists()):
        items = candidates_multi([
            scenic_query(city, False),
            f"{city['name']} 风景",
            f"{city['name']} 景点",
            f"{city['name']} 城市",
        ])
        blob = download_image_retry(items, opts.tries)
        if blob:
            day_path.write_bytes(blob)
            record(name, "day", day_path, items)
            done.append("白天✓")
        else:
            done.append("白天✗")

    if night_city and not opts.day_only and (opts.force or not night_path.exists()):
        items = candidates_multi([
            f"{name} 夜景",
            f"{name} 夜色 灯光",
            f"{name} 夜景 城市灯光",
        ])
        blob = download_image_retry(items, opts.tries)
        if blob:
            night_path.write_bytes(blob)
            record(name, "night", night_path, items)
            done.append("夜景✓")
        else:
            done.append("夜景✗")

    return name, " ".join(done) if done else "已存在（跳过）"


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取城市照片到 web/img/cities/")
    parser.add_argument("--only", nargs="*", help="只抓这些城市")
    parser.add_argument("--limit", type=int, default=0, help="最多抓多少个（0=全部）")
    parser.add_argument("--force", action="store_true", help="已存在的也重抓")
    parser.add_argument("--day-only", action="store_true", help="只抓白天照")
    parser.add_argument("--night-only", action="store_true", help="只抓夜景照")
    parser.add_argument("--workers", type=int, default=5, help="并发数（默认 5，别太高）")
    parser.add_argument("--delay", type=float, default=0.3, help="每个城市之间的间隔秒数")
    parser.add_argument("--tries", type=int, default=5, help="每个城市最多尝试几张候选图")
    args = parser.parse_args()

    cities = catalog.cities()
    if args.only:
        wanted = set(args.only)
        cities = [c for c in cities if c["name"] in wanted]
        missing = wanted - {c["name"] for c in cities}
        if missing:
            print("目录里没有这些城市：" + "、".join(sorted(missing)))
    if args.limit:
        cities = cities[: args.limit]
    if not cities:
        print("没有要处理的城。")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    night_cities = [c["name"] for c in cities if is_night_city(c)]
    print(f"准备处理 {len(cities)} 个城市（其中适合夜景 {len(night_cities)} 个）"
          f" → {OUT_DIR.relative_to(PROJECT_ROOT)}")

    stats = {"both": 0, "day": 0, "night": 0, "skip": 0, "fail": 0}
    fails: List[str] = []

    def worker(city: dict) -> Tuple[str, str]:
        time.sleep(args.delay)
        try:
            return fetch_city(city, args)
        except Exception as exc:                                   # noqa: BLE001
            return city["name"], f"异常 {type(exc).__name__}: {exc}"

    with futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for name, note in pool.map(worker, cities):
            if note.startswith("已存在"):
                stats["skip"] += 1
            elif "白天✓" in note and "夜景✓" in note:
                stats["both"] += 1
                log(f"  ✓✓ {name}: {note}")
            elif "白天✓" in note:
                stats["day"] += 1
                log(f"  ✓  {name}: {note}")
            elif "夜景✓" in note:
                stats["night"] += 1
                log(f"  ✓  {name}: {note}（无白天照）")
            else:
                stats["fail"] += 1
                fails.append(f"{name}（{note.strip()}）")
                log(f"  ✗  {name}: {note}")

    manifest = {}
    for c in catalog.cities():
        day = (OUT_DIR / f"{c['name']}.jpg").exists()
        night = (OUT_DIR / f"{c['name']}@night.jpg").exists()
        if day or night:
            manifest[c["name"]] = {"day": day, "night": night, "night_city": is_night_city(c)}
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    total_day = sum(1 for v in manifest.values() if v["day"])
    total_night = sum(1 for v in manifest.values() if v["night"])
    print(f"\n结果：白天照 {total_day} 张 · 夜景照 {total_night} 张"
          f" · 本次 双图 {stats['both']} / 白天 {stats['day']} / 夜景 {stats['night']}"
          f" / 跳过 {stats['skip']} / 失败 {stats['fail']}")
    if fails:
        print("失败清单（可重跑本脚本补齐）：\n  " + "\n  ".join(fails[:30]))
    print("清单已写入 web/img/cities/photos.json；想自己换图直接覆盖同名文件即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
