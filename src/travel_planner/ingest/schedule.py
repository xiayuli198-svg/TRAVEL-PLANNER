"""时刻表数据管道：leftTicket（发现车次）+ queryTrainInfo（整列停站）。"""
from __future__ import annotations

import re
from typing import Dict, List

from .client12306 import Client12306


def query_left_ticket(client: Client12306, date: str,
                      from_code: str, to_code: str) -> List[Dict[str, str]]:
    """查询某日 OD 对的全部车次（仅取车次号与三字码，余票不解析）。"""
    resp = client.get("/otn/leftTicket/query", params={
        "leftTicketDTO.train_date": date,
        "leftTicketDTO.from_station": from_code,
        "leftTicketDTO.to_station": to_code,
        "purpose_codes": "ADULT",
    })
    data = resp.get("data") if isinstance(resp, dict) else None
    if (not isinstance(data, dict) or resp.get("status") is False
            or not isinstance(data.get("result"), list)):
        raise ValueError(f"leftTicket 返回异常: {str(resp)[:200]}")
    result = data["result"]
    trains: List[Dict[str, str]] = []
    for row in result:
        f = row.split("|") if isinstance(row, str) else []
        if len(f) < 10 or not all(f[i] for i in (2, 3, 6, 7)):
            raise ValueError("leftTicket 返回不完整车次，保留 OD 待重试")
        trains.append({
            "train_no": f[2],
            "train_code": f[3],
            "from_code": f[6],
            "to_code": f[7],
            "dep_time": f[8],
            "arr_time": f[9],
        })
    return trains


def query_train_info(client: Client12306, train_no: str, date: str) -> List[Dict]:
    """查询某车次某日完整停站序列。

    返回每站 {seq, station_name, arr, dep, diff}：
    - arr/dep 为 'HH:MM'，始发站到达与终到站出发为 '--'；
    - diff = arrive_day_diff（相对该车次始发日的天偏移，字符串转 int）。
    """
    resp = client.get("/otn/queryTrainInfo/query", params={
        "leftTicketDTO.train_no": train_no,
        "leftTicketDTO.train_date": date,
        "rand_code": "",
    })
    data = resp.get("data") if isinstance(resp, dict) and resp.get("status") is not False else None
    if isinstance(data, dict):
        stops_raw = data.get("data") or []
    elif isinstance(data, list):
        stops_raw = data
    else:
        stops_raw = []
    if not stops_raw:
        raise ValueError(f"queryTrainInfo 无停站数据: {str(resp)[:200]}")

    stops: List[Dict] = []
    for i, item in enumerate(stops_raw, start=1):
        name = (item.get("station_name") or "").strip()
        if not name:
            continue
        arr = _norm_time(item.get("arrive_time"))
        dep = _norm_time(item.get("start_time"))
        diff = _to_int(item.get("arrive_day_diff"))
        stops.append({"seq": i, "station_name": name, "arr": arr,
                      "dep": dep, "diff": diff})
    return stops


def _norm_time(v) -> str:
    if not v or v in ("----", "--", "-"):
        return "--"
    return str(v)[:5]


def _to_int(v) -> int:
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+", v)
        return int(m.group()) if m else 0
    return 0
