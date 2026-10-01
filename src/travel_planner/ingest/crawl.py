"""某日全国时刻表爬取器：枚举枢纽城市间 OD 对 → 发现车次 → 整列入库。

覆盖原理：凡经过 ≥2 个枢纽站的车次，会被至少一个枢纽 OD 对发现，
随后 queryTrainInfo 会把整列停站（含非枢纽中间站）全部入库。

韧性策略：连续失败达到阈值后长冷却并重建 Cookie 会话（应对 12306 风控），
失败记录实时追加到 data/crawl_failures_<date>.txt。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import time
from typing import Dict, List, Optional, Sequence, Tuple

from .. import crawl_progress, db
from .client12306 import (Client12306, CrawlCancelled, DEFAULT_DELAY,
                          RateLimited, TemporaryRequestError)
from .schedule import query_left_ticket, query_train_info

# 直辖市 + 省会 + 计划单列市 + 重要枢纽（城市名须与 station_name.js 的 city 字段一致）
HUB_CITIES: Tuple[str, ...] = (
    "北京", "天津", "上海", "重庆",
    "石家庄", "太原", "呼和浩特", "沈阳", "大连", "长春", "哈尔滨",
    "南京", "苏州", "无锡", "常州", "徐州", "杭州", "宁波", "温州", "金华",
    "合肥", "芜湖", "蚌埠", "阜阳", "福州", "厦门", "南昌", "九江", "赣州", "上饶",
    "济南", "青岛", "烟台", "郑州", "洛阳", "商丘", "武汉", "襄阳", "宜昌",
    "长沙", "株洲", "衡阳", "怀化", "广州", "深圳", "佛山", "东莞", "珠海", "汕头", "湛江",
    "南宁", "柳州", "桂林", "海口", "三亚", "成都", "绵阳", "乐山", "宜宾",
    "贵阳", "遵义", "昆明", "大理", "丽江", "拉萨", "西安", "宝鸡",
    "兰州", "西宁", "银川", "乌鲁木齐", "唐山", "秦皇岛", "保定", "邯郸",
    "台州", "绍兴", "嘉兴",
)


def pick_hub_stations(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """每个枢纽城市选一个主站（优先站名与城市同名者）。"""
    ph = ",".join("?" * len(HUB_CITIES))
    rows = conn.execute(
        f"SELECT code,name,city,ordinal FROM stations WHERE city IN ({ph})",
        list(HUB_CITIES),
    ).fetchall()
    by_city: Dict[str, List[sqlite3.Row]] = {}
    for r in rows:
        by_city.setdefault(r["city"], []).append(r)
    hubs: List[sqlite3.Row] = []
    for city, rs in sorted(by_city.items()):
        rs.sort(key=lambda r: (r["name"] != city, r["name"] != city + "站",
                               len(r["name"]), r["ordinal"]))
        hubs.append(rs[0])
    return hubs


def _resolve_and_anchor(conn: sqlite3.Connection, stops: List[Dict],
                        from_code: str, name2code: Optional[Dict[str, str]] = None) -> List[Dict]:
    """站名→三字码；以发现车次的 OD 起点站为锚，把 diff 归零为相对查询日的天偏移。"""
    if name2code is None:
        name2code = {r["name"]: r["code"]
                     for r in conn.execute("SELECT name, code FROM stations")}
    out: List[Dict] = []
    for s in stops:
        code = name2code.get(s["station_name"])
        if not code:
            continue  # 站名表未覆盖（极少见），丢弃该站
        out.append({
            "seq": s["seq"], "station_code": code, "station_name": s["station_name"],
            "arr": s["arr"], "dep": s["dep"], "diff": s["diff"],
        })
    if not out:
        return out
    anchor = next((i for i, s in enumerate(out) if s["station_code"] == from_code), 0)
    base = out[anchor]["diff"]
    for s in out:
        s["day"] = s["diff"] - base
    # 首末站的 '--' 用邻值补齐
    if out[0]["arr"] == "--":
        out[0]["arr"] = out[0]["dep"]
    if out[-1]["dep"] == "--":
        out[-1]["dep"] = out[-1]["arr"]
    return out


class _ResilientCrawler:
    """带连续失败冷却与会话重建的爬取控制。"""

    def __init__(self, date: str, delay: float,
                 cooldown_threshold: int = 12, cooldown_seconds: int = 300,
                 max_cooldowns: Optional[int] = None, cancel_cb=None, progress_cb=None):
        self.client = Client12306(delay=delay, cancel_cb=cancel_cb, wait_cb=self._on_wait)
        self.fail_path = db.PROJECT_ROOT / "data" / f"crawl_failures_{date}.txt"
        self.fail_path.parent.mkdir(parents=True, exist_ok=True)
        self.fail_fh = self.fail_path.open("a", encoding="utf-8")
        self.delay = delay
        self.threshold = cooldown_threshold
        self.cooldown_seconds = cooldown_seconds
        self.max_cooldowns = max_cooldowns
        self.consec = 0
        self.cooldowns = 0
        self.progress_cb = progress_cb
        self.phase, self.done, self.total, self.extra = "pairs", 0, 0, {}

    def progress(self, phase: str, done: int, total: int, extra: Dict) -> None:
        self.phase, self.done, self.total, self.extra = phase, done, total, dict(extra)
        if self.progress_cb:
            self.progress_cb(phase, done, total, {**extra, "delay": self.client.delay})

    def _on_wait(self, phase: str, remaining: float, reason: str) -> None:
        if self.progress_cb:
            self.progress_cb(phase if remaining else self.phase, self.done, self.total,
                             {**self.extra, "retry_in": round(remaining, 1),
                              "reason": reason if remaining else "",
                              "delay": self.client.delay})

    def _cooldown(self, reason: str, retry_after: float = 0) -> None:
        self.cooldowns += 1
        if self.max_cooldowns is not None and self.cooldowns > self.max_cooldowns:
            raise RuntimeError("冷却次数达到上限，断点已保存，可稍后续爬")
        self.client.slow_down()
        seconds = max(retry_after, min(1800, self.cooldown_seconds * 2 ** min(self.cooldowns - 1, 6)))
        print(f"[cool] {reason}，等待 {seconds:.0f}s，请求间隔 {self.client.delay:.2f}s", flush=True)
        self.client.wait(seconds, "cooldown", reason)
        self.client.reset_session()
        self.consec = 0

    def request(self, fn, what: str):
        """风控时保留当前工作项，冷却后重试；不会把后续 OD 扫成失败。"""
        while True:
            self.client.check_cancelled()
            try:
                return fn()
            except (RateLimited, TemporaryRequestError) as e:
                self.note_fail(f"{what}: {e}")
                self._cooldown(str(e), getattr(e, "retry_after", 0))

    def note_fail(self, msg: str) -> None:
        self.fail_fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}\n")
        self.fail_fh.flush()
        print(f"  ! {msg}", flush=True)

    def on_failure(self, e: Exception, what: str) -> None:
        self.consec += 1
        self.note_fail(f"{what}: {e!r}")
        if self.consec >= self.threshold:
            self._cooldown(f"连续失败 {self.consec} 次")

    def on_success(self) -> None:
        self.consec = 0
        self.cooldowns = 0

    def close(self) -> None:
        self.fail_fh.close()


def crawl_date(conn: sqlite3.Connection, date: str, hubs: Optional[Sequence[str]] = None,
               ods: Optional[Sequence[Tuple[str, str]]] = None,
               pairs_limit: int = 0, delay: float = DEFAULT_DELAY,
               progress_every: int = 50, cooldown_threshold: int = 12,
               cooldown_seconds: int = 300, resume: bool = False,
               progress_cb=None, cancel_cb=None) -> Dict:
    """爬取某日时刻表。ods 为指定 OD 对（如 [('BJP','SHH'), ...]）；否则枚举枢纽对。

    **断点续爬按 OD 成员记账**（见 ``crawl_progress``）：每一对开爬前记 ``running``、
    做完记 ``done`` 并立刻落盘，所以断电/关窗口/进程被杀之后，续爬只会重跑「没确认完成」的那些对。
    这一点比旧的「按序号跳过前 N 对」可靠：换了 OD 列表（全国 → 定向，或换了城市）
    也不会错位跳空或重复爬。

    ``progress_cb(phase, done, total, extra)`` 供 Web 端后台任务显示进度；
    ``cancel_cb()`` 返回 True 时在当前车次结束后收工（断点已落盘，可 --resume 续爬）。
    """
    state_path = db.PROJECT_ROOT / "data" / f"crawl_state_{date}.txt"
    disc_path = db.PROJECT_ROOT / "data" / f"discovered_{date}.jsonl"

    def write_state(phase: str, index: int) -> None:
        """只写「人看的」一行面包屑（原子替换，断电不会留半截文件）。

        真正的续爬依据是 SQLite 账本，这个文件坏了/没了都不影响续爬。
        """
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = state_path.with_suffix(".txt.tmp")
            tmp.write_text(f"version 2\nphase {phase}\nindex {index}\n", encoding="utf-8")
            tmp.replace(state_path)
        except OSError:
            pass

    if ods:
        pairs = [(a.upper(), b.upper()) for a, b in ods]
        planned_note = f"定向 {len(pairs)} 对"
    else:
        if hubs is None:
            hubs = [r["code"] for r in pick_hub_stations(conn)]
        hubs = list(hubs)
        pairs = [(a, b) for a in hubs for b in hubs if a != b]
        if pairs_limit and pairs_limit > 0:
            pairs = pairs[:pairs_limit]
        planned_note = f"全国枢纽 {len(pairs)} 对"

    # ---- 续爬：按 OD 成员跳过已完成的 ----
    pairs = list(dict.fromkeys(pairs))
    crawl_progress.ensure_schema(conn)
    had_ledger = conn.execute("SELECT 1 FROM crawl_runs WHERE date=?", (date,)).fetchone()
    had_pairs = conn.execute("SELECT 1 FROM crawl_pairs WHERE date=? LIMIT 1", (date,)).fetchone()
    crawl_progress.start_run(conn, date, len(pairs), note=planned_note, reset=not resume, pairs=pairs)
    done_keys = crawl_progress.done_ods(conn, date) if resume else set()
    pending = [(a, b) for a, b in pairs
               if crawl_progress.od_key(a, b) not in done_keys]

    # 旧格式断点（只有序号）：仅在全国爬取时能安全翻译成成员记账 —— 那份列表是确定性的。
    # 定向爬取的列表每次重算，翻译就是错的，所以宁可不迁移（重爬一遍 OD，但已入库的车次不会重复抓）。
    if resume and not ods and not had_ledger and not had_pairs and state_path.exists():
        old_index = 0
        try:
            lines = state_path.read_text(encoding="utf-8").splitlines()
            if "version 2" not in lines:
                for line in lines:
                    if line.startswith("index "):
                        old_index = int(line.split()[1])
        except (OSError, ValueError, IndexError):
            old_index = 0                      # 文件被断电截断：当作没有断点，别让整次爬取炸掉
        if old_index:
            failed_ods = set()
            fail_path = db.PROJECT_ROOT / "data" / f"crawl_failures_{date}.txt"
            if fail_path.exists():
                for line in fail_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    match = re.search(r"\bpair ([A-Z]{3})->([A-Z]{3}):", line)
                    if match:
                        failed_ods.add(crawl_progress.od_key(*match.groups()))
            migrated = crawl_progress.legacy_state(conn, date, pairs, old_index, exclude=failed_ods)
            if migrated:
                done_keys = crawl_progress.done_ods(conn, date)
                pending = [(a, b) for a, b in pairs
                           if crawl_progress.od_key(a, b) not in done_keys]
                print(f"[resume] 旧断点按序号迁移 {migrated} 对（全国爬取的 OD 列表是确定性的）",
                      flush=True)

    # ---- 已发现的车次（这份 jsonl 是追加式的，断电最多丢最后一条） ----
    discovered: Dict[str, Dict[str, str]] = {}
    if resume and disc_path.exists():
        for line in disc_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue                        # 断电时写了一半的那一行，跳过
            if isinstance(o, dict) and all(isinstance(o.get(k), str) and o[k]
                                          for k in ("train_no", "code", "from")):
                discovered[o["train_no"]] = {"code": o["code"], "from": o["from"]}

    skipped = len(pairs) - len(pending)
    if resume and (skipped or discovered):
        print(f"[resume] 跳过已完成 {skipped}/{len(pairs)} 对 OD，已发现车次 {len(discovered)}",
              flush=True)
    write_state("pairs", skipped)

    rc = _ResilientCrawler(date, delay, cooldown_threshold, cooldown_seconds,
                           cancel_cb=cancel_cb, progress_cb=progress_cb)
    disc_path.parent.mkdir(parents=True, exist_ok=True)
    disc_fh = None
    failures = 0
    ok = 0
    stored = 0
    cancelled = False

    t0 = time.time()
    try:
        # 隔开断电留下的半行，避免新记录粘到坏行末尾而再次丢失。
        disc_fh = disc_path.open("a" if resume else "w", encoding="utf-8")
        disc_fh.write("\n")
        disc_fh.flush()
        rc.progress("pairs", skipped, len(pairs), {"discovered": len(discovered), "failures": 0})
        rc.request(rc.client.warmup, "初始化会话")
        for i, (a, b) in enumerate(pending, start=1):
            rc.client.check_cancelled()
            # 先记账再请求：断电时这一对会留在 running，下次续爬重试它
            crawl_progress.mark_running(conn, date, a, b)
            try:
                trains = rc.request(lambda: query_left_ticket(rc.client, date, a, b), f"pair {a}->{b}")
            except CrawlCancelled:
                raise
            except Exception as e:  # noqa: BLE001
                failures += 1
                crawl_progress.mark_failed(conn, date, a, b)
                rc.on_failure(e, f"pair {a}->{b}")
            else:
                new = 0
                for t in trains:
                    if t["train_no"] not in discovered:
                        discovered[t["train_no"]] = {
                            "code": t["train_code"], "from": t["from_code"]}
                        disc_fh.write(json.dumps(
                            {"train_no": t["train_no"], "code": t["train_code"],
                             "from": t["from_code"]}, ensure_ascii=False) + "\n")
                        new += 1
                if new:
                    disc_fh.flush()
                rc.on_success()
                crawl_progress.mark_done(conn, date, a, b, trains=new)
            write_state("pairs", skipped + i)
            crawl_progress.update_run(conn, date, discovered=len(discovered))
            if i % progress_every == 0 or i == len(pending):
                print(f"[pairs {skipped + i}/{len(pairs)}] 已发现车次 {len(discovered)}，"
                      f"失败 {failures}，用时 {time.time()-t0:.0f}s", flush=True)
            rc.progress("pairs", skipped + i, len(pairs),
                        {"discovered": len(discovered), "failures": failures})
            rc.client.check_cancelled()

        write_state("trains", skipped + len(pending))
        print(f"发现 {len(discovered)} 个车次，开始抓取整列停站 ...", flush=True)
        have = {r["train_no"] for r in conn.execute(
            "SELECT DISTINCT train_no FROM schedules WHERE date=?", (date,))}
        todo = sorted(set(discovered) - have)
        stored = len(discovered) - len(todo)
        station_codes = {r["name"]: r["code"]
                         for r in conn.execute("SELECT name, code FROM stations")}
        print(f"其中 {len(todo)} 个尚未入库（{len(discovered) - len(todo)} 个跳过）", flush=True)
        rc.progress("trains", 0, len(todo), {"ingested": ok, "failures": failures})
        for j, tn in enumerate(todo, start=1):
            rc.client.check_cancelled()
            meta = discovered[tn]
            try:
                stops = rc.request(lambda: query_train_info(rc.client, tn, date),
                                   f"train {meta['code']}({tn})")
                stops = _resolve_and_anchor(conn, stops, meta["from"], name2code=station_codes)
                if not stops:
                    raise ValueError("无可入库的停站数据")
            except CrawlCancelled:
                raise
            except Exception as e:  # noqa: BLE001
                failures += 1
                rc.on_failure(e, f"train {meta['code']}({tn})")
            else:
                # 数据库写错不能当成普通网络失败吞掉。
                db.upsert_schedule(conn, tn, meta["code"], date, stops)
                rc.on_success()
                ok += 1
            crawl_progress.update_run(conn, date, ingested=stored + ok)
            if j % progress_every == 0 or j == len(todo):
                print(f"[trains {j}/{len(todo)}] 已入库 {ok}", flush=True)
            rc.progress("trains", j, len(todo), {"ingested": ok, "failures": failures})
            rc.client.check_cancelled()
    except CrawlCancelled:
        cancelled = True
        rc.progress("cancelled", rc.done, rc.total,
                    {"discovered": len(discovered), "ingested": ok, "failures": failures})
        print("[cancel] 收到取消请求，已保存断点，可用 --resume 续爬", flush=True)
    except BaseException:
        crawl_progress.update_run(conn, date, state="failed", discovered=len(discovered),
                                  ingested=stored + ok, finished_at=dt.datetime.now().isoformat(timespec="seconds"))
        raise
    finally:
        if disc_fh is not None:
            disc_fh.close()
        rc.close()

    stats = {
        "date": date,
        "pairs": len(pairs),
        "skipped_pairs": skipped,
        "discovered": len(discovered),
        "ingested": stored + ok,
        "failures": failures,
        "seconds": round(time.time() - t0),
        "cancelled": cancelled,
        "state": "cancelled" if cancelled else (
            "partial" if failures or (len(discovered) and stored + ok < len(discovered)) else "done"),
    }
    crawl_progress.update_run(
        conn, date, state=stats["state"],
        discovered=len(discovered), ingested=stored + ok,
        finished_at=("" if cancelled else dt.datetime.now().isoformat(timespec="seconds")))
    db.set_meta(conn, f"crawl:{date}:stats",
                f"pairs={stats['pairs']} discovered={stats['discovered']} "
                f"ingested={stats['ingested']} failures={stats['failures']} "
                f"at={dt.datetime.now().isoformat(timespec='seconds')}")
    return stats
