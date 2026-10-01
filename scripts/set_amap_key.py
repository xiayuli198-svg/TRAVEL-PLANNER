"""换高德 key：一处命令改完**两个地方**，并且先验后用。

高德有两类 key，本项目两处都要用，别只改一处：

| 用在哪 | 存哪 | 干什么 |
|---|---|---|
| 服务端 Web 服务 API | 数据库 `meta.amap_key`（或环境变量 `AMAP_KEY`） | 地理编码、POI 检索、公交中转、驾车路径 |
| 浏览器 Web端(JS API) | `web/globe3d.js` 里的 `AMAP_KEY` 常量 | 地球页签的「高德 3D」按钮 |

用法：
    python -X utf8 scripts/set_amap_key.py <KEY>              # 验证 → 写库 → 写前端 → 破缓存
    python -X utf8 scripts/set_amap_key.py <KEY> --dry-run    # 只验证，不写
    python -X utf8 scripts/set_amap_key.py <KEY> --skip-check # 网络不通时跳过验证（不建议）

注意：**Web 服务 key 与 JS API key 不是一回事**。如果这串是纯 Web 服务 key，
「高德 3D」按钮会在浏览器里报域名/平台错误（10006 / 10009），需要另外建一个
「Web端(JS API)」类型的 key；本脚本会照实写进去并把这个前提提示出来，不会替你假装能测。
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from travel_planner import db                       # noqa: E402
from travel_planner.ingest import amap              # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

GLOBE_JS = ROOT / "web" / "globe3d.js"
INDEX_HTML = ROOT / "web" / "index.html"
KEY_LINE = re.compile(r'(const\s+AMAP_KEY\s*=\s*")([^"]*)(")')


def _load_checker():
    """把 check_amap_key.py 当模块加载，复用它的接口验证（不重复实现一遍）。"""
    spec = importlib.util.spec_from_file_location(
        "check_amap_key", ROOT / "scripts" / "check_amap_key.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                     # type: ignore[union-attr]
    return mod


def verify(key: str) -> bool:
    checker = _load_checker()
    checks = [
        ("地理编码 geocode", "geocode/geo", {"address": "北京南站", "city": "北京"}),
        ("POI 检索 place/text", "place/text",
         {"keywords": "趵突泉", "city": "济南", "offset": 1, "page": 1}),
        ("公交中转 transit", "direction/transit/integrated",
         {"origin": "116.378,39.865", "destination": "116.407,39.904", "city": "北京",
          "strategy": 0}),
        ("驾车路径 driving", "direction/driving",
         {"origin": "116.378,39.865", "destination": "116.407,39.904", "strategy": 0,
          "extensions": "base"}),
    ]
    ok = 0
    for label, path, params in checks:
        good, why = checker.describe(checker.call(path, {**params, "key": key}))
        ok += 1 if good else 0
        print(f"  {'✓' if good else '✗'} {label}：{why}")
    return ok == len(checks)


def update_frontend(key: str, *, dry_run: bool) -> tuple[bool, str]:
    text = GLOBE_JS.read_text(encoding="utf-8")
    m = KEY_LINE.search(text)
    if not m:
        return False, "web/globe3d.js 里没找到 `const AMAP_KEY = \"…\"`"
    old = m.group(2)
    if old == key:
        return True, f"前端已是这个 key（{old[:6]}…{old[-4:]}）"
    if dry_run:
        return True, f"前端将从 {old[:6]}…{old[-4:]} 改为新 key（dry-run 未写）"
    GLOBE_JS.write_text(KEY_LINE.sub(lambda mm: mm.group(1) + key + mm.group(3), text, count=1),
                        encoding="utf-8", newline="")
    return True, f"前端已更新（{old[:6]}…{old[-4:]} → {key[:6]}…{key[-4:]}）"


def bump_assets(*, dry_run: bool) -> str:
    """globe3d.js 换了内容，必须把 index.html 里的 ?v=N 提一档，否则浏览器还用旧缓存。"""
    text = INDEX_HTML.read_text(encoding="utf-8")
    versions = {int(v) for v in re.findall(r"\?v=(\d+)", text)}
    if not versions:
        return "index.html 里没有 ?v= 版本号（跳过破缓存）"
    new_v = max(versions) + 1
    if dry_run:
        return f"?v= 将从 {sorted(versions)} 提到 {new_v}（dry-run 未写）"
    text = re.sub(r"\?v=\d+", f"?v={new_v}", text)
    INDEX_HTML.write_text(text, encoding="utf-8", newline="")
    return f"?v= 已提到 {new_v}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="换高德 key（服务端 + 前端两处）")
    ap.add_argument("key", help="新高德 key")
    ap.add_argument("--dry-run", action="store_true", help="只验证与预览，不写任何文件/库")
    ap.add_argument("--skip-check", action="store_true", help="跳过联网验证（不建议）")
    args = ap.parse_args(argv)

    key = args.key.strip()
    if len(key) < 16:
        print(f"✗ 这串 key 看着不像高德 key（只有 {len(key)} 位）", file=sys.stderr)
        return 2
    print(f"新 key：{key[:6]}…{key[-4:]}（{len(key)} 位）")

    if not args.skip_check:
        print("先验证（用本项目真正会调的 4 个接口）：")
        if not verify(key):
            print("✗ 验证没过：这个 key 在 Web 服务接口上不可用，先别换 —— "
                  "到高德控制台确认 key 类型是「Web服务」、且没有 IP 白名单限制。",
                  file=sys.stderr)
            return 1
    else:
        print("（已跳过联网验证）")

    conn = db.connect()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (amap.KEY_META,)).fetchone()
        old = (row["value"] if row else "") or ""
        print(f"服务端 meta.{amap.KEY_META}：{(old[:6] + '…' + old[-4:]) if old else '(未设置)'}"
              f" → {key[:6]}…{key[-4:]}")
        if not args.dry_run:
            with conn:
                conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (amap.KEY_META, key))
    finally:
        conn.close()

    ok, note = update_frontend(key, dry_run=args.dry_run)
    print("前端：" + note)
    if not ok:
        print("✗ 前端没改成，去 web/globe3d.js 手动改 AMAP_KEY", file=sys.stderr)
        return 1
    print("破缓存：" + bump_assets(dry_run=args.dry_run))

    if args.dry_run:
        print("\n（dry-run：没写库也没写文件）")
        return 0
    print("\n✓ 换好了。要点：")
    print("  · 服务端 key 是每次请求现读 meta，**不用重启服务**；")
    print("  · 页面刷新一下即可（前端文件带了新的 ?v= 版本号）；")
    print("  · 「高德 3D」按钮用的是 Web端(JS API) key，如果它不是这个类型，")
    print("    浏览器里会报 10006/10009 —— 那要去控制台另建一个 JS API key。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
