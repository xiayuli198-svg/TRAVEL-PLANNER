"""查当前高德 key 的来源与状态（不打印完整 key，只显示前后几位）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from travel_planner import db                      # noqa: E402
from travel_planner.ingest import amap             # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def mask(key: str) -> str:
    key = key or ""
    return f"{key[:6]}…{key[-4:]}（{len(key)} 位）" if len(key) > 12 else (key or "(空)")


def main() -> int:
    env = (os.environ.get("AMAP_KEY") or "").strip()
    print("环境变量 AMAP_KEY:", mask(env) if env else "(未设置)")
    conn = db.connect()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (amap.KEY_META,)).fetchone()
        meta_key = (row["value"] if row else "") or ""
        print("meta 表 amap_key:", mask(meta_key) if meta_key else "(未设置)")
        keys = [r["key"] for r in conn.execute(
            "SELECT key FROM meta WHERE key LIKE '%amap%' OR key LIKE '%key%'")]
        print("相关 meta 键:", keys or "（无）")
        print("运行时 get_key() 取到:", mask(amap.get_key(conn)))
    finally:
        conn.close()

    js = (ROOT / "web" / "globe3d.js").read_text(encoding="utf-8")
    line = next((ln.strip() for ln in js.splitlines() if "AMAP_KEY =" in ln), "")
    print("前端 globe3d.js:", line or "（没找到 AMAP_KEY）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
