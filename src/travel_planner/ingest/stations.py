"""车站表数据管道：解析 12306 station_name.js。"""
from __future__ import annotations

from typing import Dict, List

from .client12306 import Client12306

STATION_JS = "/otn/resources/js/framework/station_name.js"


def fetch_station_rows(client: Client12306) -> List[Dict]:
    raw = client.get(STATION_JS, binary=True)
    text = raw.decode("utf-8", errors="replace")
    if "='" not in text:
        raise ValueError("station_name.js 内容格式异常")
    # 形如: var station_names ='@bjb|北京北|VAP|beijingbei|bjb|0|0357|北京|||@bjd|...'
    payload = text.split("='", 1)[1].rsplit("'", 1)[0]
    rows: List[Dict] = []
    for item in payload.split("@"):
        if not item:
            continue
        f = item.split("|")
        if len(f) < 8:
            continue
        rows.append({
            "code": f[2],        # 三字码 VAP
            "name": f[1],        # 站名
            "pinyin": f[3],
            "abbrev": f[4],
            "city": f[7],
            "telecode": f[6],
            "ordinal": int(f[5] or 0),
        })
    return rows
