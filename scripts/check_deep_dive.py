# -*- coding: utf-8 -*-
"""自查：城市深度攻略 zz_deep_dive.json

检查项
  1. 12 个城市都在
  2. 每城 days 与 itinerary 条数一致（且 D1-D5 齐、序号连续、days==5）
  3. 每天 detail 里至少 3 个「地名候选」——连续 3 个以上汉字、且不是常用动词/时间/动作短语
  4. 字段齐全：summary/itinerary/transport/stay/eat/tips/budget
  5. 参考（不算失败）：这些名字能不能对上 poi.db 的 POI 池

地名候选怎么切：先把 detail 按标点切成段，再在「动词/介词/时间词」处断开，
剩下连续的 3+ 汉字才算候选 —— 这样「夫子庙大成殿与乌衣巷」会被切成
「夫子庙大成殿」「乌衣巷」两条，而不是整串当一个地名。
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "data" / "city_guides" / "zz_deep_dive.json"
TARGETS = ["北京", "上海", "西安", "成都", "杭州", "南京", "重庆", "广州", "昆明", "长沙", "青岛", "厦门"]
FIELDS = ["summary", "itinerary", "transport", "stay", "eat", "tips", "budget"]

#: 动作/介词/时间/连词：在这些字处断开，剩下的连续汉字块才是地名候选
SPLIT_CHARS = set(
    "的了在到从往向与和及或转乘坐走逛看买吃吃喝住去来上去下进出过登上游览"
    "先再又然后接最后傍晚晚上中午下午上午凌晨清晨夜里当天今日次第每约需"
    "有无是很都就也还可要不别请让把被给为以及等会能想要多少几"
    "点分时天日月年号口站段次元人公里米步"
)
#: 非地名的整词（出现即丢弃）
STOP_TOKENS = {
    "小时", "分钟", "建议", "提醒", "提示", "门票", "免费", "预约", "开放时间",
    "身份证", "官方公告", "官方渠道", "含观光车", "不排队", "人少", "人多",
    "步行", "打车", "地铁", "公交", "换乘", "行程", "结束", "收尾", "顺路",
    "上午", "中午", "下午", "傍晚", "晚上", "清晨", "夜里", "当天", "今日",
    "早上", "早饭", "午饭", "晚饭", "夜宵", "早点", "时间", "价格", "费用",
    "住宿", "吃饭", "午饭前", "人均", "旺季", "淡季", "节假日", "周末",
    "回酒店", "回市区", "回城", "返程", "出发", "提前", "左右", "大约",
    "计划", "安排", "体验", "值得", "推荐", "注意", "千万", "如果", "因为",
    "所以", "但是", "而且", "另外", "可以", "需要", "不要", "记得", "务必",
    "全程", "全天", "半天", "整天", "第一天", "第二天", "第三天", "第四天", "第五天",
    "接下来", "晚上回", "中午在", "早上到", "下午到", "上午到",
}
#: 尾部这些词说明这块是动作短语而不是地名
BAD_TAIL = ("约", "元", "小时", "分钟", "出发", "结束", "收尾", "吃饭", "午饭", "晚饭",
            "夜宵", "入住", "返回", "回", "走", "逛", "看", "买", "吃", "喝", "玩",
            "排队", "预约", "门票", "左右", "大约", "为准", "更好", "方便", "最集中")


def segments(detail: str):
    """按标点切段 → 再在动作字处断开 → 输出 3+ 汉字的候选。"""
    flat = str(detail or "").replace(" ", "")
    out = []
    for part in re.split(r"[，。；、：（）()「」《》!？?~—\-/]", flat):
        buf = []
        for ch in part:
            if "\u4e00" <= ch <= "\u9fa5" and ch not in SPLIT_CHARS:
                buf.append(ch)
            else:
                if len(buf) >= 3:
                    out.append("".join(buf))
                buf = []
        if len(buf) >= 3:
            out.append("".join(buf))
    cleaned = []
    for tok in out:
        if tok in STOP_TOKENS:
            continue
        if len(tok) > 12:                       # 太长说明没切开，不是单个地名
            continue
        if tok.endswith(BAD_TAIL):
            continue
        if re.search(r"(小时|分钟|门票|预约|免费|人均|左右|以官方)", tok):
            continue
        cleaned.append(tok)
    # 去重但保留出现顺序
    seen, uniq = set(), []
    for t in cleaned:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    guides = data.get("guides") or {}
    fails, warns = [], []

    for key in ("updated", "collector", "note"):
        if not data.get(key):
            fails.append("顶层缺字段 %s" % key)
    for city in TARGETS:
        if city not in guides:
            fails.append("缺城市 %s" % city)
    extra = [c for c in guides if c not in TARGETS]
    if extra:
        warns.append("多余城市 %s" % extra)

    conn = None
    if (ROOT / "data" / "poi.db").exists():
        conn = sqlite3.connect(str(ROOT / "data" / "poi.db"))
        conn.row_factory = sqlite3.Row

    print("=" * 84)
    print("城市深度攻略自查：%s" % PATH.relative_to(ROOT))
    print("=" * 84)
    print("%-6s %-6s %-10s %-26s %s" % ("城市", "days", "itinerary", "每天地名候选数", "POI 池命中"))
    print("-" * 84)

    thin = []
    for city in TARGETS:
        g = guides.get(city)
        if not g:
            continue
        for f in FIELDS:
            if f not in g or g[f] in ("", None, []):
                fails.append("%s 缺字段 %s" % (city, f))
        it = g.get("itinerary") or []
        if g.get("days") != len(it):
            fails.append("%s days=%s 但 itinerary 有 %d 条" % (city, g.get("days"), len(it)))
        want = ["D%d" % i for i in range(1, len(it) + 1)]
        got = [d.get("day") for d in it]
        if got != want:
            fails.append("%s 日期序列不符：%s" % (city, got))
        if g.get("days") != 5:
            fails.append("%s days 不是 5（是 %s）" % (city, g.get("days")))
        if not isinstance(g.get("tips"), list) or len(g.get("tips") or []) < 2:
            fails.append("%s tips 太少" % city)

        counts = []
        for d in it:
            detail = str(d.get("detail") or "")
            if not d.get("title"):
                fails.append("%s %s 缺 title" % (city, d.get("day")))
            cands = segments(detail)
            counts.append(len(cands))
            if len(cands) < 3:
                fails.append("%s %s 地名候选只有 %d 个：%s"
                             % (city, d.get("day"), len(cands), cands))
                thin.append("%s %s" % (city, d.get("day")))
            if len(detail) < 120:
                warns.append("%s %s detail 偏短（%d 字）" % (city, d.get("day"), len(detail)))

        home = "-"
        if conn is not None:
            rows = list(conn.execute("SELECT name FROM city_poi_pool WHERE city=?", (city,))) + \
                   list(conn.execute("SELECT poi_name AS name FROM city_poi "
                                     "WHERE city=? AND status='ok'", (city,)))
            names = {str(r["name"] or "") for r in rows if r["name"]}
            names = {n.split("(")[0].split("（")[0] for n in names if n}
            allt = "".join(str(d.get("detail") or "") for d in it)
            hitn = sum(1 for n in names if n and n in allt)
            home = "%d/%d" % (hitn, len(names))
        print("%-6s %-6s %-10s %-26s %s" % (
            city, g.get("days"), len(it), "/".join(str(c) for c in counts), home))

    if conn is not None:
        conn.close()

    print("-" * 84)
    print("城市数 %d · 总天数 %d" % (len(guides), sum(len(g.get("itinerary") or []) for g in guides.values())))
    if thin:
        print("地名候选不足 3 的天：%s" % "、".join(thin))
    if warns:
        print("\n提示（%d 条）：" % len(warns))
        for w in warns[:20]:
            print("  ·", w)
    if fails:
        print("\n[X] 不合格（%d 条）：" % len(fails))
        for f in fails:
            print("  !", f)
        return 1
    print("\n[OK] 全部通过：12 城齐、每城 5 天、字段齐全、每天地名候选 >= 3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
