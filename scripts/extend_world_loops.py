"""给世界环线加「环境色」并补上海上航线与南北极圈（一次性数据整理）。

背景：地球模式里 14 条环线都是陆地走法，用户要「海上知名线路 + 南北极圈」，
并且颜色要**跟着环境走**（沙漠土黄、海洋深蓝、极地冰蓝…），否则浅色在球上看不清。

用法：
    python scripts/extend_world_loops.py --dry-run   # 只看会写什么
    python scripts/extend_world_loops.py            # 写入 world_catalog.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "src" / "travel_planner" / "data" / "world_catalog.json"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

#: 环境 → 色（土黄/深蓝/冰蓝…）。前端按 env 取色给环线圆点与航线着色，
#: 这样「沙漠就是土黄色」是数据说了算，不是随手挑的颜色。
ENV_COLORS = {
    "沙漠": "#d9a441", "海洋": "#2f6f9f", "极地": "#7fc4dd", "草原": "#7fa650",
    "雪山": "#93b0c4", "古迹": "#b07a4a", "雨林": "#2f7d51", "高原": "#a8846b",
    "城市": "#6b7f95",
}

#: 已有 14 条环线各归哪个环境
ENV_OF = {
    "eurasia_classic": "雪山", "europe_west": "城市", "europe_east": "古迹",
    "nordic_aurora": "极地", "mediterranean": "海洋", "silk_road": "沙漠",
    "south_asia": "古迹", "se_asia_islands": "海洋", "japan_golden": "城市",
    "middle_east": "沙漠", "north_america": "城市", "south_america": "高原",
    "africa_safari": "草原", "oceania": "草原",
}

NEW_CITIES = [
    {"name": "乌斯怀亚", "name_en": "Ushuaia", "country": "阿根廷", "area": "南美",
     "lat": -54.8019, "lon": -68.303, "tz": -3, "score": 9.0, "days": 2,
     "best_season": "11-3月（南极季）", "tags": ["南极门户", "火地岛", "世界尽头"],
     "night": False,
     "intro": "地球最南端的城市，比格尔海峡边的彩色小城。去南极的船几乎都从这里出发，"
              "火地岛国家公园、世界尽头灯塔与企鹅岛是它的三张名片。",
     "halfday": "上午火地岛国家公园半日游（小火车 + 海岸步道），回城逛圣马丁大街，"
                "傍晚在比格尔海峡边等落日。",
     "oneday": "上午去企鹅岛或海狮岛，下午火地岛国家公园，晚上在港口看南极船出港。"},
    {"name": "朗伊尔城", "name_en": "Longyearbyen", "country": "挪威", "area": "北欧",
     "lat": 78.2232, "lon": 15.6267, "tz": 1, "score": 8.8, "days": 3,
     "best_season": "6-8月（午夜太阳）/ 2-4月（雪地摩托与极光）", "tags": ["北极", "斯瓦尔巴", "午夜太阳"],
     "night": False,
     "intro": "世界最北的常住城镇，出城就要带信号枪的北极熊领地。夏天是午夜太阳，"
              "冬天是极光与雪地摩托。5-8 月邮轮季，港口每天进出南北极船。",
     "halfday": "上午全球种子库外景 + 斯瓦尔巴博物馆（了解矿业史与北极熊守则），"
                "下午坐船看冰川前沿与海鸟崖。",
     "oneday": "跟当地团去 Pyramiden 或 Barentsburg 废弃矿城，晚上在营地等午夜太阳。"},
    {"name": "罗瓦涅米", "name_en": "Rovaniemi", "country": "芬兰", "area": "北欧",
     "lat": 66.5039, "lon": 25.7294, "tz": 2, "score": 8.6, "days": 3,
     "best_season": "12-3月（雪与极光）/ 6-7月（午夜太阳）", "tags": ["北极圈", "极光", "圣诞老人村"],
     "night": False,
     "intro": "北极圈正好穿城而过。圣诞老人村在城北 8 公里，跨圈证书与北纬 66°33′ 的标线都在那里；"
              "秋天有极光，冬天有雪橇与冰钓，夏天有午夜太阳。",
     "halfday": "圣诞老人村半日：跨北极圈证书、邮局寄明信片、雪橇体验；"
                "回城去 Arktikum 博物馆看拉普兰与北极圈气候展。",
     "oneday": "白天雪地摩托或哈士奇雪橇穿越森林，晚上跟极光团出城（城里有光污染）。"},
    {"name": "摩尔曼斯克", "name_en": "Murmansk", "country": "俄罗斯", "area": "北欧",
     "lat": 68.9585, "lon": 33.0827, "tz": 3, "score": 8.4, "days": 3,
     "best_season": "12-3月（极夜与极光）/ 5-7月（极昼）", "tags": ["北极圈", "不冻港", "极昼极夜"],
     "night": False,
     "intro": "北极圈内的不冻港与苏联北方舰队母港，12 月到 1 月是极夜，5 月到 7 月是极昼。"
              "极光季（9-3 月）出城 30 公里就能看到相当稳定的极光。",
     "halfday": "阿廖沙纪念碑俯瞰港口，列宁号核动力破冰船（开放时上船参观），"
                "晚上出城追极光。",
     "oneday": "捷里别尔卡（Teriberka）一日：北冰洋岸边、废弃渔船与极夜天色，"
              "电影《利维坦》就是在这里取的景。"},
    {"name": "南极半岛", "name_en": "Antarctic Peninsula", "country": "南极洲", "area": "极地",
     "lat": -64.8167, "lon": -62.8667, "tz": -3, "score": 9.6, "days": 10,
     "best_season": "11-3月（南极夏季，12-1月是旺季）", "tags": ["南极", "企鹅", "冰川"],
     "night": False,
     "intro": "从乌斯怀亚穿越德雷克海峡两昼夜后抵达的南极半岛，登陆点多在尼科港、天堂湾一带。"
              "1-2 月是企鹅育雏与鲸群最活跃的时候，冰况最友好。",
     "halfday": "一天的登陆通常两次：上午冲锋舟绕冰山看海豹与企鹅，下午登陆徒步看苔原与科考站遗址。",
     "oneday": "连续登陆日：天堂湾看冰川崩解、尼科港看巴布亚企鹅巢，晚上船上看落日与鲸尾。"},
    {"name": "努克", "name_en": "Nuuk", "country": "格陵兰（丹麦）", "area": "北欧",
     "lat": 64.1836, "lon": -51.7214, "tz": -3, "score": 8.2, "days": 3,
     "best_season": "6-9月（冰峡湾无冰期）/ 12-3月（极光）", "tags": ["格陵兰", "冰峡湾", "极光"],
     "night": False,
     "intro": "格陵兰首府，世界最小首都之一。旧港区彩色木屋与因纽特文化是它的样子，"
              "夏天可以坐船进 Nuuk 冰峡湾看浮冰，冬天极光频率很高。",
     "halfday": "旧港区与格陵兰国家博物馆（木乃伊与皮划艇收藏），下午冰峡湾游船。",
     "oneday": "冰峡湾一日船 + 徒步：看冰山、座头鲸与因纽特猎场遗址。"},
]

NEW_LOOPS = [
    {"id": "mediterranean_cruise", "name": "地中海邮轮环线", "env": "海洋",
     "subtitle": "巴塞罗那 → 罗马 → 威尼斯 → 杜布罗夫尼克 → 雅典 → 圣托里尼 → 巴塞罗那",
     "cities": ["巴塞罗那", "罗马", "威尼斯", "杜布罗夫尼克", "雅典", "圣托里尼"],
     "days": 14, "season": "5-9月", "area": "欧洲", "transport": "邮轮（西地中海 + 亚得里亚海 + 爱琴海航线）",
     "cross_region": False, "order": 15, "plan_mode": "sea", "regions": ["欧洲"],
     "blurb": "一条船把西地中海、亚得里亚海与爱琴海串成一个环：白天靠港下船玩，晚上睡觉赶路，"
              "省掉反复打包与机场往返。罗马、威尼斯、杜布罗夫尼克、圣托里尼都是邮轮母港，"
              "上船前记得把每天的靠港时间与回船时间对上。",
     "tips": ["邮轮价格按舱位与提前量差很多，内舱早鸟常比机票 + 酒店更省；船上另收服务费",
              "靠港时间有限（通常 6-10 小时），旺季杜布罗夫尼克与圣托里尼要提前订接驳或缆车",
              "威尼斯、巴塞罗那等老港到市区需要接驳船或大巴，别把时间排在刚靠港那半小时",
              "申根多次签证 + 邮轮登船证，克罗地亚与希腊都在申根区（克罗地亚 2023 年入区）"]},
    {"id": "baltic_fjord", "name": "波罗的海与峡湾邮轮环线", "env": "海洋",
     "subtitle": "哥本哈根 → 斯德哥尔摩 → 赫尔辛基 → 雷克雅未克 → 哥本哈根",
     "cities": ["哥本哈根", "斯德哥尔摩", "赫尔辛基", "雷克雅未克"],
     "days": 12, "season": "6-8月", "area": "欧洲", "transport": "波罗的海邮轮 + 北大西洋段",
     "cross_region": False, "order": 16, "plan_mode": "sea", "regions": ["欧洲"],
     "blurb": "波罗的海三都连成一段，再跨北大西洋到冰岛绕一圈回国。夏天这一带几乎没有黑夜，"
              "傍晚靠港还能当白天用，峡湾与熔岩地貌是这条线独有的窗景。",
     "tips": ["雷克雅未克与波罗的海之间是两段开阔海，晕船体质请备药并选中低层舱位",
              "6-8 月是午夜太阳季，夜里 11 点还天亮，靠港行程可以排到很晚",
              "赫尔辛基可顺带一日往返塔林（渡轮约 2 小时），但要看邮轮靠港时间够不够",
              "冰岛段天气说变就变，防风防水外层比厚衣服更重要"]},
    {"id": "south_pacific_island", "name": "南太平洋跳岛环线", "env": "海洋",
     "subtitle": "悉尼 → 奥克兰 → 楠迪 → 凯恩斯 → 悉尼",
     "cities": ["悉尼", "奥克兰", "楠迪", "凯恩斯"],
     "days": 21, "season": "4-10月", "area": "大洋洲", "transport": "航班 + 岛间渡轮 + 大堡礁船",
     "cross_region": False, "order": 17, "plan_mode": "sea", "regions": ["大洋洲"],
     "blurb": "把南太平洋的三种海放在一条环上：悉尼的港口与海滩、奥克兰的火山与帆船湾、"
              "斐济楠迪的珊瑚礁、凯恩斯的大堡礁与雨林。整条线靠航班串联，段间都是 3-4 小时航程。",
     "tips": ["斐济对中国免签，但岛间交通以渡轮与小飞机为主，行程要留缓冲",
              "大堡礁外礁船期受风浪影响大，建议把出海排在行程前段，留出改期余地",
              "南半球季节与国内相反：4-10 月是这里的干季，水温也最舒服",
              "楠迪入境需回程票与住宿证明，落地再订容易卡关"]},
    {"id": "antarctic_peninsula", "name": "南极半岛航线", "env": "极地",
     "subtitle": "乌斯怀亚 → 德雷克海峡 → 南极半岛 → 乌斯怀亚",
     "cities": ["乌斯怀亚", "南极半岛"],
     "days": 12, "season": "11-3月（12-1月最旺）", "area": "南极", "transport": "极地探险船（乌斯怀亚登船）",
     "cross_region": True, "order": 18, "plan_mode": "sea", "regions": ["南美", "极地"],
     "blurb": "从世界尽头的乌斯怀亚登船，穿过两天两夜的德雷克海峡抵达南极半岛，"
              "之后每天两次登陆：看企鹅育雏、冰山崩解与鲸群。这是最经典也最省时的南极走法。",
     "tips": ["去南极没有个人自由行，只能买探险船船票；提前 3-6 个月订，最后一分钟票便宜但要能等",
              "德雷克海峡以颠簸出名，晕船药在登船前 1 小时吃；船公司多会提供晕船贴",
              "《南极条约》要求登陆前刷靴消毒、与动物保持 5 米以上距离，别带任何食物下船",
              "保险必须含「极地救援与医疗后送」，普通旅行险不覆盖"]},
    {"id": "arctic_svalbard", "name": "北极圈·斯瓦尔巴与极光带", "env": "极地",
     "subtitle": "朗伊尔城 → 摩尔曼斯克 → 罗瓦涅米 → 赫尔辛基",
     "cities": ["朗伊尔城", "摩尔曼斯克", "罗瓦涅米", "赫尔辛基"],
     "days": 10, "season": "6-8月（午夜太阳）/ 12-3月（极光）", "area": "北欧",
     "transport": "航班 + 北冰洋邮轮段 + 极地列车",
     "cross_region": False, "order": 19, "plan_mode": "sea", "regions": ["北欧", "极地"],
     "blurb": "从世界最北的常住城镇朗伊尔城往下走：摩尔曼斯克的不冻港与极夜、"
              "罗瓦涅米的北极圈标线、赫尔辛基收尾。夏天看午夜太阳，冬天追极光，同一组城市两种玩法。",
     "tips": ["朗伊尔城出城必须跟向导并携带防护（北极熊领地），不要独自走出镇界",
              "极光季（9-3 月）看天气比看预报重要：要晴天 + 地磁活动，KP 值只是参考",
              "摩尔曼斯克与罗瓦涅米之间可选极地列车或航班，冬季航班常因除冰延误",
              "中国公民去斯瓦尔巴虽属挪威、但按申根入境规则办签，需多次或两次申根签"]},
    {"id": "greenland_icefjord", "name": "格陵兰冰峡湾环线", "env": "极地",
     "subtitle": "雷克雅未克 → 努克 → 冰峡湾 → 雷克雅未克",
     "cities": ["雷克雅未克", "努克"],
     "days": 8, "season": "6-9月", "area": "北欧", "transport": "航班 + 冰峡湾游船",
     "cross_region": False, "order": 20, "plan_mode": "sea", "regions": ["北欧", "极地"],
     "blurb": "从冰岛飞到格陵兰首府努克，进一次世界流速最快的冰峡湾：冰山从冰川崩落、"
              "被峡湾挤成一片冰原。回程回冰岛补黄金圈与蓝湖，一条线看两种火山与冰的对话。",
     "tips": ["努克进出主要靠航班，冰岛雷克雅未克是国内转机最方便的一跳，行李额度要单独买",
              "冰峡湾游船受冰况影响会改期，行程后段留一天缓冲",
              "格陵兰不属于申根区但需要申根签或格陵兰签证，出发前在丹麦签证中心确认",
              "6-9 月才有稳定船期，冬季努克周边以狗拉雪橇与极光为主"]},
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="给世界环线加环境色并补海上/极地环线")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    cities = data.get("cities") or []
    loops = data.get("loops") or []
    have_cities = {c.get("name") for c in cities}
    have_loops = {l.get("id") for l in loops}

    added_cities = [c for c in NEW_CITIES if c["name"] not in have_cities]
    added_loops = [l for l in NEW_LOOPS if l["id"] not in have_loops]
    for loop in loops:
        if loop.get("id") in ENV_OF and not loop.get("env"):
            loop["env"] = ENV_OF[loop["id"]]
    missing_env = [l["id"] for l in loops if not l.get("env")]

    print(f"城市：现 {len(cities)} 个，新增 {len(added_cities)} 个 → "
          f"{'、'.join(c['name'] for c in added_cities) or '（无）'}")
    print(f"环线：现 {len(loops)} 条，新增 {len(added_loops)} 条 → "
          f"{'、'.join(l['name'] for l in added_loops) or '（无）'}")
    print("已有环线的环境色：" + "、".join(f"{l['name']}={l.get('env')}" for l in loops[:6]) + " …")
    if missing_env:
        print("⚠ 还没归类的环线：" + "、".join(missing_env), file=sys.stderr)
    if args.dry_run:
        print("（dry-run：没写文件）")
        return 0

    data["cities"] = cities + added_cities
    data["loops"] = loops + added_loops
    data["env_colors"] = ENV_COLORS
    CATALOG.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"✓ 已写入 {CATALOG.relative_to(ROOT)}："
          f"{len(data['cities'])} 城 / {len(data['loops'])} 环线")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
