"""路线库（routes）单测：加载、校验、打分、筛选、按城市命中。

重点：
1. ``data/route_content/*.json`` 必须有 ``build_route_catalog.py`` 能通过的内容
   （缺来源、缺坐标、时间对不上都要拦下来）。
2. 性价比分要能区分「又便宜又不慢」与「又贵又慢」。
3. 按城市/主题查询要真的命中，不能全返回来。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from travel_planner import routes as R

ROOT = Path(__file__).resolve().parents[1]
CONTENT = ROOT / "data" / "route_content"


class TestCatalog(unittest.TestCase):
    def test_catalog_exists_and_has_routes(self):
        items = R.routes()
        self.assertGreaterEqual(len(items), 5, "小样至少要有 5 条路线")
        ids = [r["id"] for r in items]
        self.assertEqual(len(ids), len(set(ids)), "id 不能重复")

    def test_every_route_is_attributed(self):
        for item in R.routes():
            self.assertTrue(item.get("source"), item["id"])
            self.assertTrue(item.get("summary"), item["id"])
            self.assertTrue(item.get("segments"), item["id"])
            self.assertIn(item.get("confidence"), ("high", "medium", "low"), item["id"])
            self.assertLessEqual(item.get("cost_low"), item.get("cost_high"), item["id"])

    def test_every_route_has_coords_for_map(self):
        for item in R.routes():
            for key in ("from_city", "to_city"):
                self.assertIsNotNone(item.get(f"{key}_lat"),
                                     f"{item['id']} 的 {key}={item.get(key)} 没有坐标")
                self.assertIsNotNone(item.get(f"{key}_lon"), item["id"])

    def test_value_and_comfort_scores(self):
        for item in R.routes():
            self.assertTrue(0 <= item["value_score"] <= 100, item["id"])
            self.assertTrue(0 <= item["comfort_score"] <= 100, item["id"])


class TestCleverScore(unittest.TestCase):
    """巧思分：与省钱/舒适各自独立，专门救「多花点钱但顺路多玩」那类走法。"""

    def test_tags_are_whitelisted(self):
        for tag, weight in R.CLEVER_WEIGHTS.items():
            self.assertGreater(weight, 0, tag)

    def test_extra_stops_dominates(self):
        plain = {"total_hours": 40, "cost_low": 1100}
        clever = {**plain, "clever_tags": ["extra_stops"]}
        self.assertEqual(R.clever_score(plain), 0)
        self.assertGreaterEqual(R.clever_score(clever), 30)

    def test_unknown_tag_scores_zero(self):
        self.assertEqual(R.clever_score({"clever_tags": ["我随口编的"]}), 0)

    def test_extra_capped(self):
        self.assertLessEqual(
            R.clever_score({"clever_tags": ["extra_stops"], "clever_extra": 999}), 100)
        self.assertLessEqual(
            R.clever_score({"clever_tags": list(R.CLEVER_WEIGHTS), "clever_extra": 40}), 100)

    def test_clever_is_independent_of_value(self):
        """同一条北京→拉萨：绕行多玩更贵更久（性价比更低），但巧思更高 —— 两个维度不该同步。"""
        direct = {"total_hours": 40, "cost_low": 720, "comfort_score": 78}
        detour = {"total_hours": 96, "cost_low": 1500, "comfort_score": 70,
                  "clever_tags": ["extra_stops", "detour_worth"]}
        self.assertLess(R.value_score(detour), R.value_score(direct), "绕行更贵更久，性价比该更低")
        self.assertGreater(R.clever_score(detour), R.clever_score(direct), "绕行更巧，巧思该更高")

    def test_smart_sort_and_filter(self):
        items = [{"id": "a", "value_score": 90, "clever_score": 0},
                 {"id": "b", "value_score": 50, "clever_score": 80},
                 {"id": "c", "value_score": 60, "clever_score": 70}]
        self.assertEqual([x["id"] for x in R.sort_routes(items, "clever")], ["b", "c", "a"])
        self.assertEqual([x["id"] for x in R.sort_routes(items, "value")], ["a", "c", "b"])

    def test_city_name_normalised(self):
        """收集来的写法五花八门，加载时要归一，否则按城市筛选会漏。"""
        self.assertEqual(R._clean_city("西宁（环湖）"), "西宁")
        self.assertEqual(R._clean_city("宏村/汤口"), "宏村")
        self.assertEqual(R._clean_city("北京站"), "北京")
        self.assertEqual(R._clean_city(" 拉萨 "), "拉萨")


class TestScoring(unittest.TestCase):
    def test_cheap_and_fast_scores_higher(self):
        cheap = {"total_hours": 10, "cost_low": 100}
        pricey = {"total_hours": 10, "cost_low": 900}
        self.assertGreater(R.value_score(cheap), R.value_score(pricey))

    def test_slow_expensive_is_low(self):
        """1200 元 / 40 小时 = 30 元每小时，属于「不快也不省」，不该拿高分。"""
        score = R.value_score({"total_hours": 40, "cost_low": 1200})
        self.assertLess(score, 70, f"慢且贵却得了 {score} 分")

    def test_high_speed_rail_is_not_punished(self):
        """京沪高铁 550 元 / 4.5 小时 ≈ 120 元每小时：是「花钱买时间」，不该被打到很低。"""
        score = R.value_score({"total_hours": 4.5, "cost_low": 550, "comfort_score": 85})
        self.assertTrue(45 <= score <= 75, f"高铁得到 {score} 分，偏低")

    def test_sleeper_train_is_high(self):
        """普速硬卧 300 元 / 11 小时 ≈ 27 元每小时：省钱档，应当明显高于高铁。"""
        sleeper = R.value_score({"total_hours": 11, "cost_low": 300, "comfort_score": 72})
        high_speed = R.value_score({"total_hours": 4.5, "cost_low": 550, "comfort_score": 85})
        self.assertGreater(sleeper, high_speed)
        self.assertGreaterEqual(sleeper, 60, f"硬卧只得了 {sleeper} 分")

    def test_long_but_cheap_middle(self):
        """300 元 / 36 小时（成都→拉萨硬座）约 8 元每小时：便宜，但要扣时间分。"""
        score = R.value_score({"total_hours": 36.5, "cost_low": 300, "comfort_score": 55})
        self.assertTrue(55 <= score <= 85, f"得到 {score} 分，区间不合理")

    def test_comfort_slightly_rewards(self):
        base = {"total_hours": 10, "cost_low": 300}
        low = R.value_score({**base, "comfort_score": 40})
        high = R.value_score({**base, "comfort_score": 90})
        self.assertGreater(high, low)
        self.assertLess(high - low, 15, "舒适度只能小幅影响性价比")


class TestQuery(unittest.TestCase):
    def test_query_by_city(self):
        hits = R.query(city="拉萨")
        self.assertTrue(hits, "应当有到拉萨的路线")
        for item in hits:
            self.assertIn("拉萨", (item["from_city"], item["to_city"], item["name"]))

    def test_query_by_theme(self):
        hits = R.query(theme="夜车")
        self.assertTrue(hits)
        for item in hits:
            self.assertIn("夜车", item["themes"])

    def test_query_unknown_city_returns_empty(self):
        self.assertEqual(R.query(city="不存在的地方"), [])

    def test_sort_orders(self):
        cheap_first = R.sort_routes(R.routes(), "cheap")[0]
        comfort_first = R.sort_routes(R.routes(), "comfort")[0]
        self.assertEqual(cheap_first["cost_low"], min(r["cost_low"] for r in R.routes()))
        self.assertEqual(comfort_first["comfort_score"],
                         max(r["comfort_score"] for r in R.routes()))

    def test_for_cities_ranks_by_hits(self):
        hits = R.for_cities(["北京", "上海"])
        self.assertTrue(hits)
        self.assertIn("北京", (hits[0]["from_city"], hits[0]["to_city"], hits[0]["name"]))

    def test_card_shape(self):
        card = R.card(R.routes()[0])
        for key in ("id", "name", "segments", "save_tips", "source", "value_score"):
            self.assertIn(key, card)
        self.assertNotIn("_file", card)


class TestContentValidation(unittest.TestCase):
    """直接调构建脚本的校验，保证素材本身是干净的（和 CI 一个口径）。"""

    def test_content_passes_validator(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "build_route_catalog", ROOT / "scripts" / "build_route_catalog.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)                      # type: ignore[union-attr]
        data, problems = mod.load_content()
        self.assertFalse(problems, f"素材加载有问题：{problems}")
        self.assertFalse(mod.validate(data), "素材校验不通过")

    def test_raw_files_shape(self):
        files = sorted(CONTENT.glob("*.json"))
        self.assertTrue(files, "data/route_content 下应当有素材文件")
        total = 0
        for path in files:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(raw, dict, path.name)
            self.assertTrue(raw.get("updated"), f"{path.name} 缺口径日期 updated")
            total += len(raw.get("routes") or [])
        self.assertEqual(total, len(R.routes()), "素材条数与目录条数不一致（忘了跑构建脚本？）")


class TestKindAndPaging(unittest.TestCase):
    """单向 vs 环线：UI 要能分开筛，规划里的用法完全不同。"""

    def test_kind_inferred_from_cities(self):
        self.assertEqual(R.route_kind({"from_city": "西宁", "to_city": "西宁"}), "loop")
        self.assertEqual(R.route_kind({"from_city": "北京", "to_city": "上海"}), "oneway")

    def test_kind_explicit_override(self):
        """同城来回但不闭环的（如「上海→上海 一日往返」）允许显式写成 oneway。"""
        self.assertEqual(R.route_kind({"from_city": "上海", "to_city": "上海",
                                       "kind": "oneway"}), "oneway")
        self.assertEqual(R.route_kind({"from_city": "北京", "to_city": "上海",
                                       "kind": "loop"}), "loop")

    def test_kind_normalised_names(self):
        """「西宁（环湖）」这种写法也要认成环线。"""
        self.assertEqual(R.route_kind({"from_city": "西宁（环湖）", "to_city": "西宁"}), "loop")

    def test_catalog_kinds_are_labelled(self):
        for item in R.routes():
            self.assertIn(item["kind"], ("oneway", "loop"), item["id"])
            self.assertTrue(item["kind_label"], item["id"])

    def test_query_filters_by_kind(self):
        loops = R.query(kind="loop")
        ones = R.query(kind="oneway")
        self.assertTrue(loops and ones)
        self.assertTrue(all(r["kind"] == "loop" for r in loops))
        self.assertTrue(all(r["kind"] == "oneway" for r in ones))
        self.assertEqual(len(loops) + len(ones), len(R.routes()))

    def test_stats_counts_both(self):
        s = R.stats()
        self.assertEqual(s["loops"] + s["oneway"], s["routes"])


class TestImportValidation(unittest.TestCase):
    """一键导入：缺来源/缺坐标/巧思高分没写理由 都要在写库前拦下。"""

    def _raw(self, **over):
        item = {
            "id": "t-import-one", "name": "自测·上海到苏州", "from_city": "上海", "to_city": "苏州",
            "category": "自测", "tier": "观景", "themes": ["自测"], "summary": "自测用",
            "segments": [{"frm": "上海", "to": "苏州", "mode": "high_speed",
                          "hours": 0.5, "price": "约 40 元"}],
            "total_hours": 0.5, "cost_low": 40, "cost_high": 60,
            "source": "自测", "source_url": "https://www.12306.cn/", "confidence": "low",
        }
        item.update(over)
        return {"routes": [item]}

    def test_good_payload_passes(self):
        from travel_planner import route_import

        rep = route_import.validate(self._raw())
        self.assertEqual(rep["problems"], [])
        self.assertEqual(rep["count"], 1)
        self.assertEqual(rep["routes"][0]["kind"], "oneway")

    def test_missing_source_is_caught(self):
        from travel_planner import route_import

        rep = route_import.validate(self._raw(source=""))
        self.assertTrue(any("source" in p for p in rep["problems"]), rep["problems"])

    def test_unknown_clever_tag_is_caught(self):
        from travel_planner import route_import

        rep = route_import.validate(self._raw(clever_tags=["我编的"], clever_extra=5))
        self.assertTrue(any("clever_tags" in p for p in rep["problems"]), rep["problems"])

    def test_high_clever_without_tips_is_caught(self):
        from travel_planner import route_import

        # extra_stops 30 + detour_worth 22 = 52 < 70，加 20 额外分才过线
        rep = route_import.validate(self._raw(clever_tags=["extra_stops", "detour_worth"],
                                              clever_extra=20))
        self.assertTrue(any("smart_tips" in p for p in rep["problems"]), rep["problems"])

    def test_clever_route_needs_baseline(self):
        from travel_planner import route_import

        rep = route_import.validate(self._raw(clever_tags=["extra_stops", "night_move"],
                                              clever_extra=10, smart_tips=["妙在顺路"]))
        self.assertTrue(any("cost_baseline" in p for p in rep["problems"]), rep["problems"])

    def test_missing_coord_is_reported(self):
        from travel_planner import route_import

        rep = route_import.validate(self._raw(to_city="不存在的地方"))
        self.assertIn("不存在的地方", rep["new_cities"])

    def test_loop_without_flag_is_caught(self):
        from travel_planner import route_import

        rep = route_import.validate(self._raw(to_city="上海"))
        self.assertTrue(any("loop" in p for p in rep["problems"]), rep["problems"])

    def test_bad_json_shape(self):
        from travel_planner import route_import

        rep = route_import.validate({"routes": []})
        self.assertEqual(rep["count"], 0)


class TestConcreteMaterialisation(unittest.TestCase):
    """把参考路线落到具体一天：能核实的核实，核实不了的必须说「查不到」。

    这是整个功能里最容易被"善意撒谎"的地方 —— 素材里的车次是攻略里的，
    到了你真正出行那天可能根本不开。所以每条都要有明确状态。
    """

    def setUp(self):
        import tempfile
        from travel_planner import db

        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stations(code,name,city,ordinal) VALUES('BJP','北京','北京',0)")
            self.conn.execute(
                "INSERT INTO stations(code,name,city,ordinal) VALUES('SHH','上海','上海',0)")
            # 一天真实数据：G1 北京→上海
            for seq, code, name, arr, dep in ((1, "BJP", "北京", "", "08:00"),
                                              (2, "SHH", "上海", "12:30", "")):
                self.conn.execute(
                    "INSERT INTO schedules(train_no,train_code,date,seq,station_code,"
                    "station_name,day,arr,dep) VALUES('T1','G1','2026-06-17',?,?,?,0,?,?)",
                    (seq, code, name, arr, dep))
        db.set_meta(self.conn, "import:2026-06-17", "test")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _route(self, segments):
        return {"id": "t", "name": "测试·北京到上海", "from_city": "北京", "to_city": "上海",
                "segments": segments}

    def test_train_segment_is_verified(self):
        from travel_planner import concrete

        plan = concrete.concrete_plan(
            self.conn, self._route([{"frm": "北京", "to": "上海", "mode": "train",
                                     "hours": 4.5, "price": "约 553 元"}]),
            "2026-06-17", today="2026-06-01")
        seg = plan["segments"][0]
        self.assertEqual(seg["status"], "ok")
        self.assertTrue(seg["verified"])
        self.assertEqual(seg["options"][0]["code"], "G1")
        self.assertEqual(plan["summary"]["verified"], 1)

    def test_missing_day_is_not_faked(self):
        from travel_planner import concrete

        plan = concrete.concrete_plan(
            self.conn, self._route([{"frm": "北京", "to": "上海", "mode": "train",
                                     "hours": 4.5, "price": "约 553 元"}]),
            "2027-01-01", today="2026-06-01")
        seg = plan["segments"][0]
        self.assertEqual(seg["status"], "no_data")
        self.assertFalse(seg["verified"])
        self.assertTrue(any("还没有时刻表数据" in w for w in plan["warnings"]))

    def test_bus_and_boat_are_manual(self):
        from travel_planner import concrete

        plan = concrete.concrete_plan(
            self.conn, self._route([
                {"frm": "桂林", "to": "阳朔", "mode": "bus", "hours": 1.5, "price": "约 30 元",
                 "note": "最省的常规走法"},
                {"frm": "桂林", "to": "阳朔", "mode": "boat", "hours": 4, "price": "约 210 元"}]),
            "2026-06-17", today="2026-06-01")
        for seg in plan["segments"]:
            self.assertEqual(seg["status"], "manual")
            self.assertFalse(seg["verified"])
            self.assertTrue(seg["note"], "手工段必须给出提示")
        self.assertIn("最省的常规走法", plan["segments"][0]["note"], "应当带上素材里的注释")
        self.assertEqual(plan["summary"]["manual"], 2)

    def test_loop_car_segment_not_asked_as_od(self):
        """环线（起终点同城）不能当两地车程去问高德。"""
        from travel_planner import concrete

        plan = concrete.concrete_plan(
            self.conn, self._route([{"frm": "西宁", "to": "西宁（环湖）", "mode": "car",
                                     "hours": 12, "price": "租车约 200 元/天"}]),
            "2026-06-17", today="2026-06-01")
        seg = plan["segments"][0]
        self.assertEqual(seg["status"], "manual")
        self.assertIn("环线", seg["note"])

    def test_far_future_warns_presale_window(self):
        from travel_planner import concrete

        plan = concrete.concrete_plan(self.conn, self._route([]), "2027-06-01", today="2026-06-01")
        self.assertTrue(any("预售窗口" in w for w in plan["warnings"]))

    def test_bad_date_rejected(self):
        from travel_planner import concrete

        self.assertFalse(concrete.concrete_plan(self.conn, self._route([]), "不是日期")["ok"])


class TestRouteTrip(unittest.TestCase):
    """路线 → 排成行程：城市序列要正确，排不出来必须说清原因。"""

    def setUp(self):
        import tempfile
        from travel_planner import db

        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        with self.conn:
            for code, name, city in (("BJP", "北京", "北京"), ("SHH", "上海", "上海"),
                                     ("NJH", "南京", "南京"), ("XJH", "南京南", "南京")):
                self.conn.execute(
                    "INSERT INTO stations(code,name,city,ordinal) VALUES(?,?,?,0)",
                    (code, name, city))
            for seq, code, name, arr, dep in ((1, "BJP", "北京", "", "08:00"),
                                              (2, "NJH", "南京", "12:00", "12:10"),
                                              (3, "SHH", "上海", "14:00", "")):
                self.conn.execute(
                    "INSERT INTO schedules(train_no,train_code,date,seq,station_code,"
                    "station_name,day,arr,dep) VALUES('T1','G1','2026-06-17',?,?,?,0,?,?)",
                    (seq, code, name, arr, dep))
        db.set_meta(self.conn, "import:2026-06-17", "test")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _route(self, segments, **over):
        item = {"id": "t", "name": "自测·北京到上海", "from_city": "北京", "to_city": "上海",
                "kind": "oneway", "segments": segments, "total_hours": 6}
        item.update(over)
        return item

    def test_stops_from_segments(self):
        from travel_planner import concrete

        route = self._route([
            {"frm": "北京", "to": "南京", "mode": "high_speed", "hours": 4, "price": "约 400 元"},
            {"frm": "南京", "to": "上海", "mode": "high_speed", "hours": 2, "price": "约 150 元"}])
        self.assertEqual(concrete.route_stops(self.conn, route), ["北京", "南京", "上海"])

    def test_station_names_become_cities(self):
        """分段里写「南京南」这种站名也要归到城市，否则引擎认不出。"""
        from travel_planner import concrete

        route = self._route([{"frm": "北京", "to": "南京南", "mode": "high_speed",
                              "hours": 4, "price": "约 400 元"}])
        self.assertEqual(concrete.route_stops(self.conn, route), ["北京", "南京"])

    def test_loop_drops_duplicate_tail(self):
        from travel_planner import concrete

        route = self._route([
            {"frm": "南京", "to": "上海", "mode": "high_speed", "hours": 2, "price": "约 150 元"},
            {"frm": "上海", "to": "南京", "mode": "high_speed", "hours": 2, "price": "约 150 元"}],
            kind="loop")
        self.assertEqual(concrete.route_stops(self.conn, route), ["南京", "上海"])

    def test_stay_modes(self):
        from travel_planner import concrete

        route = self._route([{"frm": "北京", "to": "上海", "mode": "high_speed",
                              "hours": 4, "price": "约 550 元"}])
        self.assertEqual(concrete.stays_for(self.conn, route, "transit")[0]["nights"], 0)
        self.assertEqual(concrete.stays_for(self.conn, route, "two_nights")[0]["nights"], 2)
        # 4 小时的一日游默认不过夜
        self.assertEqual(concrete.stays_for(self.conn, route)[0]["mode"], "transit")
        # 长途默认安排过夜（40 小时 → 每站 2 晚）
        long_route = self._route(route["segments"], total_hours=40)
        self.assertEqual(concrete.stays_for(self.conn, long_route)[0]["nights"], 2)
        # 中等长度（20 小时，比如一趟夜车）给 1 晚
        night_route = self._route(route["segments"], total_hours=20)
        self.assertEqual(concrete.stays_for(self.conn, night_route)[0]["nights"], 1)

    def test_unknown_stop_reports_clearly(self):
        """没有铁路站的落脚点：既要说清是哪个点，也要区分「基地式周边游」与「城际路线」。"""
        from travel_planner import concrete

        route = self._route([{"frm": "北京", "to": "黑马河", "mode": "bus",
                              "hours": 3, "price": "约 40 元"}])
        # 关掉地面接驳：仍旧按老口径报「这些地点在车站表里找不到」
        res = concrete.plan_trip(self.conn, route, "2026-06-17", ground_ok=False)
        self.assertFalse(res["ok"])
        self.assertIn("黑马河", res["unknown_stops"])
        self.assertIn("车站表", res["error"])
        # 默认口径：只剩一个铁路锚点 → 明说这是「以一座城为基地的周边游」，不是排错了
        res2 = concrete.plan_trip(self.conn, route, "2026-06-17")
        self.assertFalse(res2["ok"])
        self.assertEqual(res2.get("kind"), "base")
        self.assertIn("黑马河", res2["error"])
        self.assertIn("周边游", res2["error"])

    def test_ground_stops_become_connector_legs(self):
        """景区/小镇落脚点不该让整条路线报废：铁路段照算，地面段照素材标注为估算。"""
        from travel_planner import concrete

        route = self._route([
            {"frm": "北京", "to": "南京", "mode": "high_speed", "hours": 4,
             "price": "约 400 元"},
            {"frm": "南京", "to": "婺源", "mode": "high_speed", "hours": 2,
             "price": "约 150 元"},
            {"frm": "婺源", "to": "篁岭", "mode": "car", "hours": 0.7,
             "price": "约 60 元", "note": "县城到篁岭约 40 分钟，包车或景区直通车"},
        ], total_hours=7)
        info = concrete.stop_plan(self.conn, route)
        self.assertEqual(info["stops"], ["北京", "南京", "婺源", "篁岭"])
        self.assertEqual(info["rail"], ["北京", "南京"])
        self.assertEqual(info["ground"], ["婺源", "篁岭"])

        plan = concrete.plan_trip(self.conn, route, "2026-06-17")
        self.assertTrue(plan["ok"], plan.get("error"))
        self.assertEqual(plan["stops"], ["北京", "南京"], "交给引擎的只有铁路锚点")
        self.assertEqual(plan["ground_count"], 2)
        self.assertTrue(plan["partial"])
        stops = {g["stop"]: g for g in plan["ground_legs"]}
        self.assertEqual(stops["篁岭"]["mode"], "car")
        self.assertEqual(stops["篁岭"]["status"], "estimate")
        self.assertAlmostEqual(stops["篁岭"]["hours"], 0.7)
        self.assertEqual(stops["篁岭"]["anchor"], "南京", "婺源也没铁路站，继续往前挂到南京")
        self.assertTrue(all(g["anchor"] in plan["stops"] for g in plan["ground_legs"]))
        self.assertTrue(any("地面接驳" in w and "篁岭" in w for w in plan["warnings"]))
        attached = [g for day in plan["days"] for g in day.get("ground") or []]
        self.assertEqual(len(attached), 2, "地面段要挂进具体某一天")
        self.assertEqual(plan["summary"]["ground"], 2)
        self.assertIn("地面接驳", plan["note"])

    def test_single_city_route_rejected(self):
        from travel_planner import concrete

        route = self._route([{"frm": "南京", "to": "南京南", "mode": "metro",
                              "hours": 0.5, "price": "约 5 元"}], kind="loop")
        res = concrete.plan_trip(self.conn, route, "2026-06-17")
        self.assertFalse(res["ok"])
        self.assertIn("一个城市", res["error"])

    def test_trip_produces_rides(self):
        from travel_planner import concrete

        route = self._route([{"frm": "北京", "to": "上海", "mode": "high_speed",
                              "hours": 6, "price": "约 550 元"}])
        res = concrete.plan_trip(self.conn, route, "2026-06-17")
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["stops"], ["北京", "上海"])
        self.assertGreaterEqual(res["summary"]["rides"], 1)
        codes = [l.get("code") for day in res["days"]
                 for l in (day.get("journey") or {}).get("legs") or []]
        self.assertIn("G1", codes)

    def test_trip_warns_when_day_has_no_data(self):
        """没有那一天的数据时，引擎会拿基准日拷贝来算 —— 必须明说，不能让人以为是当天的车。"""
        from travel_planner import concrete

        route = self._route([{"frm": "北京", "to": "上海", "mode": "high_speed",
                              "hours": 6, "price": "约 550 元"}])
        res = concrete.plan_trip(self.conn, route, "2027-01-01")
        self.assertTrue(res["ok"], res.get("error"))
        self.assertTrue(any("不是当天的真实车次" in w for w in res["warnings"]), res["warnings"])

    def test_trip_uses_real_day_without_warning(self):
        from travel_planner import concrete

        route = self._route([{"frm": "北京", "to": "上海", "mode": "high_speed",
                              "hours": 6, "price": "约 550 元"}])
        res = concrete.plan_trip(self.conn, route, "2026-06-17")
        self.assertTrue(res["ok"])
        self.assertFalse(any("不是当天的真实车次" in w for w in res["warnings"]))
        # 测试库只有 1 趟车，会被判成「车次偏少」；这里只要求它给出档位而不是 none
        self.assertIn("source", res["data_source"])
        self.assertNotEqual(res["data_source"]["source"], "missing")


if __name__ == "__main__":
    unittest.main()
