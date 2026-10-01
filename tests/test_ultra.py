"""环线 Ultra（北京/河北出发穷游环线）单测：数据可用性、筛选、卡片与详情。

这个库的定位是**整条环线**（几天走完、每天住哪花多少），和单点走法的路线库互补，
所以测试重点在「能不能按出发地/天数/预算筛出来」以及「卡片字段够不够判断」。
数据本身由 `scripts/build_ultra_loops.py` 校验（出发地白名单、首尾闭环、不许写车次号）。
"""
from __future__ import annotations

import unittest

from travel_planner import ultra


class TestUltraCatalog(unittest.TestCase):
    def test_has_loops_from_both_origins(self):
        items = ultra.loops()
        self.assertGreaterEqual(len(items), 24, "北京 + 河北出发的环线应当有 20 条以上")
        stats = ultra.stats()
        self.assertGreaterEqual(stats["beijing"], 10, "北京出发至少 10 条")
        self.assertGreaterEqual(stats["hebei"], 10, "河北出发至少 10 条")
        self.assertGreaterEqual(stats["origins"], 8, "河北应当覆盖多个出发城市")

    def test_every_loop_is_a_real_loop(self):
        for item in ultra.loops():
            cities = item.get("cities") or []
            self.assertGreaterEqual(len(cities), 2, item.get("id"))
            self.assertEqual(cities[0], cities[-1],
                             f"{item['id']} 首尾城市不一致，就不是环线了")
            self.assertEqual(cities[0], item.get("origin"),
                             f"{item['id']} 起点与出发地不一致")
            self.assertEqual(len(item.get("itinerary") or []), item.get("days"),
                             f"{item['id']} 逐日安排条数与天数不符")
            self.assertLessEqual(item["budget_low"], item["budget_high"], item["id"])
            self.assertIn(item.get("confidence"), ("high", "medium", "low"), item["id"])

    def test_budget_is_budget_travel(self):
        """穷游口径：日均要压得住，但**大环线不能拿小环线的尺子量**。

        小环线（省内几站）日均 100-300 元；大环线跨省几千公里，日均里含大交通
        （比如进疆/进滇的硬卧与长途班车），380-450 是正常水平，所以上限分开定。
        """
        for item in ultra.loops():
            per_day = (item["budget_low"] + item["budget_high"]) / 2 / item["days"]
            cap = 480 if item.get("scale") == "large" else 320
            self.assertLess(per_day, cap,
                            f"{item['id']} 日均 {per_day:.0f} 元（{item.get('scale')} 上限 {cap}），不像穷游了")

    def test_no_train_numbers_in_itinerary(self):
        """逐日安排里不许出现车次号（会让人以为能照着查）——出处写在 source 里才允许。"""
        import re

        pattern = re.compile(r"[GDCZTK]\d{2,4}次?")
        for item in ultra.loops():
            for day in item.get("itinerary") or []:
                blob = str(day.get("title") or "") + str(day.get("detail") or "")
                self.assertIsNone(pattern.search(blob), f"{item['id']} {day.get('day')} 写了车次号")

    def test_large_loops_are_really_large(self):
        """大环线的定义要守住：跨省、≥8 天、整条线 ≥6 城、首尾闭环。

        用户的原话是「这些环线太小了，Ultra 还要大环线」—— 参照物是
        张家口 → 济南 → 南京 → 无锡 → 苏州 → 上海 → 杭州 这种把二十多小时直达拆开走的线，
        所以要挡住「4 天 3 城也叫大环线」这种注水。
        """
        large = [x for x in ultra.loops() if x.get("scale") == "large"]
        self.assertGreaterEqual(len(large), 12, f"大环线至少 12 条，现在只有 {len(large)} 条")
        for item in large:
            cities = item.get("cities") or []
            self.assertGreaterEqual(item["days"], 8, f"{item['id']} 大环线只有 {item['days']} 天")
            self.assertGreaterEqual(len(set(cities)), 6, f"{item['id']} 大环线只有 {len(set(cities))} 城")
            self.assertEqual(cities[0], cities[-1], f"{item['id']} 大环线也要回到出发地")
            self.assertTrue(item.get("hub_line"), f"{item['id']} 大环线要写清走哪条大动脉")
        # 大环线得跨省：regions 至少两个，或城市明显不止一个省
        multi = [x for x in large if len(x.get("regions") or []) >= 2]
        self.assertGreaterEqual(len(multi), len(large) * 0.8,
                                "大环线绝大多数应当跨大区（regions ≥ 2）")

    def test_scale_filter_and_sort(self):
        large = ultra.query(scale="large")
        self.assertTrue(large)
        self.assertTrue(all(x["scale"] == "large" for x in large))
        small = ultra.query(scale="small")
        self.assertTrue(all(x["scale"] == "small" for x in small))
        self.assertEqual(len(large) + len(small), len(ultra.query()))
        first = ultra.query(sort="scale")[0]
        self.assertEqual(first["scale"], "large", "大环线优先排序时第一条应当是大环线")
        # 卡片要带上「大环线 / 小环线」标签与所属大动脉，页面直接显示
        card = large[0]
        self.assertEqual(card["scale_label"], "大环线")
        stats = ultra.stats()
        self.assertGreaterEqual(stats["large"], 12)
        self.assertEqual(stats["large"] + stats["small"], stats["loops"])


class TestUltraQuery(unittest.TestCase):
    def test_filter_by_origin(self):
        beijing = ultra.query(origin="北京")
        self.assertTrue(beijing)
        self.assertTrue(all(x["origin"] == "北京" for x in beijing))
        shijiazhuang = ultra.query(origin="石家庄")
        self.assertTrue(shijiazhuang)
        self.assertTrue(all(x["origin"] == "石家庄" for x in shijiazhuang))

    def test_filter_by_days_and_budget(self):
        short = ultra.query(days_max=5)
        self.assertTrue(short)
        self.assertTrue(all(x["days"] <= 5 for x in short))
        cheap = ultra.query(budget_max=600)
        self.assertTrue(cheap)
        self.assertTrue(all(x["budget_low"] <= 600 for x in cheap))

    def test_keyword_search_hits_cities_and_highlights(self):
        hits = ultra.query(q="云冈") or ultra.query(q="大同")
        self.assertTrue(hits, "按地名/看点应当能搜到")
        self.assertTrue(any("大同" in x["cities"] for x in hits))

    def test_sorts(self):
        cheap_first = ultra.query(sort="budget")
        self.assertEqual(cheap_first[0]["budget_low"], min(x["budget_low"] for x in cheap_first))
        short_first = ultra.query(sort="days")
        self.assertEqual(short_first[0]["days"], min(x["days"] for x in short_first))
        value = ultra.query(sort="value")
        self.assertLessEqual(value[0]["per_day"], value[-1]["per_day"])

    def test_card_has_what_a_decision_needs(self):
        card = ultra.query()[0]
        for key in ("name", "origin", "days", "budget_low", "budget_high", "per_day",
                    "cities", "highlights", "transport", "best_season", "confidence",
                    "summary", "loop"):
            self.assertIn(key, card)
        self.assertTrue(card["loop"], "环线标记要带上")
        self.assertGreater(card["per_day"], 0)

    def test_detail_has_itinerary_and_tips(self):
        item = ultra.query()[0]
        detail = ultra.detail(item["id"])
        self.assertIsNotNone(detail)
        self.assertEqual(len(detail["itinerary"]), detail["days"])
        self.assertTrue(detail["save_tips"], "穷游线必须写怎么省")
        self.assertTrue(detail["watch_out"], "必须写注意事项")
        for day in detail["itinerary"]:
            self.assertGreater(len(str(day.get("detail") or "")), 40, day.get("day"))
        self.assertIsNone(ultra.detail("不存在的-id"))

    def test_origins_list_is_ordered_and_counted(self):
        origins = ultra.origins()
        self.assertEqual(origins[0]["name"], "北京", "北京排最前")
        self.assertEqual(sum(o["count"] for o in origins), len(ultra.loops()))
        for item in origins:
            self.assertIn(item["province"], ("北京", "河北"))


if __name__ == "__main__":
    unittest.main()
