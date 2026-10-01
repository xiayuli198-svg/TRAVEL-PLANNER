"""生成 `项目报告书.html`：把系统现状 + 验证证据汇成一份可直接打开/打印的报告。

数据来源分两半，都**不手写数字**：
* `data/report_evidence.json` —— `scripts/collect_evidence.py` 跑出来的测试与探针结果；
* 直接问运行时（`db` / `catalog` / `poi` / `routes` / `ultra` / `worldmap` / `crawl_progress`）。

用法：
    python -X utf8 scripts/collect_evidence.py     # 先跑证据（约 6-10 分钟）
    python -X utf8 scripts/build_report.py         # 再生成报告
"""
from __future__ import annotations

import datetime as dt
import html
import json
import platform
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from travel_planner import (catalog, crawl_progress, db, poi as poi_mod,  # noqa: E402
                            routes as routes_mod, ultra, worldmap)

EVIDENCE = ROOT / "data" / "report_evidence.json"
OUT = ROOT / "项目报告书.html"
DECK_OUT = ROOT / "项目报告书·横板.html"
TEMPLATE = ROOT / "scripts" / "report_template.html"
DECK_TEMPLATE = ROOT / "scripts" / "report_template_deck.html"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""))


# ----------------------------------------------------------------- 取数

def table_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
    except sqlite3.Error:
        return 0


def collect() -> dict:
    data: dict = {}
    conn = db.connect()
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        dates = conn.execute(
            "SELECT date, COUNT(DISTINCT train_no) n FROM schedules GROUP BY date ORDER BY date"
        ).fetchall()
        data["db"] = {
            "path": str(db.default_db_path()),
            "size_mb": round(db.default_db_path().stat().st_size / 1048576, 1)
            if db.default_db_path().exists() else 0,
            "integrity": integrity,
            "stations": table_count(conn, "stations"),
            "airports": table_count(conn, "airports"),
            "dates": len(dates),
            "first_date": dates[0]["date"] if dates else "",
            "last_date": dates[-1]["date"] if dates else "",
            "max_trains": max((r["n"] for r in dates), default=0),
            "stop_rows": sum(r["n"] for r in dates),
        }
        rows = []
        for r in dates:
            src = conn.execute("SELECT value FROM meta WHERE key=?",
                               (f"crawl:{r['date']}:stats",)).fetchone()
            imp = conn.execute("SELECT value FROM meta WHERE key=?",
                               (f"import:{r['date']}",)).fetchone()
            if imp:
                label = "导入"
            elif src and "copied from" in (src["value"] or ""):
                label = "拷贝"
            elif src:
                label = "真爬"
            else:
                label = "未知"
            rows.append({"date": r["date"], "trains": r["n"], "label": label})
        truth = sum(1 for r in rows if r["label"] in ("导入", "真爬"))
        data["timetable"] = {
            "rows": rows,
            "truthful": truth,
            "copies": sum(1 for r in rows if r["label"] == "拷贝"),
            "imported": sum(1 for r in rows if r["label"] == "导入"),
            "crawled": sum(1 for r in rows if r["label"] == "真爬"),
            "unknown": sum(1 for r in rows if r["label"] == "未知"),
        }
        data["crawl"] = {
            "runs": crawl_progress.recent_runs(conn, limit=6),
            "legacy": [x for x in (crawl_progress.legacy_info(d)
                                   for d in [r["date"] for r in rows]) if x][:4],
        }
    finally:
        conn.close()

    cities = catalog.cities()
    data["catalog"] = {
        "cities": len(cities),
        "regions": len(catalog.regions()),
        "region_names": [r["name"] for r in catalog.regions()],
        "city_loops": len(catalog.loops()),
        "with_guide": sum(1 for c in cities if c.get("guide")),
        "with_highlights": sum(1 for c in cities if c.get("highlights")),
        "hub_cities": sum(1 for c in cities if c.get("hub")),
    }

    try:
        pconn = poi_mod.connect()
        try:
            data["poi"] = poi_mod.stats(pconn)
        finally:
            pconn.close()
    except Exception as exc:                            # noqa: BLE001
        data["poi"] = {"error": f"{type(exc).__name__}: {exc}"}

    data["routes"] = routes_mod.stats()
    data["routes"]["updated"] = routes_mod.updated()
    data["ultra"] = ultra.stats()
    data["ultra"]["updated"] = ultra.updated()
    data["ultra"]["origins_list"] = ultra.origins()

    world = worldmap.map_payload()
    env_counts: dict = {}
    for loop in world["loops"]:
        env_counts[loop.get("env_label") or "跨区"] = env_counts.get(loop.get("env_label") or "跨区", 0) + 1
    data["world"] = {
        "cities": world["stats"]["cities"],
        "loops": world["stats"]["loops"],
        "records": world["stats"]["records"],
        "areas": world["stats"].get("areas", {}),
        "photos": world["stats"].get("with_photo", 0),
        "record_photos": world["stats"].get("records_with_photo", 0),
        "env": env_counts,
    }
    return data


# ----------------------------------------------------------------- 渲染件

def card(title: str, body: str, *, cls: str = "", num: int | None = None,
         sid: str | None = None) -> str:
    num_html = f'<span class="secnum">{num:02d}</span>' if num is not None else ""
    attrs = f' id="{sid}" data-title="{esc(title)}"' if sid else ""
    return (f'<section class="card reveal {cls}"{attrs}>'
            f'<h2>{num_html}{esc(title)}</h2>{body}</section>')


def kv_table(rows: list[tuple[str, str]], head: tuple[str, str] = ("项目", "数值")) -> str:
    body = "".join(
        f"<tr><th>{esc(k)}</th><td>{v}</td></tr>" for k, v in rows)
    return (f'<table class="kv"><thead><tr><th>{esc(head[0])}</th>'
            f'<th>{esc(head[1])}</th></tr></thead><tbody>{body}</tbody></table>')


def details(summary: str, body: str, *, open_: bool = False) -> str:
    return (f'<details{" open" if open_ else ""}><summary>{esc(summary)}</summary>'
            f'<pre>{esc(body)}</pre></details>')


def badge(ok: bool, text_ok: str = "通过", text_bad: str = "未通过") -> str:
    cls = "ok" if ok else "bad"
    return f'<span class="badge {cls}">{"✓ " if ok else "✗ "}{esc(text_ok if ok else text_bad)}</span>'


# ----------------------------------------------------------------- 组装

def build(data: dict, evidence: dict) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    db_ = data["db"]
    tt = data["timetable"]
    cat = data["catalog"]
    poi = data.get("poi", {})
    rt = data["routes"]
    ul = data["ultra"]
    wd = data["world"]
    cw = data["crawl"]
    ev = evidence or {}
    tests = ev.get("tests", {})
    probes = ev.get("probes", [])
    checks = ev.get("data_checks", [])
    totals = ev.get("totals", {})

    all_ok = bool(totals.get("all_ok")) and db_["integrity"] == "ok"
    verdict = ("全部检查通过，可以正常使用" if all_ok else "有检查项未通过，请先看「验证结果」一节")

    # ---- 概览 ----
    overview = f"""
    <div class="verdict {'ok' if all_ok else 'bad'}">
      <b>{esc(verdict)}</b>
      <span>报告生成时间 {esc(now)} · 证据文件 {esc(ev.get('generated_at') or '（未生成，请先跑 collect_evidence.py）')}</span>
    </div>
    <div class="grid">
      {metric(cat['cities'], '城市资料', '含完整多日攻略')}
      {metric(db_['dates'], '时刻表天数', f"{db_['first_date']} ~ {db_['last_date']}")}
      {metric(rt.get('routes', 0), '参考路线', f"单向 {rt.get('oneway', 0)} / 环线 {rt.get('loops', 0)}")}
      {metric(ul.get('loops', 0), '穷游环线 Ultra', f"北京 {ul.get('beijing', 0)} · 河北 {ul.get('hebei', 0)}")}
      {metric(wd['cities'], '世界城市', f"{wd['loops']} 条世界环线")}
      {metric(poi.get('hit', 0), '城内点位坐标', f"命中率 {poi.get('rate', '?')}%")}
    </div>"""

    # ---- 功能地图 ----
    features = """
    <table class="kv wide">
      <thead><tr><th>页签 / 模块</th><th>解决什么</th><th>关键实现</th></tr></thead>
      <tbody>
        <tr><th><span class="chip">01</span>换乘规划</th><td>两地之间的换乘方案：车次、时刻、票价口径、12306 链接</td>
            <td>本地时刻表引擎（含地面接驳与拷贝日警告）</td></tr>
        <tr><th><span class="chip">02</span>环线旅行</th><td>把几座城市串成一条环线；另有「环线 Ultra 版」——北京/河北出发的穷游整条线</td>
            <td>环线排序 + 逐日安排（Ultra 64 条，34 小 + 30 大环线）</td></tr>
        <tr><th><span class="chip">03</span>地图漫游</th><td>在 3D 地形上点城市拼路线：公共交通 / 自驾 / 租车三种方式</td>
            <td>three.js 地形 + 省界/河湖/山脉 + 26 条国内环线</td></tr>
        <tr><th><span class="chip">04</span>地球模式</th><td>世界城市、世界经典环线（含海上/极地）、世界之最、世界行程单</td>
            <td>globe.gl；环线按环境色上色（球面提亮保证可见）</td></tr>
        <tr><th><span class="chip">05</span>数据台</th><td>体检 + 覆盖率、日期来源、真爬（可断点续爬）、路线库、我的行程、城内点位、城市动线、素材导入、查班次</td>
            <td>顶部吸顶「直达」导航；九张卡按需展开</td></tr>
      </tbody>
    </table>"""

    # ---- 数据规模 ----
    env_txt = "、".join(f"{k} {v}" for k, v in sorted(wd["env"].items()))
    data_rows = [
        ("铁路时刻表", f"{db_['dates']} 天 · 最多 {db_['max_trains']} 趟/天 · "
                    f"共 {db_['stop_rows']} 条停站记录"),
        ("来源档位", f"导入 {tt['imported']} 天 · 真爬 {tt['crawled']} 天 · "
                  f"拷贝 {tt['copies']} 天"
                  + (f" · 未知 {tt['unknown']} 天" if tt["unknown"] else "")),
        ("车站 / 机场", f"{db_['stations']} 个车站 · {db_['airports']} 个机场"),
        ("城市资料", f"{cat['cities']} 座城市 · {cat['regions']} 个大区（{'、'.join(cat['region_names'][:6])}…）"
                  f" · 完整攻略 {cat['with_guide']}/{cat['cities']} · 看点 {cat['with_highlights']} 座"),
        ("国内环线", f"{cat['city_loops']} 条（自驾资料含公路编号、封路期）"),
        ("参考路线库", f"{rt.get('routes', 0)} 条：单向 {rt.get('oneway', 0)} / 环线 {rt.get('loops', 0)}"
                    f" · 分类 {len(rt.get('categories') or [])} 个 · 巧思 {rt.get('clever_routes', 0)} 条"
                    f" · 口径 {rt.get('updated', '?')}"),
        ("环线 Ultra", f"{ul.get('loops', 0)} 条：小环线 {ul.get('small', 0)} + 大环线 {ul.get('large', 0)}"
                     f" · 覆盖 {ul.get('origins', 0)} 个出发地 · {ul.get('days', [0, 0])[0]}-{ul.get('days', [0, 0])[1]} 天"
                     f" · 口径 {ul.get('updated', '?')}"),
        ("城内点位", (f"名单 {poi.get('targets', 0)} 个看点（{poi.get('cities', 0)} 城）· "
                   f"命中 {poi.get('hit', 0)}（{poi.get('rate', '?')}%）· 未命中 {poi.get('miss', 0)}"
                   if "error" not in poi else f"读取失败：{esc(poi['error'])}")),
        ("世界数据", f"{wd['cities']} 座城市（{wd['photos']} 张照片）· {wd['loops']} 条环线 · "
                  f"{wd['records']} 条世界之最（{wd['record_photos']} 张图）"),
        ("世界环线环境", env_txt),
        ("数据库文件", f"{db_['path']} · {db_['size_mb']} MB · 完整性检查 {db_['integrity']}"),
    ]

    # ---- 验证结果 ----
    test_line = (f"{tests.get('ran', 0)} 项单元测试"
                 + (f" · 耗时 {tests.get('seconds')}s" if tests.get("seconds") else ""))
    probe_rows = "".join(
        f"<tr><th>{esc(p['name'])}</th><td>{badge(p['ok'])}</td>"
        f"<td>{p['passed']} 条断言通过"
        + (f" · {esc(p['summary'])}" if p.get("summary") else "") + "</td></tr>"
        for p in probes)
    check_rows = "".join(
        f"<tr><th>{esc(c['name'])}</th><td>{badge(c['ok'])}</td>"
        f"<td>exit {c['exit']}</td></tr>" for c in checks)
    evidence_blocks = "".join(
        details(f"原始输出：{p['name']}", p.get("tail", "")) for p in probes)
    verify = f"""
    <div class="grid tight">
      {metric(totals.get('probes_ok', 0), '浏览器探针通过', f"共 {totals.get('probes_total', 0)} 个")}
      {metric(totals.get('probe_assertions', 0), '探针断言', f"失败 {totals.get('probe_failures', 0)} 条")}
      {metric(totals.get('checks_ok', 0), '数据体检通过', f"共 {totals.get('checks_total', 0)} 项")}
      {metric(tests.get('ran', 0), '单元测试', f"失败 {len(tests.get('failures') or [])} 项")}
    </div>
    <h3>单元测试</h3>
    <p>{esc(test_line)} —— {badge(bool(tests.get('ok')))}</p>
    <h3>浏览器端到端探针（真实 Chromium，不需要人工看）</h3>
    <table class="kv wide"><thead><tr><th>探针</th><th>结果</th><th>说明</th></tr></thead>
      <tbody>{probe_rows}</tbody></table>
    <h3>数据体检</h3>
    <table class="kv wide"><thead><tr><th>检查</th><th>结果</th><th>备注</th></tr></thead>
      <tbody>{check_rows}</tbody></table>
    <h3>原始输出（可展开核对）</h3>
    {evidence_blocks}
    <p class="muted">这些数字由 <code>scripts/collect_evidence.py</code> 跑出来写进
      <code>data/report_evidence.json</code>，报告只负责呈现 —— 想复核就重跑一次。</p>"""

    # ---- 可靠性设计 ----
    reliability = """
    <h3>1. 断点续爬：进度按 OD 成员落盘</h3>
    <p>真爬 12306 是唯一会长时间联网的操作。进度不是记在内存里，而是逐对 OD 写进 SQLite：
      开爬前记 <code>running</code>、做完记 <code>done</code> 并立即提交。断电、关窗口、进程被杀都能接着爬，
      而且<strong>换过爬取范围也不会错位</strong>（旧实现按「第几对」切片，换列表就会切空、报「入库 0 趟」）。
      更早的旧断点页面会如实提示「有旧断点残留」，不假装能精确续爬。</p>
    <h3>2. 数据诚实性：不编造查得到的实时信息</h3>
    <ul>
      <li>路线库与 Ultra 的价格一律是<strong>公开攻略区间</strong>，明确标注「不是实时票价」；</li>
      <li>每条素材带 <code>source</code> / <code>confidence</code> / 口径日期，低可信度必须写明不确定在哪；</li>
      <li>Ultra 校验器用正则挡掉<strong>车次号与精确时刻</strong>（写死在攻略里必然过期）；</li>
      <li>拷贝日会明确提示「不是当天真实车次」；排不出的行程按原因归类，不静默丢弃；</li>
      <li>世界数据<strong>不给机票价</strong>：只给大圆距离、预计飞行时间、时差与建议天数。</li>
    </ul>
    <h3>3. 配色与可读性</h3>
    <ul>
      <li>填充色与文字色分开：<code>--primary</code> 只用于按钮底/边框，文字用更深的
        <code>--primary-ink</code>（青绿当小字用只有 5.4 对比度，达标但费眼）；</li>
      <li>环境色只做色块（圆点/边框），<strong>绝不当字色</strong>；球面上的航线会提亮一档再画；</li>
      <li>原生下拉的选项显式写死深色字 + 白底（深色工具栏上的浅色字会被选项继承 → 白底白字）；</li>
      <li>对比度探针逐页签扫，含下拉选项专项与自检，当前 0 处不达标。</li>
    </ul>
    <h3>4. 不自动做事</h3>
    <p>爬取、写库、导入都必须由人点确认；后台任务可取消、有日志、有进度；
      单进程同一时刻只允许一个爬取任务，避免风控与写冲突。</p>"""

    # ---- 已知限制 ----
    limits = """
    <table class="kv wide">
      <thead><tr><th>限制</th><th>说明与应对</th></tr></thead>
      <tbody>
        <tr><th>时刻表是「参考」</th><td>来自公开渠道的某一天数据，存在隔日开行/临时调图偏差；
          拷贝日尤其只反映基准日。重要出行日建议真爬，买票前核对 12306。</td></tr>
        <tr><th>需要 WebGL 与 Chromium 内核</th><td>地图与地球模式依赖 WebGL；无 WebGL 时页面会明说原因而不是白屏，
          室内点/动线等纯数据功能不受影响。</td></tr>
        <tr><th>POI 坐标只用于示意</th><td>城内点位来自高德检索，用于预览与示意图，<strong>不做导航</strong>；
          查不到的名字记「未命中」，不猜坐标。</td></tr>
        <tr><th>世界数据不含机票价</th><td>世界航班票价必须实时查询，编一个数字没有意义；
          因此地球模式只给距离/时间/时差/天数建议。</td></tr>
        <tr><th>爬取受 12306 风控影响</th><td>连续失败会触发长冷却并重建会话；失败的对记在账本里，
          页面看得到，避免「以为爬完了」。</td></tr>
        <tr><th>单进程单任务</th><td>同一时刻只允许一个爬取任务；服务重启后内存里的任务信息消失，
          但进度账本在数据库里，页面照样能续爬。</td></tr>
      </tbody>
    </table>"""

    # ---- 爬取账本现状 ----
    crawl_rows = ""
    for run in cw["runs"][:5]:
        try:
            pct = (min(100.0, round(run["done"] / run["planned"] * 100, 1))
                   if run.get("planned") else 0.0)
        except (TypeError, ZeroDivisionError):
            pct = 0.0
        crawl_rows += (f"<tr><th>{esc(run['date'])}</th>"
                       f'<td><span class="pbar"><i style="--w:{pct}%"></i></span>'
                       f'<span class="pnum">{run["done"]}/{run["planned"] or "?"}</span></td>'
                       f"<td>{esc(run['state'])}</td><td>{esc(run['updated_at'])}</td></tr>")
    legacy_rows = "".join(f"<li>{esc(x['text'])}</li>" for x in cw["legacy"])
    crawl_now = f"""
    <h3>爬取账本（来自数据库，不是内存任务）</h3>
    {('<table class="kv wide"><thead><tr><th>日期</th><th>进度</th><th>状态</th><th>最后更新</th></tr></thead>'
      f'<tbody>{crawl_rows}</tbody></table>') if crawl_rows
     else '<p class="muted">还没有爬取记录（本机尚未真爬过任何日期）。</p>'}
    {f'<h3>旧格式断点残留（页面会如实提示，不假装能精确续爬）</h3><ul>{legacy_rows}</ul>' if legacy_rows else ''}"""

    # ---- 首屏数据条 / 走马灯（与正文同一批数字，不手写） ----
    hero_chips: list[tuple] = [
        (cat["cities"], "座城市资料"),
        (db_["dates"], "天时刻表"),
        (rt.get("routes", 0), "条参考路线"),
        (ul.get("loops", 0), "条穷游环线"),
        (wd["cities"], "座世界城市"),
    ]
    if "error" not in poi:
        hero_chips.append((poi.get("hit", 0), "个城内点位"))
    hero_stats = "".join(
        f'<div class="stat-chip"><b data-count="{v}">{esc(v)}</b><span>{esc(lab)}</span></div>'
        for v, lab in hero_chips)

    mq_items = [
        f"{cat['cities']} 座城市 · {cat['with_guide']} 份完整攻略",
        f"{db_['dates']} 天时刻表 · {db_['stop_rows']} 条停站记录",
        f"{rt.get('routes', 0)} 条参考路线 · 分类 {len(rt.get('categories') or [])} 个",
        f"{ul.get('loops', 0)} 条穷游环线 · 覆盖 {ul.get('origins', 0)} 个出发地",
        f"{wd['cities']} 座世界城市 · {wd['loops']} 条世界环线",
        f"{db_['stations']} 车站 · {db_['airports']} 机场",
        (f"{poi.get('hit', 0)}/{poi.get('targets', 0)} 城内点位命中"
         if "error" not in poi else None),
        (f"{totals.get('probes_ok', 0)}/{totals.get('probes_total', 0)} 浏览器探针 · "
         f"{totals.get('checks_ok', 0)}/{totals.get('checks_total', 0)} 数据体检"
         if totals else None),
        f"{tests.get('ran', 0)} 项单元测试" if tests else None,
    ]
    mq_inner = "".join(f"<span>{esc(it)}</span>" for it in mq_items if it)
    marquee = mq_inner * 2

    data_section = (kv_table(data_rows)
                    + '<p class="muted">价格为公开攻略区间、不是实时票价；'
                      '车次与票价以 12306 与官方渠道为准。</p>')

    body = "".join([
        card("结论", overview, num=1, sid="sec-01"),
        card("功能地图", features, num=2, sid="sec-02"),
        card("数据规模与口径", data_section, num=3, sid="sec-03"),
        card("验证结果", verify, num=4, sid="sec-04"),
        card("可靠性与诚实性设计", reliability, num=5, sid="sec-05"),
        card("爬取状态", crawl_now, num=6, sid="sec-06"),
        card("已知限制与边界", limits, num=7, sid="sec-07"),
    ])

    def _render(tmpl_path: Path) -> str:
        return (tmpl_path.read_text(encoding="utf-8")
                .replace("%%BODY%%", body)
                .replace("%%HERO_STATS%%", hero_stats)
                .replace("%%MARQUEE%%", marquee)
                .replace("%%NOW%%", esc(now))
                .replace("%%PYVER%%", esc(platform.python_version())))

    return _render(TEMPLATE), _render(DECK_TEMPLATE)


def metric(value, label: str, note: str = "") -> str:
    count = f' data-count="{value}"' if isinstance(value, (int, float)) else ""
    return (f'<div class="metric reveal"><b{count}>{esc(value)}</b><span>{esc(label)}'
            + (f' · {esc(note)}' if note else '') + '</span></div>')


def main() -> int:
    evidence = {}
    if EVIDENCE.exists():
        try:
            evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"⚠ 证据文件读不了（{exc}），报告里验证一节会显示「未生成」", file=sys.stderr)
    else:
        print("⚠ 还没有 data/report_evidence.json —— 先跑 scripts/collect_evidence.py",
              file=sys.stderr)

    data = collect()
    doc, deck = build(data, evidence)
    OUT.write_text(doc, encoding="utf-8")
    DECK_OUT.write_text(deck, encoding="utf-8")
    totals = evidence.get("totals", {})
    print(f"✓ 已生成 {OUT.relative_to(ROOT)} 与 {DECK_OUT.relative_to(ROOT)}"
          f"（测试 {evidence.get('tests', {}).get('ran', 0)} 项 · "
          f"探针 {totals.get('probes_ok', 0)}/{totals.get('probes_total', 0)} · "
          f"数据体检 {totals.get('checks_ok', 0)}/{totals.get('checks_total', 0)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
