"""12306 接口探针：验证 leftTicket / queryTrainInfo 的可用性与字段位置。"""
from __future__ import annotations

import datetime as dt
import pprint
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from travel_planner.ingest.client12306 import Client12306  # noqa: E402


def main() -> None:
    c = Client12306()
    raw = c.get("/otn/resources/js/framework/station_name.js", binary=True)
    text = raw.decode("utf-8", "replace")
    payload = text.split("='", 1)[1].rsplit("'", 1)[0]
    print(f"station_name.js: {len(payload)} 字节, 约 {payload.count('@')} 个车站")

    date = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    print(f"探针日期: {date}")

    c.warmup()
    print("warmup(init 页) 完成")

    try:
        resp = c.get("/otn/leftTicket/query", params={
            "leftTicketDTO.train_date": date,
            "leftTicketDTO.from_station": "BJP",
            "leftTicketDTO.to_station": "SHH",
            "purpose_codes": "ADULT",
        })
        data = resp.get("data") or {}
        result = data.get("result") or []
        print(f"leftTicket(北京->上海): {len(result)} 趟")
        for row in result[:2]:
            print("  原始行:", row[:240])
            f = row.split("|")
            print(f"  按文档解析: train_no={f[2]!r} 车次={f[3]!r} "
                  f"from={f[6]!r} to={f[7]!r} dep={f[8]!r} arr={f[9]!r}")

        if result:
            f = result[0].split("|")
            d2 = c.get("/otn/queryTrainInfo/query", params={
                "leftTicketDTO.train_no": f[2],
                "leftTicketDTO.train_date": date,
                "rand_code": "",
            })
            data2 = d2.get("data") or {}
            stops = data2.get("data") if isinstance(data2, dict) else data2
            print(f"queryTrainInfo({f[3]}): {len(stops)} 站")
            if stops:
                print("  首站:", end=" ")
                pprint.pprint(stops[0], width=160)
                print("  末站:", end=" ")
                pprint.pprint(stops[-1], width=160)
    except Exception as e:  # noqa: BLE001
        print("FAIL:", type(e).__name__, e)


if __name__ == "__main__":
    main()
