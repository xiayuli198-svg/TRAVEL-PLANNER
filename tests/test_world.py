"""地球模式（worldmap）测试：世界城市 / 经典环线 / 世界之最 / 行程单。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from travel_planner import worldmap

CATALOG = Path(worldmap.DATA_DIR) / "world_catalog.json"
HAS_CATALOG = CATALOG.exists()


@unittest.skipUnless(HAS_CATALOG, "还没有生成 world_catalog.json")
class TestWorldCatalog(unittest.TestCase):
    def test_cities_are_complete(self):
        cities = worldmap.cities()
        self.assertGreaterEqual(len(cities), 60, "世界城市应当有 60 座以上")
        names = [c["name"] for c in cities]
        self.assertEqual(len(names), len(set(names)), "城市名不应重复")
        for c in cities:
            self.assertTrue(c.get("country"), f"{c['name']} 缺国家")
            self.assertTrue(c.get("area"), f"{c['name']} 缺区域")
            self.assertTrue(-90 <= float(c["lat"]) <= 90, c["name"])
            self.assertTrue(-180 <= float(c["lon"]) <= 180, c["name"])
            self.assertIsInstance(c.get("tz"), (int, float), f"{c['name']} 缺时差")
            self.assertTrue(c.get("intro"), f"{c['name']} 缺简介")
            self.assertTrue(c.get("guide"), f"{c['name']} 缺完整攻略")
            guide = c["guide"]
            self.assertEqual(len(guide.get("itinerary") or []), guide.get("days"),
                             f"{c['name']} 行程条数与天数不符")

    def test_night_cities_have_night_photos(self):
        night = [c for c in worldmap.cities() if c.get("night")]
        self.assertGreaterEqual(len(night), 5, "应当有若干夜游城市")
        manifest_path = Path(worldmap.PHOTO_DIR) / "photos.json"
        if not manifest_path.exists():
            self.skipTest("还没抓过世界照片")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for c in night:
            entry = manifest.get(c["name"]) or {}
            self.assertTrue(entry.get("night"), f"夜游城市 {c['name']} 缺夜景照")

    def test_loops_reference_known_cities(self):
        loops = worldmap.loops()
        self.assertGreaterEqual(len(loops), 10, "世界环线应当有 10 条以上")
        for item in loops:
            self.assertTrue(item.get("cities"), item.get("name"))
            # 海上航线可以是「母港↔登陆点」两站（乌斯怀亚↔南极半岛），
            # 陆地环线仍然必须 3 站以上，否则「环」不成立
            low = 2 if item.get("mode") == "sea" else 3
            self.assertTrue(low <= len(item["cities"]) <= 12, f"{item['name']} 城市数不合理")
            self.assertEqual(len(item["stops"]), len(item["cities"]), f"{item['name']} 有城市缺失")
            self.assertTrue(item.get("days") and 3 <= item["days"] <= 60, item["name"])
            self.assertTrue(item.get("transport"), f"{item['name']} 缺交通方式")
            self.assertTrue(item.get("blurb"), f"{item['name']} 缺简介")
            self.assertGreater(item["km"], 100, f"{item['name']} 里程异常")

    def test_loops_carry_environment_colours(self):
        """环线颜色跟着环境走：沙漠土黄、海洋深蓝、极地冰蓝 —— 由数据决定，不靠前端随手挑。

        浅色线在地球深色海面上几乎看不见，所以每条环线必须自带 env/env_color，
        前端只负责用；env 又必须落在已登记的色板里，防止写错字悄悄退化成灰色。
        """
        loops = worldmap.loops()
        for item in loops:
            env = item.get("env")
            self.assertIn(env, worldmap.ENV_COLORS, f"{item['name']} 没归类环境：{env}")
            self.assertEqual(item.get("env_color"), worldmap.ENV_COLORS[env], item["name"])
            self.assertTrue(item.get("env_label"), item["name"])

    def test_sea_and_polar_loops_exist(self):
        """用户点名要的两类：海上知名线路 + 南北极圈，必须真在数据里。"""
        loops = worldmap.loops()
        seas = [l for l in loops if l.get("env") == "海洋"]
        polars = [l for l in loops if l.get("env") == "极地"]
        self.assertGreaterEqual(len(seas), 3, "海上线路至少 3 条")
        self.assertGreaterEqual(len(polars), 2, "极地线路至少 2 条")
        self.assertTrue(any(l.get("mode") == "sea" for l in seas), "海上线路要标 mode=sea")
        # 极地里有两条是「母港↔登陆点」的航线（南极半岛、格陵兰冰峡湾），也该标 sea
        polar_sea = [l for l in polars if l.get("mode") == "sea"]
        self.assertGreaterEqual(len(polar_sea), 2, "极地航线也要标 mode=sea")
        self.assertTrue(all(len(l["cities"]) >= 2 for l in polar_sea), "极地航线至少两站")
        polar_names = " ".join(l["name"] for l in polars)
        self.assertTrue(any(k in polar_names for k in ("南极", "北极", "斯瓦尔巴", "格陵兰")),
                        f"极地线路看不出极地：{polar_names}")

    def test_records_are_complete(self):
        records = worldmap.records()
        self.assertGreaterEqual(len(records), 20, "世界之最应当有 20 条以上")
        cats = {r["category"] for r in records}
        self.assertTrue({"自然", "工程", "人文"} <= cats, f"分类缺失：{cats}")
        for r in records:
            for key in ("id", "name", "value", "blurb", "visit", "country", "nearest_city"):
                self.assertTrue(r.get(key), f"{r.get('name')} 缺 {key}")
            self.assertTrue(-90 <= float(r["lat"]) <= 90, r["name"])
            self.assertTrue(-180 <= float(r["lon"]) <= 180, r["name"])

    def test_map_payload_shape(self):
        payload = worldmap.map_payload()
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["cities"]), len(worldmap.cities()))
        self.assertEqual(len(payload["loops"]), len(worldmap.loops()))
        self.assertEqual(len(payload["records"]), len(worldmap.records()))
        self.assertIn("areas", payload["stats"])
        card = payload["cities"][0]
        for key in ("name", "lat", "lon", "photo", "photo_day", "has_guide", "night"):
            self.assertIn(key, card)
        self.assertIn("photo", payload["records"][0])

    def test_cards_expose_lng_for_globe(self):
        """地球渲染读 lng：卡片必须同时给 lon 与 lng，否则点位与卡片全落到 undefined。"""
        payload = worldmap.map_payload()
        for card in payload["cities"]:
            self.assertEqual(card["lng"], card["lon"], f"{card['name']} 的 lng 与 lon 不一致")
            self.assertIsInstance(card["lng"], (int, float), card["name"])
        for rec in payload["records"]:
            self.assertEqual(rec["lng"], rec["lon"], f"{rec['name']} 的 lng 与 lon 不一致")

    def test_globe_frontend_uses_backend_field_names(self):
        """前端不能读一个后端根本不发的经度字段（曾经的 lng/lon 错位事故）。"""
        web = Path(worldmap.DATA_DIR).parents[2] / "web"
        src = (web / "globe3d.js").read_text(encoding="utf-8")
        self.assertNotIn("rec.lng", src, "世界之最的经度字段是 lon")
        self.assertNotIn("c.lng", src, "世界城市的经度字段是 lon")
        self.assertIn("state.data.cityIndex", src)
        # 定位与正反面判定必须成对出现，否则会出现「背面显示、正面消失」
        self.assertIn("function isFront(", src)
        self.assertIn("function worldOf(", src)
        self.assertIn("pointOfView", src)

    def test_no_duplicate_js_declarations(self):
        """前端不许出现同名函数/同一 id —— 后一个声明会**静默覆盖**前一个。

        真出过事故：数据台加维护计划时写了个 `renderPlan(plan)`，
        和单程规划页签的 `renderPlan(res)` 同名，于是「开始规划」一按就报
        `Cannot read properties of undefined (reading 'length')`。
        浏览器不会报错，只有点下去才知道，所以这里静态拦住。
        """
        import collections
        import re

        web = Path(worldmap.DATA_DIR).parents[2] / "web"
        for js in ("app.js", "map.js", "globe3d.js"):
            src = (web / js).read_text(encoding="utf-8")
            decls = re.findall(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", src, re.M)
            dup = [name for name, n in collections.Counter(decls).items() if n > 1]
            self.assertFalse(dup, f"{js} 里有同名函数（会互相覆盖）：{dup}")
            consts = re.findall(
                r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()",
                src, re.M)
            self.assertFalse(set(decls) & set(consts),
                             f"{js} 里函数与 const 撞名：{sorted(set(decls) & set(consts))}")
        html = (web / "index.html").read_text(encoding="utf-8")
        ids = re.findall(r'id="([A-Za-z0-9_-]+)"', html)
        dup_ids = [name for name, n in collections.Counter(ids).items() if n > 1]
        self.assertFalse(dup_ids, f"index.html 里有重复 id：{dup_ids}")

    def test_globe_projection_matches_vendor_convention(self):
        """经纬度→世界坐标必须走 globe.gl 自己的 getCoords。

        以前自己按 `x=R·cosφ·cosθ, z=-R·cosφ·sinθ` 算，和 three-globe 内部的
        极角公式（φ=(90-lat)、θ=(90-lng)）不一致，导致正反面判反、投影全 NaN、
        81 张城市卡片一张都不显示。
        """
        web = Path(worldmap.DATA_DIR).parents[2] / "web"
        src = (web / "globe3d.js").read_text(encoding="utf-8")
        self.assertIn("getCoords", src, "worldOf 应当优先用 globe.gl 的 getCoords")
        self.assertIn("(90 - lat)", src, "兜底公式要用 three-globe 的极角写法")
        self.assertIn("getScreenCoords(size", src, "投影用 getScreenCoords(viewportSize, x, y, z)")
        self.assertNotIn("getScreenCoords(host.clientWidth", src,
                         "别按 (w, h, x, y) 调用：签名是 (viewportSize, x, y, z)")

    def test_web_shell_is_intact_utf8(self):
        """网页外壳必须是无 BOM 的 UTF-8：前端读的 id 一个都不能少（曾因编码事故整页乱码）。"""
        import re

        web = Path(worldmap.DATA_DIR).parents[2] / "web"
        raw = (web / "index.html").read_bytes()
        self.assertNotIn(raw[:3], (b"\xef\xbb\xbf",), "index.html 不要写 BOM")
        html = raw.decode("utf-8")          # 解不出来就说明编码坏了
        self.assertNotIn("锛", html, "index.html 出现了 GBK 乱码")
        self.assertIn("<!DOCTYPE html>", html)
        ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
        for js in ("app.js", "map.js", "globe3d.js"):
            src = (web / js).read_text(encoding="utf-8")
            used = set(re.findall(r'getElementById\(\s*"([A-Za-z0-9_-]+)"', src))
            used |= set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', src))
            dynamic = {"map-loop-detail", "map-search", "globe-loop-detail"}   # 运行时才建的
            missing = sorted(used - ids - dynamic)
            self.assertFalse(missing, f"{js} 读的 id 在 index.html 里不存在：{missing}")

    def test_globe_panels_sit_outside_the_3d_stage(self):
        """地球页签的左右两栏要在 3D 窗口外面（页面白底上），不能压在地球上。"""
        import re

        web = Path(worldmap.DATA_DIR).parents[2] / "web"
        html = (web / "index.html").read_text(encoding="utf-8")
        shell = re.search(r'<div class="globe-shell"[^>]*>(.*?)\n    </div>\n', html, re.S)
        self.assertIsNotNone(shell, "找不到 .globe-shell")
        body = shell.group(1)
        stage = re.search(r'<div class="globe-stage"[^>]*>(.*?)\n      </div>', body, re.S)
        self.assertIsNotNone(stage, "找不到 .globe-stage")
        for pid in ("globe-loops-panel", "globe-records-panel", "globe-picked-panel"):
            self.assertNotIn(pid, stage.group(1), f"{pid} 还在 3D 窗口里面")
            self.assertIn(pid, body, f"{pid} 不在 .globe-shell 里")
        # 地球画布与城市卡必须在 stage 内（卡片坐标相对 #globe-viz 计算）
        self.assertIn("globe-viz", stage.group(1))
        self.assertIn("globe-cards", stage.group(1))

    def test_asset_versions_are_in_sync(self):
        """index.html 里所有 `?v=` 必须同一个号。

        浏览器按 URL 缓存静态文件，哪个文件版本号没提，改完就还是旧的 ——
        本项目真出现过 `style.css?v=57` 与 `app.js?v=58` 并存。
        统一用 `python scripts/bump_assets.py` 归一（取最大 +1）。
        """
        import re

        web = Path(worldmap.DATA_DIR).parents[2] / "web"
        html = (web / "index.html").read_text(encoding="utf-8")
        found = sorted({int(v) for v in re.findall(r"\?v=(\d+)", html)})
        self.assertTrue(found, "index.html 里应当有 ?v= 版本号")
        self.assertEqual(len(found), 1,
                         f"静态资源版本号不一致：{found} —— 跑 python scripts/bump_assets.py")

    def test_env_colour_is_never_used_as_text_colour(self):
        """环境色只能当色块（圆点/边框），不能当字色。

        出过的问题：环线列表的「沙漠/海洋/极地/雪山」标签直接把 env_color 写成 `color:`，
        极地冰蓝 #7fc4dd、雪山灰蓝 #93b0c4、沙漠土黄 #d9a441 在白底上的对比度只有 2 左右，
        用户的原话是「浅色我看不见」。字色固定用深色，颜色靠圆点与边框表达。
        """
        web = Path(worldmap.DATA_DIR).parents[2] / "web"
        import re

        js = (web / "globe3d.js").read_text(encoding="utf-8")
        # 注意别把 border-color 也一起拦了：色块边框用环境色是对的，字色不行
        self.assertFalse(re.search(r"(?<!border-)color:\$\{envColor\}", js),
                         "env_color 不能当字色（白底上看不清）")
        self.assertIn("globeStroke", js, "球面上的线要用 globeStroke 提亮过再画")
        self.assertIn("const envColor = globeStroke(", js, "航线颜色必须走 globeStroke")
        css = (web / "style.css").read_text(encoding="utf-8")
        self.assertIn(".loop-title .tag.env", css, "环境标签要固定字色")
        rule = css.split(".loop-title .tag.env", 1)[1].split("}", 1)[0]
        self.assertIn("color: #14313a", rule, f"环境标签字色必须是深色：{rule.strip()[:80]}")

    def test_city_and_record_detail(self):
        name = worldmap.cities()[0]["name"]
        detail = worldmap.city_detail(name)
        self.assertTrue(detail["ok"])
        self.assertTrue(detail["guide"].get("itinerary"))
        self.assertFalse(worldmap.city_detail("不存在的地方")["ok"])
        rec = worldmap.records()[0]
        rd = worldmap.record_detail(rec["id"])
        self.assertTrue(rd["ok"])
        self.assertEqual(rd["record"]["value"], rec["value"])
        self.assertFalse(worldmap.record_detail("nope")["ok"])


class TestWorldItinerary(unittest.TestCase):
    def test_plan_uses_distances_and_timezones(self):
        if not HAS_CATALOG:
            self.skipTest("还没有生成 world_catalog.json")
        names = [c["name"] for c in worldmap.cities()[:3]]
        res = worldmap.plan_itinerary(names, closed=True)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["order"][0], res["order"][-1], "闭环应回到起点")
        self.assertEqual(len(res["legs"]), 3)
        s = res["summary"]
        self.assertGreater(s["total_km"], 0)
        self.assertGreater(s["flight_hours"], 0)
        self.assertGreaterEqual(s["total_days"], s["visit_days"])
        for leg in res["legs"]:
            self.assertGreater(leg["km"], 0)
            self.assertGreater(leg["flight_hours"], 2.0)   # 含起降与机场时间
        self.assertTrue(res["warnings"])

    def test_plan_rejects_unknown_and_short(self):
        self.assertFalse(worldmap.plan_itinerary(["不存在的地方", "巴黎"])["ok"])
        if HAS_CATALOG:
            self.assertFalse(worldmap.plan_itinerary([worldmap.cities()[0]["name"]])["ok"])

    def test_long_haul_warning(self):
        if not HAS_CATALOG:
            self.skipTest("还没有生成 world_catalog.json")
        by_name = {c["name"]: c for c in worldmap.cities()}
        pair = None
        for a in worldmap.cities():
            for b in worldmap.cities():
                if a["name"] == b["name"]:
                    continue
                if abs(a["lon"] - b["lon"]) > 120 and pair is None:
                    pair = [a["name"], b["name"]]
        if not pair:
            self.skipTest("没有找到跨洲长途组合")
        res = worldmap.plan_itinerary(pair)
        self.assertTrue(res["ok"])
        self.assertTrue(any("飞行超过 10 小时" in w for w in res["warnings"]),
                        f"应当提示长航段：{res['warnings']}")


if __name__ == "__main__":
    unittest.main()
