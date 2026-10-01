"""地图漫游（roam）测试：城市目录完整性、攻略与照片覆盖、停留映射、成环规划装配。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from travel_planner import catalog, db, drive, roam, service, tourism

CHINA_LON = (73.0, 135.5)
CHINA_LAT = (3.0, 54.0)


class TestPlanGuides(unittest.TestCase):
    """攻略与查询的联动：任何一次查询都要把沿途城市的资料带出来。

    之前只有「查询恰好命中攻略」才显示，等于大半场景看不到攻略；
    现在从规划结果里摊出城市序列（起点/中转/终点），逐城挂卡片。
    """

    @staticmethod
    def _plan(legs):
        return {"ok": True, "journeys": [{"legs": legs}]}

    def test_roles_and_layover_from_journey(self):
        plan = self._plan([
            {"from": "北京南", "to": "南京南", "dep_min": 480, "arr_min": 720},
            {"from": "南京南", "to": "上海虹桥", "dep_min": 780, "arr_min": 840},
        ])
        cities = roam.cities_of_plan(plan)
        self.assertEqual([c["name"] for c in cities], ["北京", "南京", "上海"],
                         "站名要归到城市：北京南→北京")
        roles = {c["name"]: c["role"] for c in cities}
        self.assertEqual(roles["北京"], "起点")
        self.assertEqual(roles["南京"], "中转")
        self.assertEqual(roles["上海"], "终点")
        self.assertEqual(cities[1]["layover_min"], 60, "换乘等待按下一段发车算")

    def test_layover_hint_levels(self):
        card = {"hours": 6}
        self.assertIn("别出站", roam.layover_hint(card, 90))
        self.assertIn("就近走一走", roam.layover_hint(card, 180))
        # 5 小时等待、这座城建议玩 6 小时 —— 够就近走，但不够走完整条线（要说实话）
        self.assertIn("另留时间", roam.layover_hint(card, 300))
        # 城市本身只要 4 小时就能逛完时，5 小时等待就该按半日线走
        self.assertIn("半日线", roam.layover_hint({"hours": 4}, 300))
        self.assertEqual(roam.layover_hint(card, None), "", "没有停留时长就不给建议")

    def test_cards_carry_role_hint_and_highlights(self):
        plan = self._plan([{"from": "北京", "to": "上海", "dep_min": 480, "arr_min": 760}])
        data = roam.plan_guides(plan)
        self.assertEqual(len(data["cities"]), 2)
        first = data["cities"][0]
        self.assertEqual(first["name"], "北京")
        self.assertEqual(first["role"], "起点")
        self.assertTrue(first["action_hint"], "要告诉人这座城建议留多久")
        self.assertTrue(first["halfday"], "半日安排要带出来")
        self.assertIn("攻略", data["note"])

    def test_repeated_city_keeps_the_transfer_info(self):
        """往返同一座城时，换乘等待要记在**换乘的那座城**上。"""
        plan = self._plan([
            {"from": "北京", "to": "天津", "dep_min": 480, "arr_min": 540},
            {"from": "天津", "to": "北京", "dep_min": 600, "arr_min": 660},
        ])
        cities = {c["name"]: c for c in roam.cities_of_plan(plan)}
        self.assertEqual(list(cities), ["北京", "天津"])
        self.assertEqual(cities["天津"]["role"], "中转")
        self.assertEqual(cities["天津"]["layover_min"], 60)

    def test_loop_shape_and_limit(self):
        loop = {"ok": True, "legs": [{"frm": "北京", "to": "南京", "role": "途经"},
                                     {"frm": "南京", "to": "杭州", "role": "途经"}]}
        names = [c["name"] for c in roam.cities_of_plan(loop)]
        self.assertEqual(names, ["北京", "南京", "杭州"])
        self.assertEqual(len(roam.cities_of_plan(loop, limit=2)), 2)

    def test_unknown_places_are_skipped(self):
        plan = self._plan([{"from": "黑马河", "to": "火星营地", "dep_min": 480, "arr_min": 600}])
        self.assertEqual(roam.cities_of_plan(plan), [])

    def test_station_index_from_db(self):
        """有数据库时按站名表归一（与规划引擎同一份口径）。"""
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "t.db")
            try:
                with conn:
                    conn.execute("INSERT INTO stations(code,name,city,ordinal) "
                                 "VALUES('VNP','北京南','北京',0)")
                plan = self._plan([{"from": "北京南", "to": "北京",
                                    "dep_min": 480, "arr_min": 520}])
                names = [c["name"] for c in roam.cities_of_plan(plan, conn=conn)]
                self.assertEqual(names, ["北京"])
            finally:
                conn.close()


class TestCarClass(unittest.TestCase):
    """租车车型：踩过「选商务型和 SUV 一个价」的坑 —— 前端写「商务型」，
    费用模型里的键叫「商务」，不归一就静默回退到 SUV，看起来像没实现。"""

    def test_web_value_is_normalised(self):
        self.assertEqual(drive.normalize_car_class("商务型", ["SUV", "商务", "经济型"])[0], "商务")
        self.assertEqual(drive.normalize_car_class("SUV", ["SUV", "商务"])[0], "SUV")
        self.assertEqual(drive.normalize_car_class("mpv", ["SUV", "商务"])[0], "商务")
        self.assertEqual(drive.normalize_car_class("经济", ["SUV", "经济型"])[0], "经济型")

    def test_unknown_class_falls_back_and_says_so(self):
        car, note = drive.normalize_car_class("房车", ["SUV", "商务"])
        self.assertEqual(car, "SUV")
        self.assertTrue(note, "换了车型必须说明，否则又变成「选了没反应」")

    def test_classes_have_different_prices(self):
        cities = [{"name": "北京"}, {"name": "天津"}]
        summary = {"total_days": 3, "fuel": 600, "tolls": 200, "total_km": 800}
        quotes = {cls: drive.rental_quote(cities, summary, car_class=cls)
                  for cls in ("经济型", "SUV", "商务")}
        daily = {cls: (q["daily_low"], q["daily_high"]) for cls, q in quotes.items()}
        self.assertEqual(len(set(daily.values())), 3, f"三种车型应当三个价：{daily}")
        self.assertLess(daily["经济型"][0], daily["SUV"][0])
        self.assertLess(daily["SUV"][0], daily["商务"][0])

    def test_frontend_alias_lands_on_business_tier(self):
        cities = [{"name": "北京"}, {"name": "天津"}]
        summary = {"total_days": 3, "fuel": 600, "tolls": 200, "total_km": 800}
        alias = drive.rental_quote(cities, summary, car_class="商务型")
        suv = drive.rental_quote(cities, summary, car_class="SUV")
        self.assertEqual(alias["car_class"], "商务")
        self.assertEqual(alias["car_class_requested"], "商务型")
        self.assertNotEqual(alias["daily_low"], suv["daily_low"])

    def test_rates_also_normalise(self):
        rows = drive.city_rental_rates(["北京"], car_class="商务型")
        self.assertEqual(rows[0]["car_class"], "商务")
        self.assertEqual(len(rows[0]["car_options"]), 3)


class TestCityGuides(unittest.TestCase):
    """完整旅游攻略（每座城市一条多日攻略）与照片覆盖。"""

    def test_every_city_has_a_complete_guide(self):
        cities = catalog.cities()
        missing = [c["name"] for c in cities if not c.get("guide")]
        self.assertEqual(missing, [], f"这些城市缺攻略：{missing[:10]}")
        for item in cities:
            guide = item["guide"]
            days = guide.get("days")
            self.assertIsInstance(days, int, f"{item['name']} 的 days 不是整数")
            self.assertTrue(1 <= days <= 6, f"{item['name']} 的 days 越界：{days}")
            itinerary = guide.get("itinerary") or []
            self.assertEqual(len(itinerary), days,
                             f"{item['name']} 行程条数({len(itinerary)})与天数({days})不符")
            for day in itinerary:
                self.assertTrue(day.get("day") and day.get("detail"),
                                f"{item['name']} 某天缺 day/detail")
                self.assertGreaterEqual(len(day["detail"]), 30, f"{item['name']} 的行程太短")
            for key in ("summary", "transport", "stay", "eat", "budget"):
                self.assertTrue(guide.get(key), f"{item['name']} 缺 {key}")
            self.assertTrue(guide.get("tips"), f"{item['name']} 缺 tips")

    def test_night_cities_are_flagged(self):
        night = [c for c in catalog.cities() if c.get("night")]
        self.assertGreaterEqual(len(night), 60, "标记为夜游的城市太少")
        for name in ("上海", "重庆", "香港", "西安"):
            item = catalog.city(name)
            self.assertTrue(item and item.get("night"), f"{name} 应该被标记为夜游城市")

    def test_photos_cover_all_cities(self):
        manifest_path = Path(catalog.DATA_DIR).parents[2] / "web" / "img" / "cities" / "photos.json"
        if not manifest_path.exists():
            self.skipTest("还没有抓过照片（web/img/cities/photos.json 不存在）")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        no_day = [c["name"] for c in catalog.cities() if not (manifest.get(c["name"]) or {}).get("day")]
        self.assertEqual(no_day, [], f"这些城市缺白天照：{no_day[:10]}")
        for item in catalog.cities():
            entry = manifest.get(item["name"]) or {}
            if item.get("night"):
                self.assertTrue(entry.get("night"), f"夜游城市 {item['name']} 缺夜景照")

    def test_city_detail_payload(self):
        detail = roam.city_detail("杭州")
        self.assertTrue(detail["ok"])
        self.assertTrue(detail["city"]["has_guide"])
        self.assertGreaterEqual(len(detail["guide"].get("itinerary") or []), 1)
        self.assertTrue(detail["city"]["photo_day"] or detail["city"]["photo"])
        self.assertFalse(roam.city_detail("不存在的城市")["ok"])


class TestDriveMode(unittest.TestCase):
    """自驾 / 租车：环线自驾资料、车程装配、租车费用估算。"""

    def test_drive_presets_cover_every_loop(self):
        loops = catalog.loops()
        self.assertGreaterEqual(len(loops), 20)
        missing = [l["id"] for l in loops if not drive.loop_drive(l["id"])]
        self.assertEqual(missing, [], f"这些环线缺自驾资料：{missing}")
        for loop in loops:
            d = drive.loop_drive(loop["id"])
            self.assertGreaterEqual(d.get("km", 0), 50, loop["id"])
            self.assertTrue(1 <= d.get("days", 0) <= 30, loop["id"])
            self.assertTrue(d.get("roads"), f"{loop['id']} 缺公路信息")
            self.assertTrue(d.get("season"), f"{loop['id']} 缺适合季节")
            self.assertTrue(d.get("warnings"), f"{loop['id']} 缺提醒")
            rental = d.get("rental") or {}
            for key in ("pickup", "dropoff", "car", "note"):
                self.assertTrue(rental.get(key), f"{loop['id']} 租车缺 {key}")
            self.assertTrue(catalog.city(rental["pickup"]),
                            f"{loop['id']} 的取车城市 {rental['pickup']} 不在城市目录里")
            self.assertTrue(catalog.city(rental["dropoff"]),
                            f"{loop['id']} 的还车城市 {rental['dropoff']} 不在城市目录里")

    def test_rental_model_and_tiers(self):
        model = drive.rental_model()
        for tier in ("A", "B", "C", "D"):
            self.assertIn(tier, model["tiers"])
            for cls in ("经济型", "SUV", "商务"):
                low, high = model["tiers"][tier][cls]
                self.assertLess(low, high, f"{tier}/{cls}")
        self.assertGreaterEqual(len(model["platforms"]), 4)
        for p in model["platforms"]:
            self.assertTrue(p.get("name") and p.get("url", "").startswith("http"))
        # A 档（一线/热门）应当比 C 档（普通地级市）贵
        self.assertGreater(model["tiers"]["A"]["SUV"][0], model["tiers"]["C"]["SUV"][0])
        self.assertEqual(drive.city_tier("北京"), "A")
        self.assertEqual(drive.city_tier("阿里"), "D")
        self.assertEqual(drive.city_tier("雅安"), "C")
        # 省会/热门城市属于 A 或 B（具体分档允许按旺季行情微调）
        self.assertIn(drive.city_tier("杭州"), {"A", "B"})
        self.assertIn(drive.city_tier("成都"), {"A", "B"})

    def test_rental_quote_breakdown(self):
        cities = [catalog.city("成都"), catalog.city("乐山")]
        summary = {"total_days": 5, "total_km": 320, "tolls": 130, "fuel": 200}
        q = drive.rental_quote(cities, summary, car_class="SUV")
        self.assertTrue(q["ok"])
        self.assertEqual(q["pickup"], "成都")
        self.assertEqual(q["days"], 5)
        # 取还车城市不同 → 记异地还车费
        self.assertTrue(q["one_way"])
        self.assertGreater(q["one_way_fee"], 0)
        self.assertLess(q["total_low"], q["total_high"])
        self.assertEqual(q["tolls"], 130)
        self.assertGreaterEqual(q["total_low"], q["rent_only_low"] + 200)
        # 同一城市取还 → 无异地费，总价更低
        same = drive.rental_quote([catalog.city("成都")], summary, car_class="SUV")
        self.assertFalse(same["one_way"])
        self.assertEqual(same["one_way_fee"], 0)
        self.assertLess(same["total_low"], q["total_low"])
        # 拉萨（A/D 档）比乐山贵
        far = drive.rental_quote([catalog.city("拉萨")], summary, car_class="SUV")
        self.assertGreater(far["daily_low"], 100)

    def test_plan_drive_without_network(self):
        """把 route() 换成固定值，验证行程装配（不联网）。"""
        real_route = drive.route
        calls = []

        def fake_route(conn, a, b, refresh=False):
            calls.append((a, b))
            return {"ok": True, "source": "测试", "distance_km": 150,
                    "duration_min": 120, "tolls": 60.0, "note": ""}

        drive.route = fake_route
        try:
            res = drive.plan_drive(None, "2026-06-17", ["成都", "乐山", "宜宾"],
                                   stays=[{"mode": "nights", "nights": 2},
                                          {"mode": "nights", "nights": 1}],
                                   with_rental=True)
        finally:
            drive.route = real_route
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["plan_order"], ["成都", "乐山", "宜宾", "成都"])
        self.assertEqual(len(res["legs"]), 3)
        self.assertEqual([c for c in calls], [("成都", "乐山"), ("乐山", "宜宾"), ("宜宾", "成都")])
        s = res["summary"]
        self.assertEqual(s["total_km"], 450)
        self.assertEqual(s["tolls"], 180)
        self.assertEqual(s["drive_hours"], 6.0)
        self.assertFalse(s["estimated"])
        self.assertEqual(res["legs"][0]["drive"]["fuel"],
                         round(150 * drive.rental_model()["consumption_per_100km"] / 100
                               * drive.rental_model()["fuel_price"]))
        self.assertIn("rental", res)
        self.assertTrue(res["rental"]["pickup"] == "成都")
        # 闭环 → 起点还车，不该收异地还车费
        self.assertFalse(res["rental"]["one_way"])
        self.assertEqual(res["rental"]["one_way_fee"], 0)

    def test_estimate_fallback_has_note(self):
        est = drive.estimate_route("成都", "拉萨")
        self.assertTrue(est["ok"])
        self.assertEqual(est["source"], "估算")
        self.assertGreater(est["distance_km"], 1000)
        self.assertTrue(est["note"])

    def test_drive_minutes_adds_breaks(self):
        self.assertEqual(drive.drive_minutes(60)["break"], 0)
        self.assertEqual(drive.drive_minutes(150)["break"], 15)
        self.assertEqual(drive.drive_minutes(300)["break"], 30)


class TestCatalogIntegrity(unittest.TestCase):
    def test_cities_are_complete_and_geolocated(self):
        cities = catalog.cities()
        self.assertGreaterEqual(len(cities), 200, "城市目录应覆盖 200 座以上城市")
        names = [c["name"] for c in cities]
        self.assertEqual(len(names), len(set(names)), "城市名不应重复")
        region_names = {r["name"] for r in catalog.regions()}
        self.assertGreaterEqual(len(region_names), 9)
        for item in cities:
            self.assertTrue(item["region"], f"{item['name']} 没有归属大区")
            self.assertIn(item["region"], region_names)
            self.assertIsNotNone(item["lon"], f"{item['name']} 缺经度")
            self.assertIsNotNone(item["lat"], f"{item['name']} 缺纬度")
            self.assertTrue(CHINA_LON[0] <= item["lon"] <= CHINA_LON[1], item["name"])
            self.assertTrue(CHINA_LAT[0] <= item["lat"] <= CHINA_LAT[1], item["name"])
            self.assertTrue(item["intro"], f"{item['name']} 缺简介")
            self.assertGreaterEqual(len(item["highlights"]), 1, f"{item['name']} 缺看点")
            for h in item["highlights"]:
                self.assertIn(h["kind"], catalog.HIGHLIGHT_KINDS,
                              f"{item['name']} 看点类型非法：{h['kind']}")
            self.assertTrue(item["plan_name"], f"{item['name']} 缺规划点")
            self.assertTrue(6.0 <= float(item["score"]) <= 9.7, item["name"])

    def test_every_region_has_cities(self):
        for region in catalog.regions():
            items = catalog.cities(region["id"])
            self.assertGreaterEqual(len(items), 5, f"{region['name']} 城市太少")

    def test_loop_presets_reference_known_cities(self):
        loops = catalog.loops()
        self.assertGreaterEqual(len(loops), 10, "经典环线应有 10 条以上")
        for item in loops:
            self.assertEqual(item["missing"], [], f"{item['name']} 引用了不存在的城市")
            self.assertGreaterEqual(len(item["stops"]), 2)
            self.assertIsNotNone(item["bounds"], item["name"])
            self.assertIn(item["plan_mode"], ("rail", "reference"))
            for stop in item["stops"]:
                self.assertIsNotNone(stop["lon"], stop["name"])

    def test_cross_region_loop_detected(self):
        cross = [x["name"] for x in catalog.loops() if x["cross_region"]]
        self.assertTrue(cross, "应至少有一条跨大区环线（视角需要多移动一次）")


class TestRoamHelpers(unittest.TestCase):
    def test_auto_stay_follows_suggested_days(self):
        self.assertEqual(roam.auto_stay({"days": 0.5})["mode"], "halfday")
        self.assertEqual(roam.auto_stay({"days": 1})["nights"], 1)
        self.assertEqual(roam.auto_stay({"days": 2})["nights"], 2)
        self.assertEqual(roam.auto_stay({"days": 4})["nights"], 3)

    def test_map_payload_shape(self):
        payload = roam.map_payload()
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["cities"]), len(catalog.cities()))
        self.assertEqual(len(payload["regions"]), len(catalog.regions()))
        card = payload["cities"][0]
        for key in ("name", "lon", "lat", "score", "halfday", "highlights", "rail", "plan_name"):
            self.assertIn(key, card)

    def test_near_route_finds_city_between_stops(self):
        # 上海 → 杭州 之间，嘉兴应该被判定为顺路可加
        selected = [catalog.city("上海"), catalog.city("杭州")]
        near = roam.near_route(selected, catalog.cities(), limit=8, max_km=120)
        names = [x["name"] for x in near]
        self.assertIn("嘉兴", names)
        self.assertTrue(all(x["km"] <= 120 for x in near))


class TestRoamPlan(unittest.TestCase):
    """用假的引擎结果验证装配逻辑（不需要真实的全国时刻表）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_env = os.environ.get("TRAVEL_PLANNER_TOURISM_DB")
        os.environ["TRAVEL_PLANNER_TOURISM_DB"] = str(Path(self.tmp.name) / "tourism.db")
        self.calls = []
        self._real_do_loop = service.do_loop

        def fake_do_loop(conn, date, stops, stays, **kwargs):
            self.calls.append({"stops": list(stops),
                               "stays": [dict(s) for s in stays],
                               "date": date})
            legs = []
            for i in range(len(stops) - 1):
                legs.append({
                    "day_no": i + 1,
                    "date": date,
                    "frm": stops[i],
                    "to": stops[i + 1],
                    "stay": None,
                    "error": "",
                    "journey": {
                        "dep_min": 480, "arr_min": 660, "price": 55.0,
                        "price_text": "约¥55", "rides": 1,
                        "dep_text": "08:00", "arr_text": "11:00",
                        "duration_text": "3小时", "legs": [],
                    },
                })
            return {"ok": True, "error": None, "date": date, "mode": kwargs.get("mode"),
                    "order": None, "legs": legs, "total_days": len(legs),
                    "city_count": len(stops), "warnings": []}

        service.do_loop = fake_do_loop

    def tearDown(self):
        service.do_loop = self._real_do_loop
        if self._old_env is None:
            os.environ.pop("TRAVEL_PLANNER_TOURISM_DB", None)
        else:
            os.environ["TRAVEL_PLANNER_TOURISM_DB"] = self._old_env
        self.tmp.cleanup()

    def test_closed_loop_rotates_stays(self):
        res = roam.plan(None, "2026-06-17", ["杭州", "绍兴", "宁波"],
                        stays=[{"mode": "nights", "nights": 1},
                               {"mode": "halfday"},
                               {"mode": "nights", "nights": 2}])
        self.assertTrue(res["ok"], res.get("error"))
        call = self.calls[0]
        self.assertEqual(call["stops"], ["杭州", "绍兴", "宁波", "杭州"])
        # stays[i] 是「到达 stops[i+1] 之后」的停留，所以是每个城市自己的停留整体左移：
        self.assertEqual(call["stays"][0]["mode"], "halfday")   # 到绍兴 → 玩半天
        self.assertEqual(call["stays"][1]["nights"], 2)         # 到宁波 → 住 2 晚
        self.assertEqual(call["stays"][2]["nights"], 1)         # 回到杭州（终点，不再出发）
        self.assertEqual(res["order_cities"], ["杭州", "绍兴", "宁波"])

    def test_open_journey_drops_start_stay(self):
        res = roam.plan(None, "2026-06-17", ["昆明", "大理", "丽江"], closed=False,
                        stays=[{"mode": "transit"}, {"mode": "nights", "nights": 2},
                               {"mode": "nights", "nights": 1}])
        self.assertTrue(res["ok"], res.get("error"))
        call = self.calls[0]
        self.assertEqual(call["stops"], ["昆明", "大理", "丽江"])
        self.assertEqual(len(call["stays"]), 2)
        self.assertEqual(call["stays"][0]["nights"], 2)   # 大理
        self.assertEqual(call["stays"][1]["nights"], 1)   # 丽江

    def test_plan_attaches_city_guides_and_summary(self):
        res = roam.plan(None, "2026-06-17", ["杭州", "绍兴"])
        self.assertTrue(res["ok"], res.get("error"))
        self.assertIn("杭州", res["guides"])
        self.assertTrue(res["guides"]["杭州"]["halfday"])
        self.assertEqual(res["summary"]["city_count"], 2)
        self.assertEqual(res["summary"]["play_hours"] > 0, True)
        self.assertEqual(res["summary"]["travel_hours"], 6.0)   # 2 段 × 3 小时
        self.assertEqual(res["summary"]["price"], 110.0)
        for leg in res["legs"]:
            self.assertIn("city_tourism", leg)
        self.assertIn("near_route", res["suggestions"])

    def test_unknown_city_rejected(self):
        res = roam.plan(None, "2026-06-17", ["杭州", "不存在的城市"])
        self.assertFalse(res["ok"])
        self.assertIn("不存在的城市", res["error"])

    def test_single_city_rejected(self):
        res = roam.plan(None, "2026-06-17", ["杭州"])
        self.assertFalse(res["ok"])

    def test_plan_uses_seat_city_for_prefectures(self):
        # 黔东南 没有同名车站，规划时用驻地凯里
        res = roam.plan(None, "2026-06-17", ["贵阳", "黔东南"])
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(self.calls[0]["stops"], ["贵阳", "凯里", "贵阳"])


class TestTourismRegion(unittest.TestCase):
    def test_region_query_and_highlights(self):
        with tempfile.TemporaryDirectory() as d:
            conn = tourism.connect(Path(d) / "tourism.db")
            jiangnan = tourism.in_region(conn, "江南")
            self.assertGreaterEqual(len(jiangnan), 20)
            hangzhou = tourism.get(conn, "杭州")
            self.assertEqual(hangzhou["region"], "江南")
            self.assertTrue(any(h["kind"] == "博物馆" for h in hangzhou["highlights"]))
            counts = tourism.counts(conn)
            self.assertEqual(sum(counts.values()), len(tourism.list_for(conn)))
            conn.close()


if __name__ == "__main__":
    unittest.main()
