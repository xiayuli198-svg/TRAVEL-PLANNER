"""本地 Web 服务：FastAPI + 单页前端。

启动: python -m travel_planner.web  （或 scripts/run_web.ps1）
访问: http://127.0.0.1:8000
"""
from __future__ import annotations

from pathlib import Path
import datetime as dt
import json
from typing import List, Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from typing import Union

from . import (catalog, concrete, crawl_progress, db, drive, guide as guide_mod, jobs,
               maintenance, od_focus, poi as poi_mod, roam, route_import,
               routes as routes_mod, service, trips as trips_mod, ultra as ultra_mod, worldmap)
from .ingest.client12306 import DEFAULT_DELAY

STATIC_DIR = Path(__file__).resolve().parents[2] / "web"

app = FastAPI(title="travel-planner", docs_url="/api/docs")


class PlanReq(BaseModel):
    frm: str
    to: str
    date: str
    time: str = "08:00"
    max_transfers: int = Field(default=3, ge=0, le=8)
    mode: str = "mixed"
    top: int = Field(default=10, ge=1, le=50)
    links: bool = True
    guide: bool = True
    buffer_min: int = Field(default=0, ge=0, le=180)
    objective: str = "fast"
    slack_hours: int = Field(default=2, ge=0, le=24)
    tour_stay: str = "halfday"
    with_flights: bool = False
    max_days: Optional[int] = Field(default=None, ge=2, le=60)
    # 游览模式偏好：城市数包含出发地和目的地；休整天数按每个游览点计算。
    tour_city_count: Optional[int] = Field(default=None, ge=2, le=20)
    rest_days: int = Field(default=0, ge=0, le=14)

    @field_validator("date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        try:
            dt.date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date 必须是 YYYY-MM-DD") from exc
        return value

    @field_validator("time")
    @classmethod
    def valid_time(cls, value: str) -> str:
        try:
            dt.time.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("time 必须是 HH:MM") from exc
        if len(value) != 5:
            raise ValueError("time 必须是 HH:MM")
        return value


class StayModel(BaseModel):
    mode: str = "nights"   # transit / halfday / nights
    nights: int = 1
    rest_days: int = Field(default=0, ge=0, le=14)


class LoopReq(BaseModel):
    date: str
    stops: List[str] = Field(min_length=2, max_length=12)
    stays: Optional[List[Union[int, StayModel]]] = None
    free_order: bool = False
    time: str = "08:00"
    max_transfers: int = Field(default=2, ge=0, le=8)
    mode: str = "mixed"
    links: bool = True
    guide: bool = True
    buffer_min: int = Field(default=0, ge=0, le=180)
    objective: str = "fast"
    slack_hours: int = Field(default=2, ge=0, le=24)
    max_days: Optional[int] = Field(default=None, ge=2, le=60)
    # 游览环线的城市数偏好（通常与 stops 数一致）；休整天数按每个到达点计算。
    tour_city_count: Optional[int] = Field(default=None, ge=2, le=20)
    rest_days: int = Field(default=0, ge=0, le=14)

    @field_validator("date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        try:
            dt.date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date 必须是 YYYY-MM-DD") from exc
        return value

    @field_validator("time")
    @classmethod
    def valid_time(cls, value: str) -> str:
        try:
            dt.time.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("time 必须是 HH:MM") from exc
        if len(value) != 5:
            raise ValueError("time 必须是 HH:MM")
        return value


class RoamReq(BaseModel):
    """地图漫游：地图上点出来的城市序列 → 逐日乘车表。"""
    date: str
    cities: List[str] = Field(min_length=2, max_length=12)
    closed: bool = True            # 默认回到起点成环
    free_order: bool = False       # 中间点顺序寻优
    stays: Optional[List[StayModel]] = None
    time: str = "08:00"
    mode: str = "mixed"
    max_transfers: int = Field(default=2, ge=0, le=8)
    links: bool = True
    guide: bool = True
    buffer_min: int = Field(default=30, ge=0, le=180)
    objective: str = "fast"
    slack_hours: int = Field(default=2, ge=0, le=24)
    max_days: Optional[int] = Field(default=None, ge=2, le=90)
    rest_days: int = Field(default=0, ge=0, le=14)

    @field_validator("date")
    @classmethod
    def valid_roam_date(cls, value: str) -> str:
        try:
            dt.date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date 必须是 YYYY-MM-DD") from exc
        return value

    @field_validator("time")
    @classmethod
    def valid_roam_time(cls, value: str) -> str:
        try:
            dt.time.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("time 必须是 HH:MM") from exc
        if len(value) != 5:
            raise ValueError("time 必须是 HH:MM")
        return value


class CopyReq(BaseModel):
    from_date: str
    to_date: str
    # 默认不覆盖目标日，避免误删手工导入或重新爬取的数据；需要覆盖时由
    # 数据页二次确认后显式传 true。
    overwrite: bool = False

    @field_validator("from_date", "to_date")
    @classmethod
    def valid_copy_date(cls, value: str) -> str:
        try:
            parsed = dt.date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("日期必须是 YYYY-MM-DD") from exc
        if parsed.isoformat() != value:
            raise ValueError("日期必须是 YYYY-MM-DD")
        return value


def _conn():
    return db.connect()


@app.get("/api/stations")
def api_stations(q: str = ""):
    conn = _conn()
    try:
        return {"suggestions": service.station_suggestions(conn, q)}
    finally:
        conn.close()


@app.post("/api/plan")
def api_plan(req: PlanReq):
    conn = _conn()
    try:
        # 记一笔查询历史：数据台的定向爬取据此把你常走的方向优先排进队列
        od_focus.remember_search(conn, req.frm, req.to, req.date, req.mode)
        result = service.do_plan(conn, req.frm, req.to, req.date,
                                 time=req.time, max_transfers=req.max_transfers,
                                 mode=req.mode, top=req.top,
                                 links=req.links, guide=req.guide,
                                 buffer_min=req.buffer_min,
                                 objective=req.objective, slack_hours=req.slack_hours,
                                 tour_stay=req.tour_stay, with_flights=req.with_flights,
                                 max_days=req.max_days,
                                 tour_city_count=req.tour_city_count,
                                 rest_days=req.rest_days)
        # 攻略联动：不管走哪条线，把沿途城市的资料（半日/一日/动线入口）一起带上
        if isinstance(result, dict) and result.get("ok"):
            result["guides"] = roam.plan_guides(result, conn=conn)
        return result
    finally:
        conn.close()


@app.post("/api/loop")
def api_loop(req: LoopReq):
    conn = _conn()
    try:
        for stop in (req.stops or []):
            od_focus.remember_search(conn, stop, "", req.date, req.mode)
        result = service.do_loop(conn, req.date, req.stops, req.stays,
                                 free_order=req.free_order, time=req.time,
                                 max_transfers=req.max_transfers, mode=req.mode,
                                 links=req.links, guide=req.guide,
                                 buffer_min=req.buffer_min,
                                 objective=req.objective, slack_hours=req.slack_hours,
                                 max_days=req.max_days,
                                 tour_city_count=req.tour_city_count,
                                 rest_days=req.rest_days)
        if isinstance(result, dict) and result.get("ok"):
            result["guides"] = roam.plan_guides(result, conn=conn)
        return result
    finally:
        conn.close()


@app.get("/api/roam/map")
def api_roam_map():
    """地图漫游初始化数据：大区 + 全部城市卡 + 经典环线。"""
    return roam.map_payload()


@app.get("/api/cities")
def api_cities(keyword: str = "", limit: int = 400):
    """只给城市名的轻量列表（数据台「直达」里的城市输入框用它做候选）。

    `/api/roam/cities` 是整套地图卡片（几百 KB），只为填个 datalist 拉它太浪费。
    顺带回报「有完整攻略的城市数」——数据可信度那张表要它，不能拿数据库里的
    `city_tourism` 行数充数（那只记了页面上改过的覆盖记录，不是攻略覆盖率）。
    """
    items = catalog.cities(keyword=keyword)
    names = sorted({c["name"] for c in items})
    all_cities = catalog.cities()
    return {
        "ok": True, "total": len(names), "cities": names[:max(1, min(limit, 2000))],
        "catalog_total": len(all_cities),
        "with_guide": sum(1 for c in all_cities if c.get("guide")),
    }


@app.get("/api/roam/cities")
def api_roam_cities(region: str = ""):
    if not region:
        return {"ok": True, "cities": roam.map_payload()["cities"]}
    return roam.region_cities(region)


@app.get("/api/roam/loops")
def api_roam_loops():
    payload = roam.map_payload()
    return {"ok": True, "loops": payload["loops"]}


@app.get("/api/roam/city")
def api_roam_city(name: str):
    """单座城市的完整攻略（前端点「需要更多攻略」时才请求，首屏不会变大）。"""
    return roam.city_detail(name)


class DriveReq(BaseModel):
    """自驾 / 租车模式。"""
    date: str
    cities: List[str] = Field(min_length=2, max_length=12)
    closed: bool = True
    stays: Optional[List[StayModel]] = None
    time: str = "08:00"
    rest_days: int = Field(default=0, ge=0, le=14)
    with_rental: bool = False
    car_class: str = "SUV"
    max_drive_hours: float = Field(default=6.5, ge=2, le=12)
    refresh: bool = False

    @field_validator("date")
    @classmethod
    def valid_drive_date(cls, value: str) -> str:
        try:
            dt.date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date 必须是 YYYY-MM-DD") from exc
        return value

    @field_validator("time")
    @classmethod
    def valid_drive_time(cls, value: str) -> str:
        try:
            dt.time.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("time 必须是 HH:MM") from exc
        if len(value) != 5:
            raise ValueError("time 必须是 HH:MM")
        return value


@app.post("/api/roam/drive")
def api_roam_drive(req: DriveReq):
    conn = _conn()
    try:
        result = drive.plan_drive(conn, req.date, req.cities, closed=req.closed,
                                  stays=req.stays, time=req.time, rest_days=req.rest_days,
                                  with_rental=req.with_rental, car_class=req.car_class,
                                  max_drive_hours=req.max_drive_hours, refresh=req.refresh)
        if result.get("ok"):
            # 与公交模式统一：城市用卡片结构（带照片、攻略标记、租车档位）
            cards = []
            for name in result.get("order_cities") or []:
                item = catalog.city(name)
                if item:
                    cards.append(roam.city_card(item))
            if cards:
                result["cities"] = cards
        return result
    finally:
        conn.close()


@app.get("/api/roam/rental")
def api_roam_rental(cities: str = "", car_class: str = "SUV"):
    """不排行程，只看这些城市的租车行情（地图上按城市显示）。"""
    names = [c.strip() for c in (cities or "").split(",") if c.strip()]
    if not names:
        names = [c["name"] for c in catalog.cities()[:0]]
    return {"ok": True, "car_class": car_class,
            "rates": drive.city_rental_rates(names, car_class),
            "model": drive.rental_model()}


@app.post("/api/roam/plan")
def api_roam_plan(req: RoamReq):
    conn = _conn()
    try:
        return roam.plan(conn, req.date, req.cities,
                         closed=req.closed, free_order=req.free_order,
                         stays=req.stays, time=req.time, mode=req.mode,
                         max_transfers=req.max_transfers, links=req.links,
                         guide=req.guide, buffer_min=req.buffer_min,
                         objective=req.objective, slack_hours=req.slack_hours,
                         max_days=req.max_days, rest_days=req.rest_days)
    finally:
        conn.close()


@app.get("/api/world/map")
def api_world_map():
    """地球模式初始化：世界城市卡 + 经典环线 + 世界之最。"""
    return worldmap.map_payload()


@app.get("/api/world/city")
def api_world_city(name: str):
    return worldmap.city_detail(name)


@app.get("/api/world/record")
def api_world_record(id: str):
    return worldmap.record_detail(id)


class WorldPlanReq(BaseModel):
    cities: List[str] = Field(min_length=2, max_length=16)
    closed: bool = False


@app.post("/api/world/plan")
def api_world_plan(req: WorldPlanReq):
    """世界行程单：飞行距离/时间 + 时差 + 建议天数（不含机票价）。"""
    return worldmap.plan_itinerary(req.cities, closed=req.closed)


@app.get("/api/status")
def api_status():
    conn = _conn()
    try:
        return service.data_status(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------- 数据台维护
class MaintainPlanReq(BaseModel):
    """维护计划请求：给日期，或给区间自动找缺口。只读，不写库。"""
    dates: List[str] = Field(default_factory=list)
    start: str = ""
    end: str = ""
    base: str = ""
    crawl: List[str] = Field(default_factory=list)

    @field_validator("dates", "crawl")
    @classmethod
    def valid_dates(cls, value: List[str]) -> List[str]:
        for item in value:
            try:
                dt.date.fromisoformat(item)
            except ValueError as exc:
                raise ValueError(f"{item} 必须是 YYYY-MM-DD") from exc
        return value


class MaintainRunReq(BaseModel):
    """维护执行请求。dry_run 默认 True：先预览，再点确定才真写。"""
    action: str                       # apply | delete_date | clear_flight_cache
    steps: List[dict] = Field(default_factory=list)
    date: str = ""
    dry_run: bool = True
    overwrite: bool = False


class CrawlReq(BaseModel):
    """真爬 12306：必须显式调用才会启动，服务不会自己爬。

    ``cities`` 非空时走**定向爬取**：只爬这些城市相关的 OD（默认 320 对），
    而不是全国 6006 对 —— 快得多，够规划用（时刻表本身定位就是「参考」）。
    """
    date: str
    delay: float = Field(default=DEFAULT_DELAY, ge=0.1, le=30.0)
    resume: bool = True
    cities: List[str] = Field(default_factory=list)
    max_pairs: int = Field(default=320, ge=2, le=6006)
    #: 精确指定 OD（三字码，如 ["BJP:SHH"]）—— 调试与补单段时用，优先于 cities
    ods: List[str] = Field(default_factory=list)

    @field_validator("ods")
    @classmethod
    def valid_ods(cls, value: List[str]) -> List[str]:
        for item in value:
            a, _, b = item.partition(":")
            if not a or not b:
                raise ValueError(f"ods 每项应为 FROM:TO，收到 {item!r}")
        return value

    @field_validator("date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        try:
            dt.date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date 必须是 YYYY-MM-DD") from exc
        return value


@app.get("/api/maintain/overview")
def api_maintain_overview():
    """数据台首屏：规模 + 每个日期的来源档位 + 体检项（只读）。"""
    conn = _conn()
    try:
        data = maintenance.overview(conn)
        data["ok"] = True
        data["jobs"] = jobs.REGISTRY.snapshots(include_logs=8)
        return data
    finally:
        conn.close()


@app.post("/api/maintain/plan")
def api_maintain_plan(req: MaintainPlanReq):
    """生成维护计划（只读）：哪天要补、从哪天拷、哪几天建议真爬。"""
    conn = _conn()
    try:
        dates = list(req.dates)
        if req.start and req.end:
            dates = maintenance.missing_between(conn, req.start, req.end)
        if not dates:
            return {"ok": False, "error": "给一个或多个日期，或者给起止区间"}
        plan = maintenance.plan(conn, dates, base=req.base or None, crawl=req.crawl)
        plan["ok"] = True
        return plan
    finally:
        conn.close()


@app.post("/api/maintain/run")
def api_maintain_run(req: MaintainRunReq):
    """执行维护动作。默认 dry_run=True，页面上确认后才带 dry_run=false 再调一次。"""
    conn = _conn()
    try:
        if req.action == "apply":
            if not req.steps:
                return {"ok": False, "error": "没有要执行的步骤"}
            return maintenance.apply_plan(conn, req.steps, dry_run=req.dry_run,
                                          overwrite=req.overwrite)
        if req.action == "delete_date":
            if not req.date:
                return {"ok": False, "error": "缺少日期"}
            return maintenance.delete_date(conn, req.date, dry_run=req.dry_run)
        if req.action == "clear_flight_cache":
            return maintenance.clear_flight_cache(conn, dry_run=req.dry_run)
        return {"ok": False, "error": f"未知动作 {req.action}"}
    finally:
        conn.close()


@app.post("/api/crawl/start")
def api_crawl_start(req: CrawlReq):
    """启动后台爬取任务（用户点了按钮才会走到这里）。带 cities 时只爬这些城市的 OD。"""
    conn = _conn()
    try:
        ods = None
        focus = None
        if req.ods:
            ods = [tuple(x.upper().split(":", 1)) for x in req.ods]
            focus = {"pairs": ods, "cities_used": ["（手工指定）"], "truncated": 0,
                     "targets": [], "error": ""}
        elif req.cities:
            focus = od_focus.focused_pairs_for_plan(conn, req.cities, max_pairs=req.max_pairs)
            ods = focus["pairs"]
            if not ods:
                return {"ok": False, "error": focus.get("error") or "没有生成任何 OD 对",
                        "focus": focus}
    finally:
        conn.close()
    res = jobs.REGISTRY.start_crawl(req.date, delay=req.delay, resume=req.resume, ods=ods)
    if res.get("ok"):
        resume_note = ""
        conn = _conn()
        try:
            before = crawl_progress.summary(conn, req.date)
            if req.resume and before.get("done"):
                resume_note = (f"上次已完成 {before['done']}/{before['planned']} 对 OD，"
                               f"这次只补剩下的 {before['remaining']} 对。")
            elif req.resume and before.get("planned") and before.get("state") == "done":
                resume_note = "这一天上次已经爬完了，本次会核对一遍（已入库的车次不会重复抓）。"
            res["progress"] = before
        finally:
            conn.close()
        res["note"] = (("已进入后台队列：" + ("定向爬取" if ods else "全国爬取")
                       + "，耗时取决于车次数和限速冷却；遇限速自动降速等待，可随时取消。"
                         "进度逐对落盘，回来点「接着爬」即可。")
                       + (" " + resume_note if resume_note else ""))
        res["focus"] = focus
        # 定向爬只覆盖部分 OD：这一天若已有数据，新爬的车次会与旧行并存（总数看着像「拼起来的」）。
        if ods:
            conn = _conn()
            try:
                have = conn.execute("SELECT COUNT(DISTINCT train_no) c FROM schedules WHERE date=?",
                                    (req.date,)).fetchone()["c"]
            finally:
                conn.close()
            if have:
                res["warning"] = (f"{req.date} 已有 {have} 趟数据：定向爬取不会删除它们，"
                                  "结果会是「已有 + 新爬」的混合。想只保留本次结果，"
                                  "先在日期表里删掉这一天再爬。")
    return res


@app.post("/api/crawl/plan")
def api_crawl_plan(req: CrawlReq):
    """只预览「定向爬取会爬哪些 OD」，不启动任务。"""
    conn = _conn()
    try:
        info = od_focus.focused_pairs_for_plan(conn, req.cities, max_pairs=req.max_pairs)
        return {"ok": not info.get("error"), "focus": info,
                "describe": od_focus.describe_focus(info),
                "sample": [f"{a}→{b}" for a, b in info["pairs"][:15]]}
    finally:
        conn.close()


@app.get("/api/crawl/targets")
def api_crawl_targets():
    """建议爬哪些城市：最近查过的 + 常用枢纽（「主动学习」的入口）。"""
    conn = _conn()
    try:
        sug = od_focus.suggest_targets(conn)
        sug["ok"] = True
        sug["describe"] = ("最近查过：" + "、".join(sug["from_history"])
                           if sug["from_history"] else "还没有查询记录；先规划一次就会记住")
        return sug
    finally:
        conn.close()


@app.get("/api/crawl/status")
def api_crawl_status(date: str = ""):
    if date:
        job = jobs.REGISTRY.get(date)
        return {"ok": True, "job": job.snapshot() if job else None}
    return {"ok": True, "jobs": jobs.REGISTRY.snapshots(), "running": jobs.REGISTRY.any_running()}


@app.get("/api/crawl/progress")
def api_crawl_progress(date: str = "", limit: int = 200):
    """某日爬取进度账本（断电/关窗口之后仍然查得到，页面据此显示「接着爬」）。

    进程内存里的任务重启就没了，所以「有没有残留进度、还剩多少、能不能续」必须问数据库：
    ``crawl_progress`` 按 OD 成员记账，谁做完了谁没做完一目了然。
    """
    conn = _conn()
    try:
        if date:
            data = crawl_progress.summary(conn, date)
            data["rows"] = crawl_progress.progress_rows(conn, date, limit=limit)
            data["note_text"] = crawl_progress.note_for(data)
            data["legacy"] = crawl_progress.legacy_info(date)
            running = jobs.REGISTRY.get(date)
            data["running_job"] = running.snapshot() if running else None
            data["ok"] = True
            return data
        return {"ok": True, "runs": crawl_progress.recent_runs(conn, limit=limit),
                "running": jobs.REGISTRY.any_running(),
                "jobs": jobs.REGISTRY.snapshots(include_logs=0)}
    finally:
        conn.close()


@app.post("/api/crawl/cancel")
def api_crawl_cancel(date: str = ""):
    return jobs.REGISTRY.cancel(date or None)


@app.get("/api/maintain/route")
def api_maintain_route(date: str):
    """查这个日期时数据是从哪来的（页面用来解释「这趟是拷贝的」）。"""
    conn = _conn()
    try:
        return {"ok": True, "date": date, "notes": maintenance.route_for_date(conn, date)}
    finally:
        conn.close()


# ---------------------------------------------------------------- 省钱/舒适/巧思路线库
@app.get("/api/routes")
def api_routes(city: str = "", category: str = "", theme: str = "",
               tier: str = "", clever: str = "", kind: str = "",
               sort: str = "value", skip: int = 0, limit: int = 0):
    """路线库列表：筛选 + 排序 + 分页（这是「参考」，不是实时票价）。

    ``clever=巧思`` 只看巧思分 ≥60 的「别出心裁」走法（这类常贵一点慢一点，
    按性价比排序会被埋掉）；``kind=loop|oneway`` 区分环线与单向；
    ``limit>0`` 时启用分页 —— 几百条路线不能一次全塞给页面。
    """
    items = routes_mod.query(city=city, category=category, theme=theme, tier=tier,
                             clever=clever, kind=kind, sort=sort)
    total = len(items)
    start = max(0, int(skip or 0))
    size = max(0, int(limit or 0))
    page = items[start:start + size] if size else items
    return {
        "ok": True,
        "updated": routes_mod.updated(),
        "note": (routes_mod.catalog().get("note")
                 or "价格为公开攻略区间，出行前请以 12306 / 官方渠道为准。"),
        "stats": routes_mod.stats(),
        "total": total,
        "skip": start,
        "limit": size,
        "has_more": bool(size) and start + size < total,
        "routes": [routes_mod.card(r) for r in page],
        "filters": {
            "categories": routes_mod.categories(),
            "themes": routes_mod.themes(),
            "tiers": [t for t in routes_mod.TIER_ORDER
                      if any(r.get("tier") == t for r in routes_mod.routes())],
            "clever_tags": routes_mod.stats().get("clever_tags") or [],
            "kinds": ["oneway", "loop"],
        },
    }


class RouteImportReq(BaseModel):
    """一键导入路线素材：**先校验、再入库**。

    ``apply=False``（默认）只校验并返回报告，不落盘；页面确认后再带 ``apply=true`` 调一次。
    校验复用 build_route_catalog 的那套（来源/坐标/时间/巧思高分必须写理由）。
    """
    data: str                       # 直接粘贴的 JSON 文本
    apply: bool = False
    filename: str = ""              # 空则自动命名 imported_<日期>.json
    promote: bool = False           # 写入后是否立刻正式化（默认只进暂存区，先审后收）


@app.post("/api/routes/import")
def api_routes_import(req: RouteImportReq):
    """一键导入：校验 →（可选）写入暂存区 → 补坐标 → 返回报告。"""
    text = (req.data or "").strip()
    if not text:
        return {"ok": False, "error": "没有收到内容"}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"不是合法 JSON：{exc}"}
    if isinstance(raw, list):
        raw = {"routes": raw}
    if not isinstance(raw, dict) or not raw.get("routes"):
        return {"ok": False, "error": "顶层应当是对象且包含 routes 数组"}

    try:
        report = route_import.validate(raw)
    except Exception as exc:                              # noqa: BLE001
        return {"ok": False, "error": f"校验器出错：{exc}"}
    if not report["problems"]:
        # 缺坐标的城市先用高德补齐（dry-run 时不写文件）
        report["coords"] = route_import.fill_missing_coords(report["cities"], apply=req.apply)
    if not req.apply:
        report.update({"ok": True, "applied": False})
        return report
    if report["problems"]:
        return {"ok": False, "applied": False, "error": "校验不通过，未写入", **report}
    try:
        out = route_import.write_staged(raw, filename=req.filename)
    except Exception as exc:                              # noqa: BLE001
        return {"ok": False, "applied": False, "error": f"写入失败：{exc}"}
    report.update(out)
    report.update({"ok": True, "applied": True})
    if req.promote:
        report["promote"] = route_import.promote()
        routes_mod.catalog()
    report["next"] = ("已写入暂存区；点「把暂存区正式化」即可上线"
                      if not req.promote else "已直接上线")
    return report


@app.get("/api/routes/staged")
def api_routes_staged():
    """看看暂存区里有什么（子代理产出 / 一键导入的内容）。"""
    return {"ok": True, **route_import.staged_summary()}


@app.post("/api/routes/promote")
def api_routes_promote():
    """把暂存区正式化：合并进 smart_routes.json 并归档原文件。"""
    try:
        out = route_import.promote()
    except Exception as exc:                              # noqa: BLE001
        return {"ok": False, "error": f"正式化失败：{exc}"}
    if out.get("ok"):
        routes_mod.catalog()                              # mtime 变了，触发重载
        out["stats"] = routes_mod.stats()
    return out


@app.get("/api/routes/detail")
def api_route_detail(id: str):
    item = routes_mod.route(id)
    if not item:
        return {"ok": False, "error": "没有这条路线"}
    return {"ok": True, "route": routes_mod.card(item)}


@app.get("/api/routes/suggest")
def api_route_suggest(cities: str = "", limit: int = 4):
    """按城市推荐相关路线（规划页在出结果后调用）。"""
    wanted = [c.strip() for c in (cities or "").split(",") if c.strip()]
    items = routes_mod.for_cities(wanted, limit=max(1, min(10, limit)))
    return {"ok": True, "cities": wanted,
            "routes": [routes_mod.card(r) for r in items]}


class RoutePlanReq(BaseModel):
    """把参考路线落到某一天：用本地时刻表核实车次、用高德核实车程。"""
    id: str
    date: str

    @field_validator("date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        try:
            dt.date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date 必须是 YYYY-MM-DD") from exc
        return value


@app.post("/api/routes/concrete")
def api_route_concrete(req: RoutePlanReq):
    """参考路线 × 真实数据：每段给出可核实的车次/车程，查不到就说查不到。"""
    item = routes_mod.route(req.id)
    if not item:
        return {"ok": False, "error": "没有这条路线"}
    conn = _conn()
    try:
        return concrete.concrete_plan(conn, item, req.date)
    finally:
        conn.close()


class RouteTripReq(BaseModel):
    """把参考路线排成真实行程（走规划引擎，与规划页同一个）。"""
    id: str
    date: str
    stay_mode: str = ""            # transit | one_night | two_nights，空=按素材时长推断
    objective: str = "fast"        # fast | cheap
    mode: str = "mixed"            # rail | air | mixed | green
    max_transfers: int = Field(default=2, ge=0, le=5)
    buffer_min: int = Field(default=30, ge=0, le=180)
    ground_ok: bool = True         # 景区/小镇落脚点是否允许拆成「铁路段 + 地面接驳段」

    @field_validator("date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        try:
            dt.date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date 必须是 YYYY-MM-DD") from exc
        return value


@app.post("/api/routes/trip")
def api_route_trip(req: RouteTripReq):
    """路线 → 一键变行程：城市序列交给规划引擎，排出可执行的乘车表。"""
    item = routes_mod.route(req.id)
    if not item:
        return {"ok": False, "error": "没有这条路线"}
    conn = _conn()
    try:
        return concrete.plan_trip(conn, item, req.date,
                                  objective=req.objective, mode=req.mode,
                                  max_transfers=req.max_transfers,
                                  buffer_min=req.buffer_min,
                                  stay_mode=req.stay_mode or None,
                                  ground_ok=req.ground_ok)
    finally:
        conn.close()


@app.post("/api/copy-date")
def api_copy_date(req: CopyReq):
    conn = _conn()
    try:
        return service.copy_date(conn, req.from_date, req.to_date, req.overwrite)
    finally:
        conn.close()


# ---------------------------------------------------------------- 我的行程（存档）
def _trips_conn():
    return trips_mod.connect()


class TripSaveReq(BaseModel):
    """把一条参考路线排成行程并存档（也可以在已有行程上改名）。"""
    id: str = ""                   # 路线 id；配合 date 使用
    date: str = ""
    stay_mode: str = ""
    objective: str = "fast"
    mode: str = "mixed"
    name: str = ""
    note: str = ""
    replace: bool = True           # 同一天同一套偏好已有存档时，是否覆盖

    @field_validator("date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        if not value:
            return value
        try:
            dt.date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date 必须是 YYYY-MM-DD") from exc
        return value


@app.get("/api/trips")
def api_trips(q: str = "", kind: str = "", date: str = "", order: str = "created",
              skip: int = 0, limit: int = 20):
    """我的行程列表（不含完整方案，点开某条再取）。"""
    conn = _trips_conn()
    try:
        out = trips_mod.list_saved(conn, q=q, kind=kind, date=date, order=order,
                                   skip=skip, limit=limit)
        out["stats"] = trips_mod.stats(conn)
        out["coverage"] = trips_mod.coverage(conn)
        return out
    finally:
        conn.close()


@app.get("/api/trips/detail")
def api_trip_detail(id: int):
    conn = _trips_conn()
    try:
        item = trips_mod.get(conn, id)
        if not item:
            return {"ok": False, "error": "没有这份行程"}
        return {"ok": True, "trip": item}
    finally:
        conn.close()


@app.post("/api/trips/save")
def api_trip_save(req: TripSaveReq):
    """路线 → 排成行程 → 存进「我的行程」。排不出来会说清原因，不留半成品。"""
    item = routes_mod.route(req.id) if req.id else None
    if not item:
        return {"ok": False, "error": "没有这条路线"}
    if not req.date:
        return {"ok": False, "error": "要给一个出行日（用它那天的时刻表排）"}
    tconn = _trips_conn()
    conn = _conn()
    try:
        out = trips_mod.save_route(conn, tconn, item, req.date, replace=req.replace,
                                   objective=req.objective, mode=req.mode,
                                   stay_mode=req.stay_mode or None,
                                   name=req.name, note=req.note)
        if not out.get("ok"):
            return out
        detail = trips_mod.get(tconn, out["id"]) if out.get("id") else None
        return {"ok": True, "saved": out, "trip": detail,
                "note": "已存进「我的行程」；行程是本地时刻表算出来的参考，出行前以 12306 为准。"}
    finally:
        conn.close()
        tconn.close()


@app.post("/api/trips/rename")
def api_trip_rename(payload: dict):
    tid, name = payload.get("id"), payload.get("name") or ""
    conn = _trips_conn()
    try:
        return trips_mod.rename(conn, int(tid), name)
    finally:
        conn.close()


@app.post("/api/trips/delete")
def api_trip_delete(payload: dict):
    tid = payload.get("id")
    conn = _trips_conn()
    try:
        return trips_mod.remove(conn, int(tid))
    finally:
        conn.close()


@app.get("/api/trips/skips")
def api_trip_skips(limit: int = 200):
    """排不出城际行程的条目（同城选站类、没有铁路站的小镇）—— 这些也是路线库的体检结果。"""
    conn = _trips_conn()
    try:
        return {"ok": True, "skips": trips_mod.skips(conn, limit=limit)}
    finally:
        conn.close()


class TripBuildReq(BaseModel):
    """批量建档：把整库路线按同一天排成行程并存档（后台任务，可取消）。"""
    date: str
    stay_mode: str = ""
    objective: str = "fast"
    mode: str = "mixed"
    replace: bool = False          # 默认不覆盖已有存档，只补没排过的
    ids: List[str] = Field(default_factory=list)
    limit: int = 0                 # 0 = 全库；调试时可以只跑前 N 条

    @field_validator("date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        try:
            dt.date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date 必须是 YYYY-MM-DD") from exc
        return value


@app.post("/api/trips/build")
def api_trips_build(req: TripBuildReq):
    """启动批量建档。这是**读本地时刻表**的重活（不联网、不碰 12306），可随时取消。"""
    out = trips_mod.BUILD.start(req.date, stay_mode=req.stay_mode, objective=req.objective,
                                mode=req.mode, replace=req.replace,
                                ids=req.ids or None, limit=req.limit)
    if out.get("ok"):
        out["note"] = ("正在把路线排成行程并存档：只用本地时刻表，不联网；"
                       "屏幕可以关，任务在服务进程里继续跑。")
    return out


@app.get("/api/trips/build/status")
def api_trips_build_status():
    return trips_mod.BUILD.status()


@app.post("/api/trips/build/cancel")
def api_trips_build_cancel():
    return trips_mod.BUILD.cancel()


# ---------------------------------------------------------------- 环线 Ultra（穷游环线）
@app.get("/api/ultra")
def api_ultra(origin: str = "", days_max: int = 0, budget_max: int = 0, q: str = "",
              sort: str = "value", scale: str = ""):
    """环线 Ultra 列表：北京出发 / 河北出发的穷游环线（整条线，不是单点走法）。

    ``scale`` 可取 ``large``（大环线：跨省沿大动脉走 8 天以上、6 城以上）或 ``small``。
    价格是公开攻略区间、不是实时票价；每条带来源与置信度，低置信度的写明了不确定在哪。
    """
    items = ultra_mod.query(origin=origin, days_max=days_max, budget_max=budget_max,
                            q=q, sort=sort, scale=scale)
    return {"ok": True, "total": len(items), "updated": ultra_mod.updated(),
            "note": ultra_mod.note(), "stats": ultra_mod.stats(),
            "origins": ultra_mod.origins(), "loops": items}


@app.get("/api/ultra/detail")
def api_ultra_detail(id: str):
    item = ultra_mod.detail(id)
    if not item:
        return {"ok": False, "error": "没有这条环线"}
    return {"ok": True, "loop": item}


@app.get("/api/trips/export")
def api_trips_export():
    conn = _trips_conn()
    try:
        return {"ok": True, "json": trips_mod.export_json(conn),
                "stats": trips_mod.stats(conn)}
    finally:
        conn.close()


# ---------------------------------------------------------------- 城内点位（只读预览）
@app.get("/api/poi")
def api_poi(city: str = "", status: str = "", skip: int = 0, limit: int = 30):
    """城内看点坐标（只读）：按城市/是否命中筛选，带覆盖率。

    数据来自高德关键词检索，**仅供城内预览与示意图**，不做导航；
    本地自用缓存，不随工具分发。
    """
    conn = poi_mod.connect()
    try:
        out = poi_mod.list_pois(conn, city=city, status=status, limit=limit, skip=skip)
        out["stats"] = poi_mod.stats(conn)
        return out
    finally:
        conn.close()


@app.get("/api/poi/export")
def api_poi_export():
    conn = poi_mod.connect()
    try:
        return {"ok": True, "json": poi_mod.export_json(conn), "stats": poi_mod.stats(conn)}
    finally:
        conn.close()


class PoiHarvestReq(BaseModel):
    """按名单补坐标（一次性、要人点；1 个名字 1 次高德请求，已查过的会跳过）。"""
    limit: int = Field(default=0, ge=0)          # 0 = 名单里所有还没查的
    delay: float = Field(default=0.3, ge=0.0, le=5.0)
    refresh: bool = False                        # 重查已落库的名字
    only_miss: bool = False                      # 只重查上次判为未命中的


@app.post("/api/poi/harvest")
def api_poi_harvest(req: PoiHarvestReq):
    out = poi_mod.HARVEST.start(limit=req.limit, delay=req.delay, refresh=req.refresh,
                                only_miss=req.only_miss)
    if out.get("ok"):
        out["note"] = ("正在按名单逐个查（约 900 个看点）：1 个名字 1 次请求，"
                       "已查过的会跳过；屏幕可以关，任务在服务进程里继续。")
    return out


@app.get("/api/poi/harvest/status")
def api_poi_harvest_status():
    return poi_mod.HARVEST.status()


@app.post("/api/poi/harvest/cancel")
def api_poi_harvest_cancel():
    return poi_mod.HARVEST.cancel()


# ---------------------------------------------------------------- 城市停留动线（B+）
@app.get("/api/guide/city")
def api_guide_city(city: str, pool: bool = True):
    """一座城的停留动线：把散文按天对齐成带坐标的点位。

    ``pool=1``（默认）时，如果这座城还没抓过 POI 池，就先抓 4 页（4 次请求）——
    步行街/老街/商圈散在别的类型里，没有池子就对不上。
    """
    conn = guide_mod.connect()
    try:
        built = None
        if pool:
            built = guide_mod.ensure_pool(conn, city, pages=1)
        data = guide_mod.city_days(conn, city)
        if built and built.get("ok") and built.get("added"):
            data["pool_built"] = built
        return data
    finally:
        conn.close()


class GuidePoolReq(BaseModel):
    city: str
    pages: int = Field(default=1, ge=1, le=5)
    refresh: bool = False


@app.post("/api/guide/pool")
def api_guide_pool(req: GuidePoolReq):
    """给一座城补/重抓 POI 池（4 类 × pages 页请求）。"""
    conn = guide_mod.connect()
    try:
        out = guide_mod.ensure_pool(conn, req.city, pages=req.pages, refresh=req.refresh)
        if out.get("ok"):
            out["days"] = guide_mod.city_days(conn, req.city)["days"]
        return out
    finally:
        conn.close()


@app.get("/api/guide/day")
def api_guide_day(city: str, day: str, live: bool = True):
    """某一天的动线：点位 + 市内腿（按需现算，落缓存）+ 示意图。

    腿是**实时查询**高德的结果，不是时刻表；图只是按经纬度摆位的示意图，不做导航。
    """
    conn = guide_mod.connect()
    try:
        return guide_mod.day_view(conn, city, day, live=live)
    finally:
        conn.close()


@app.get("/")
def index():
    # 外壳不缓存：页面里的 /static/*.js|css 带 ?v= 版本号，改了前端只要更新版本号，
    # 浏览器就不会继续用旧样式/旧脚本（否则会出现「页签是新的、样式是旧的」）。
    return FileResponse(STATIC_DIR / "index.html",
                        headers={"Cache-Control": "no-cache, must-revalidate"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
