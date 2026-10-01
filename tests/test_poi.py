"""城内点位（poi）单测：挑点规则、负缓存、覆盖率、后台任务。

这里最要紧的是**挑点**：高德对同一个关键词会返回一堆"配套点"，
实测过 豫园→豫园(地铁站)、鼓浪屿→鼓浪屿船票官方、颐和园→颐和园路、外滩→外滩中心。
名字都对得上，所以必须靠类型把它们压下去 —— 宁可记未命中，也不收一个错坐标。
全部离线跑（抓取器是注入的假数据）。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from travel_planner import poi

#: 上海中心点（城市目录里的值），用来算距离
SH = {"city": "上海", "name": "外滩", "lat": 31.2304, "lon": 121.4737, "province": "上海"}


def _poi(name, lon, lat, type_full="", rating=""):
    return {"name": name, "lon": lon, "lat": lat, "type_full": type_full,
            "kind": type_full.split(";")[-1] if type_full else "",
            "rating": rating, "address": "", "tel": ""}


class TestNormName(unittest.TestCase):
    def test_strips_notes_and_tails(self):
        self.assertEqual(poi.norm_name("龙门石窟景区"), "龙门石窟")
        self.assertEqual(poi.norm_name("上海博物馆(人民广场馆)"), "上海博物馆")
        self.assertEqual(poi.norm_name("中央大街·步行街"), "中央大街步行街")
        self.assertEqual(poi.norm_name("外滩"), "外滩")

    def test_type_words_stay_in_the_name(self):
        """「博物馆/公园/寺」是名字的一部分，抹掉就会让「上海博物馆」变成「上海」。"""
        self.assertEqual(poi.norm_name("上海博物馆"), "上海博物馆")
        self.assertEqual(poi.norm_name("王城公园"), "王城公园")
        self.assertEqual(poi.norm_name("南普陀寺"), "南普陀寺")

    def test_longest_tail_wins(self):
        """「景区」比「风景区」短，先撞上就会把「黄龙风景区」削成「黄龙风」。"""
        self.assertEqual(poi.norm_name("黄龙风景区"), "黄龙")
        self.assertEqual(poi.norm_name("泰宁世界地质公园"), "泰宁世界")
        self.assertEqual(poi.norm_name("平江路历史街区"), "平江路")


class TestJudge(unittest.TestCase):
    def test_exact_place_beats_its_annex(self):
        """外滩中心（商务写字楼）不能顶替外滩。"""
        annex = poi.judge("外滩", SH, _poi("外滩中心", 121.4872, 31.2325,
                                          "商务住宅;楼宇;商务写字楼"))
        real = poi.judge("外滩", SH, _poi("外滩", 121.4900, 31.2400,
                                         "风景名胜;风景名胜;风景名胜", "4.7"))
        self.assertFalse(annex["ok"], annex)
        self.assertTrue(real["ok"], real)
        self.assertGreater(real["score"], 100)

    def test_annex_points_are_rejected_by_name(self):
        """「豫园(地铁站)」归一化后就是「豫园」，只能靠名字里多出来的「地铁站」否掉。"""
        metro = poi.judge("豫园", SH, _poi("豫园(地铁站)", 121.4874, 31.2280,
                                          "交通设施服务;地铁站;地铁站"))
        annex = poi.judge("外滩", SH, _poi("外滩中心", 121.4872, 31.2325,
                                          "商务住宅;楼宇;商务写字楼"))
        for verdict in (metro, annex):
            self.assertFalse(verdict["ok"], verdict)
            self.assertIn("配套点", verdict["reason"])

    def test_shop_and_cashier_annexes_are_rejected(self):
        """实测漏网的两个：天涯海角(天鹿雅苑店)、蜈支洲岛(项目收银处)。"""
        shop = poi.judge("天涯海角", {"city": "三亚", "name": "天涯海角", "lat": 18.2987,
                                      "lon": 109.4522, "province": "海南"},
                         _poi("天涯海角(天鹿雅苑店)", 109.5008, 18.2676,
                              "购物服务;购物相关场所;购物相关场所", "3.9"))
        cashier = poi.judge("蜈支洲岛", {"city": "三亚", "name": "蜈支洲岛", "lat": 18.2987,
                                        "lon": 109.4522, "province": "海南"},
                            _poi("蜈支洲岛(项目收银处)", 109.7275, 18.2743,
                                 "体育休闲服务;娱乐场所;游戏厅", "4.2"))
        for verdict in (shop, cashier):
            self.assertFalse(verdict["ok"], verdict)

    def test_plain_place_with_good_type_is_kept(self):
        target = {"city": "三亚", "name": "亚龙湾", "lat": 18.2987, "lon": 109.4522,
                  "province": "海南"}
        verdict = poi.judge("亚龙湾", target,
                            _poi("亚龙湾海滩", 109.6460, 18.2292, "风景名胜;风景名胜;海滩", "4.8"))
        self.assertTrue(verdict["ok"], verdict)

    def test_ticket_office_is_rejected(self):
        ticket = poi.judge("鼓浪屿", {"city": "厦门", "name": "鼓浪屿",
                                      "lat": 24.4489, "lon": 118.0686, "province": "福建"},
                           _poi("鼓浪屿船票官方", 118.0757, 24.4823,
                                "生活服务;售票处;售票处"))
        self.assertFalse(ticket["ok"], ticket)
        self.assertIn("不像", ticket["reason"] + ticket.get("reason", ""))

    def test_road_is_rejected(self):
        road = poi.judge("颐和园", {"city": "北京", "name": "颐和园",
                                    "lat": 39.9042, "lon": 116.4074, "province": "北京"},
                         _poi("颐和园路", 116.3056, 39.9866, "地名地址信息;交通地名;道路名"))
        self.assertFalse(road["ok"], road)

    def test_walking_street_typed_as_road_is_kept(self):
        """中央大街在高德那里是「地名地址;道路名」——名字完全一致时不能因此否掉。"""
        target = {"city": "哈尔滨", "name": "中央大街", "lat": 45.7570,
                  "lon": 126.6425, "province": "黑龙江"}
        verdict = poi.judge("中央大街", target,
                            _poi("中央大街", 126.6189, 45.7739,
                                 "地名地址信息;交通地名;道路名"))
        self.assertTrue(verdict["ok"], verdict)
        self.assertGreaterEqual(verdict["score"], poi.ACCEPT_SCORE)

    def test_far_away_managed_county_is_kept_with_note(self):
        """酒泉代管敦煌（约 380 公里）：省份对得上就该收，但要在备注里说清距离。"""
        target = {"city": "酒泉", "name": "莫高窟", "lat": 39.7325, "lon": 98.4941,
                  "province": "甘肃"}
        verdict = poi.judge("莫高窟", target,
                            _poi("莫高窟", 94.8100, 40.0400, "风景名胜;风景名胜;世界遗产", "4.8"))
        self.assertTrue(verdict["ok"], verdict)
        self.assertGreater(verdict["distance_km"], 250)

    def test_unrelated_name_is_rejected(self):
        verdict = poi.judge("外滩", SH, _poi("南京路步行街", 121.4800, 31.2350,
                                            "风景名胜;风景名胜;风景名胜"))
        self.assertFalse(verdict["ok"])
        self.assertIn("名字对不上", verdict["reason"])

    def test_cross_province_is_kept_out(self):
        """同名的点在别的省（今天真踩过：八达岭被查到广东）必须丢弃。"""
        target = {"city": "北京", "name": "八达岭长城", "lat": 39.9042,
                  "lon": 116.4074, "province": "北京"}
        verdict = poi.judge("八达岭长城", target,
                            _poi("八达岭长城", 113.6213, 23.1328, "风景名胜;风景名胜"))
        self.assertFalse(verdict["ok"])
        self.assertTrue("省份" in verdict["reason"] or "公里" in verdict["reason"], verdict)

    def test_too_far_is_rejected(self):
        target = {"city": "上海", "name": "外滩", "lat": 31.2304, "lon": 121.4737,
                  "province": "上海"}
        verdict = poi.judge("外滩", target, _poi("外滩", 104.3, 24.9, "风景名胜;风景名胜"))
        self.assertFalse(verdict["ok"])


class TestPick(unittest.TestCase):
    def test_picks_best_and_records_why(self):
        batch = [
            _poi("豫园(地铁站)", 121.4874, 31.2280, "交通设施服务;地铁站;地铁站"),
            _poi("豫园", 121.4920, 31.2270, "风景名胜;风景名胜;风景名胜", "4.8"),
            _poi("豫园商城", 121.4900, 31.2280, "购物服务;商场;商场", "4.5"),
        ]
        best = poi.pick("豫园", SH, batch)
        self.assertIsNotNone(best)
        self.assertEqual(best["name"], "豫园")
        self.assertIn("名字完全一致", best["why"])
        self.assertGreater(best["score"], 100)

    def test_nothing_good_means_miss(self):
        batch = [_poi("豫园(地铁站)", 121.4874, 31.2280, "交通设施服务;地铁站;地铁站")]
        self.assertIsNone(poi.pick("豫园", SH, batch))

    def test_near_miss_explains_what_came_back(self):
        batch = [_poi("颐和园路", 116.3056, 39.9866, "地名地址信息;交通地名;道路名")]
        target = {"city": "北京", "name": "颐和园", "lat": 39.9042, "lon": 116.4074,
                  "province": "北京"}
        reason = poi.near_miss(target, batch)
        self.assertIn("颐和园路", reason)


class TestHarvest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "poi.db"
        self.targets = poi.targets()
        self.assertGreater(len(self.targets), 100, "城市目录里应当有几百个看点")

    def tearDown(self):
        self.tmp.cleanup()

    def _fake(self, table):
        def fetch(city, name):
            return list(table.get((city, name), []))
        return fetch

    def test_harvest_stores_hits_and_misses(self):
        first = self.targets[0]
        second = self.targets[1]
        table = {
            (first["city"], first["name"]): [
                _poi(first["name"], first["lon"] + 0.01, first["lat"] + 0.01,
                     "风景名胜;风景名胜;风景名胜", "4.6")],
            (second["city"], second["name"]): [
                _poi(second["name"] + "路", second["lon"] + 0.01, second["lat"],
                     "地名地址信息;交通地名;道路名")],
        }
        out = poi.harvest(limit=2, delay=0, db_path=self.db, fetch=self._fake(table))
        self.assertTrue(out["ok"], out)
        self.assertEqual((out["total"], out["hit"], out["miss"]), (2, 1, 1))
        conn = poi.connect(self.db)
        try:
            rows = poi.list_pois(conn)["pois"]
            self.assertEqual(len(rows), 2)
            by_name = {r["name"]: r for r in rows}
            hit = by_name[first["name"]]
            self.assertEqual(hit["status"], "ok")
            self.assertTrue(hit["has_coord"])
            self.assertGreater(hit["score"], 100)
            miss = by_name[second["name"]]
            self.assertEqual(miss["status"], "miss")
            self.assertIsNone(miss["lon"])
            self.assertIn("路", miss["why"], "未命中也要写清返回了什么")
            cov = poi.coverage(conn)
            self.assertEqual(cov["hit"], 1)
            self.assertEqual(cov["miss"], 1)
            self.assertEqual(cov["pending"], len(self.targets) - 2)
        finally:
            conn.close()

    def test_second_run_skips_what_is_done(self):
        calls = []

        def fetch(city, name):
            calls.append((city, name))
            target = next(t for t in self.targets if t["name"] == name)
            return [_poi(name, target["lon"] + 0.01, target["lat"] + 0.01,
                         "风景名胜;风景名胜;风景名胜", "4.5")]

        first = poi.harvest(limit=1, delay=0, db_path=self.db, fetch=fetch)
        self.assertEqual(first["total"], 1)
        # 再跑一次（同样 limit=1）：上次查过的那个被跳过，改查名单里的下一个
        second = poi.harvest(limit=1, delay=0, db_path=self.db, fetch=fetch)
        self.assertEqual(second["total"], 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(set(calls)), 2, "同一个名字不该被查两次")
        # --refresh 才会重查已落库的名字
        refreshed = poi.harvest(limit=1, delay=0, db_path=self.db, refresh=True, fetch=fetch)
        self.assertEqual(refreshed["total"], 1)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], calls[2], "--refresh 重查的是第一个名字")

    def test_failure_is_recorded_but_does_not_stop_the_run(self):
        first, second = self.targets[0], self.targets[1]

        def fetch(city, name):
            if (city, name) == (first["city"], first["name"]):
                raise RuntimeError("模拟网络错误")
            return [_poi(name, second["lon"] + 0.01, second["lat"] + 0.01,
                         "风景名胜;风景名胜;风景名胜", "4.4")]

        out = poi.harvest(limit=2, delay=0, db_path=self.db, fetch=fetch)
        self.assertEqual(out["failed"], 1)
        self.assertEqual(out["hit"], 1)
        self.assertTrue(out["failures"])
        conn = poi.connect(self.db)
        try:
            miss = [r for r in poi.list_pois(conn)["pois"] if r["status"] == "miss"]
            self.assertEqual(len(miss), 1)
            self.assertIn("查询失败", miss[0]["why"])
        finally:
            conn.close()

    def test_export_and_stats(self):
        first = self.targets[0]
        poi.harvest(limit=1, delay=0, db_path=self.db, fetch=self._fake({
            (first["city"], first["name"]): [
                _poi(first["name"], first["lon"] + 0.01, first["lat"] + 0.01,
                     "风景名胜", "4.5")]}))
        conn = poi.connect(self.db)
        try:
            blob = poi.export_json(conn)
            self.assertIn(first["name"], blob)
            stats = poi.stats(conn)
            self.assertEqual(stats["last_harvest"]["hit"], 1)
            self.assertEqual(stats["targets"], len(self.targets))
        finally:
            conn.close()


class TestRevalidate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "poi.db"
        self.targets = poi.targets()

    def tearDown(self):
        self.tmp.cleanup()

    def test_demotes_stored_annex_points(self):
        """换了规则之后，旧命中里的配套点要能本地降级（不必把 900 个名字重查一遍）。"""
        target = next(t for t in self.targets if t["name"] == "亚龙湾") if any(
            t["name"] == "亚龙湾" for t in self.targets) else self.targets[0]
        conn = poi.connect(self.db)
        try:
            # 手写一条「名字对得上、其实是配套点」的旧命中
            poi.store(conn, target, {"name": f"{target['name']}(天鹿雅苑店)",
                                     "lon": target["lon"] + 0.01, "lat": target["lat"],
                                     "type_full": "购物服务;购物相关场所;购物相关场所",
                                     "rating": "3.9", "distance_km": 1.2, "score": 99.0,
                                     "why": "名字完全一致"})
            self.assertEqual(poi.coverage(conn)["hit"], 1)
            out = poi.revalidate(conn)
            self.assertEqual(out["checked"], 1)
            self.assertEqual(out["demoted"], 1)
            self.assertEqual(out["samples"][0]["poi_name"], f"{target['name']}(天鹿雅苑店)")
            after = poi.coverage(conn)
            self.assertEqual(after["hit"], 0)
            self.assertEqual(after["miss"], 1)
            row = poi.list_pois(conn, status="miss")["pois"][0]
            self.assertIn("配套点", row["why"])
        finally:
            conn.close()


class TestQueryVariants(unittest.TestCase):
    """名字不是高德里的正式 POI 名时，换个说法再查一次。"""

    def test_descriptive_tail_is_stripped(self):
        self.assertIn("平江路", poi.query_variants("平江路历史街区"))
        self.assertIn("北戴河", poi.query_variants("北戴河海滨"))
        self.assertEqual(poi.query_variants("稻城亚丁"), ["稻城亚丁"], "没得可拆就别乱拆")

    def test_and_clause_is_dropped(self):
        self.assertIn("天星小轮", poi.query_variants("天星小轮与维港"))

    def test_city_word_is_dropped(self):
        self.assertIn("敦煌博物馆", poi.query_variants("敦煌市博物馆"))

    def test_variants_are_capped_and_deduped(self):
        items = poi.query_variants("某某市历史街区")
        self.assertLessEqual(len(items), 3)
        self.assertEqual(len(items), len(set(items)))


class TestQueryForms(unittest.TestCase):
    """港澳台与自治州必须用县市级城市名查（校对时实测出来的）。"""

    def test_at_city_override(self):
        self.assertEqual(poi.split_query("日月潭 @南投县"), ("日月潭", "南投县"))
        self.assertEqual(poi.split_query("北港老街 @云林县"), ("北港老街", "云林县"))
        self.assertEqual(poi.split_query("外滩"), ("外滩", ""))

    def test_prefecture_falls_back_to_seat(self):
        self.assertIn("伊宁市", poi.city_candidates("伊犁"))
        self.assertIn("德令哈市", poi.city_candidates("海西州"))
        self.assertEqual(poi.city_candidates("北京"), ["北京"])

    def test_hk_mo_tw_are_inside_the_province_boxes(self):
        """这三个盒子以前没有，港澳台的点位全被当成「查偏」丢掉了。"""
        self.assertTrue(poi.in_province(25.0309, 121.5683), "台北101 该落在台湾框里")
        self.assertTrue(poi.in_province(22.2904, 114.1762), "维多利亚港 该落在香港框里")
        self.assertTrue(poi.in_province(22.1987, 113.5439), "澳门半岛 该落在澳门框里")

    def test_harvest_uses_the_seat_for_prefectures(self):
        """限定城市查不到时，应当换驻地县市名再试一次。"""
        targets = poi.targets()
        target = next((t for t in targets if t["city"] in poi.CITY_SEATS), None)
        self.assertIsNotNone(target, "城市目录里应当有自治州/盟")
        seat = poi.CITY_SEATS[target["city"]]
        calls = []

        def fetch(city, name):
            calls.append(city)
            if city == seat:
                return [{"name": target["name"], "lon": target["lon"] + 0.01,
                         "lat": target["lat"] + 0.01, "type_full": "风景名胜;风景名胜",
                         "kind": "风景名胜", "rating": "4.5", "address": "", "tel": ""}]
            return []

        original = poi.targets
        poi.targets = lambda: [target]          # 名单缩成这一条，跑真正的 harvest 流程
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = poi.harvest(limit=0, delay=0, db_path=Path(tmp) / "poi.db", fetch=fetch)
        finally:
            poi.targets = original
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["hit"], 1, out)
        self.assertIn(seat, calls, f"应当用驻地「{seat}」再查一次")
        self.assertIn(target["city"], calls, "原名也要试过")


class TestApplyFixes(unittest.TestCase):
    """把子代理的名称校对结果并进库：坐标要带来源，没坐标的保持未命中。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "poi.db"
        self.conn = poi.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _raw(self):
        return {"updated": "2026-09-23", "fixes": [
            {"city": "新北", "name": "九份老街", "official_name": "九份老街",
             "county": "瑞芳区", "lat": 25.106948, "lon": 121.848807,
             "source": "高德关键词检索「九份老街」（city=新北市）", "confidence": "high",
             "query": "九份老街 @新北市"},
            {"city": "汉中", "name": "汉中油菜花海", "official_name": "汉中油菜花海",
             "county": "", "lat": None, "lon": None,
             "source": "季节性花海无单一点位", "confidence": "low", "query": "皇塘油菜花"},
        ]}

    def test_applies_coords_with_provenance(self):
        out = poi.apply_fixes(self.conn, self._raw())
        self.assertEqual((out["total"], out["applied"], out["no_coord"]), (2, 1, 1))
        rows = {r["name"]: r for r in poi.list_pois(self.conn)["pois"]}
        hit = rows["九份老街"]
        self.assertEqual(hit["status"], "ok")
        self.assertAlmostEqual(hit["lat"], 25.106948)
        self.assertAlmostEqual(hit["lon"], 121.848807)
        self.assertIn("校对", hit["source"], "要能看出这个点是怎么来的")
        self.assertIn("高德关键词检索", hit["note"])
        miss = rows["汉中油菜花海"]
        self.assertEqual(miss["status"], "miss")
        self.assertIn("皇塘油菜花", miss["why"], "没坐标的把建议关键词记下来")

    def test_applied_fixes_survive_revalidate(self):
        """核心回归：并进来的校对点位不能被 revalidate 当成「查偏」再打回去。

        校对给出的正式名常是「国立故宫博物院（台北故宫博物院）」这种带旧名的写法，
        自动规则去比会判成「名字对不上」—— 已核对的条目不该被二审。
        """
        raw = self._raw()
        raw["fixes"].append(
            {"city": "台北", "name": "台北故宫博物院",
             "official_name": "国立故宫博物院（台北故宫博物院）", "county": "士林区",
             "lat": 25.102355, "lon": 121.548439,
             "source": "高德关键词检索「国立故宫博物院」（city=台北市）", "confidence": "high",
             "query": "国立故宫博物院 @台北市"})
        poi.apply_fixes(self.conn, raw)
        out = poi.revalidate(self.conn)
        self.assertEqual(out["demoted"], 0, f"不该被降级：{out['samples']}")
        self.assertGreaterEqual(out["skipped_reviewed"], 2, "校对过的条目应当跳过二审")
        self.assertEqual(poi.coverage(self.conn)["hit"], 2)


class TestJob(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "poi.db"
        self.targets = poi.targets()
        self.first = self.targets[0]

    def tearDown(self):
        self.tmp.cleanup()

    def test_job_runs_offline_with_injected_fetcher(self):
        original = poi.fetch_amap

        def fake(city, name, *, key="", timeout=20):
            return [_poi(name, self.first["lon"] + 0.01, self.first["lat"] + 0.01,
                         "风景名胜;风景名胜", "4.6")]

        poi.fetch_amap = fake                             # type: ignore[assignment]
        try:
            job = poi.PoiHarvestJob(limit=3, delay=0, key="test-key", db_path=self.db)
            job.start()
            self.assertTrue(poi.wait_for(job, timeout=60))
        finally:
            poi.fetch_amap = original                     # type: ignore[assignment]
        snap = job.snapshot()
        self.assertEqual(snap["state"], "done", snap)
        self.assertEqual(snap["total"], 3)
        self.assertEqual(snap["hit"], 3)
        self.assertTrue(snap["logs"])

    def test_registry_refuses_a_second_job(self):
        class FakeJob:
            state = poi.STATE_RUNNING

            def snapshot(self):
                return {"state": self.state}

        original = poi.HARVEST._current
        poi.HARVEST._current = FakeJob()                  # type: ignore[assignment]
        try:
            out = poi.HARVEST.start(limit=1, db_path=self.db)
        finally:
            poi.HARVEST._current = original               # type: ignore[assignment]
        self.assertFalse(out["ok"])
        self.assertIn("在跑", out["error"])


if __name__ == "__main__":
    unittest.main()
