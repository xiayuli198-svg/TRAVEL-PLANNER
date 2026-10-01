"""后台任务：把「一次一小时的爬取」搬到网页上，但要人点确认才启动。

原则（与使用者约定一致）：
* **不自动爬**：只有 UI/接口收到明确请求才会创建任务；服务启动、定时器都不会自己爬。
* **可见**：进度、阶段、日志、预计剩余都在 ``snapshot()`` 里，页面轮询即可。
* **可停**：``cancel()`` 置位后，爬取会在当前车次结束时收工；断点已落盘，可 ``resume`` 续爬。
* **单进程单任务**：同一时刻只允许一个爬取任务，避免 12306 风控和 SQLite 写冲突。
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Dict, List, Optional

from . import db
from .ingest.client12306 import DEFAULT_DELAY

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_PARTIAL = "partial"
_FINAL = (STATE_DONE, STATE_FAILED, STATE_CANCELLED, STATE_PARTIAL)


class CrawlJob:
    """单个日期的爬取任务（线程内跑 ``crawl.crawl_date``）。"""

    def __init__(self, date: str, *, delay: float = DEFAULT_DELAY, resume: bool = True,
                 db_path: Optional[str] = None, ods=None, label: str = ""):
        self.date = date
        self.delay = delay
        self.resume = resume
        self.db_path = db_path
        self.ods = list(ods) if ods else None      # 定向爬取：只爬这些 OD 对
        self.label = label or (f"定向 {len(self.ods)} 对" if self.ods else "全国枢纽")
        self.state = STATE_RUNNING
        self.phase = "准备"
        self.done = 0
        self.total = 0
        self.extra: Dict = {}
        self.error = ""
        self.started = dt.datetime.now()
        self.finished: Optional[dt.datetime] = None
        self.logs: List[str] = []
        self.stats: Dict = {}
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- 供爬取线程回调 ----
    def _log(self, message: str) -> None:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"{stamp} {message}")
        if len(self.logs) > 400:
            del self.logs[:100]

    def _on_progress(self, phase: str, done: int, total: int, extra: Dict) -> None:
        previous_phase = self.phase
        self.phase = {"pairs": "发现车次", "trains": "抓取停站", "cancelled": "已取消",
                      "cooldown": "限速冷却", "retrying": "连接重试"}.get(phase, phase)
        self.done, self.total = int(done), int(total)
        self.extra = dict(extra or {})
        # 每 20 步记一条，日志不至于淹掉
        if previous_phase != self.phase or (
                phase not in ("cooldown", "retrying") and (self.done % 20 == 0 or self.done == self.total)):
            self._log(f"{self.phase} {self.done}/{self.total} " +
                      " ".join(f"{k}={v}" for k, v in self.extra.items()))

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # ---- 生命周期 ----
    def start(self) -> "CrawlJob":
        self._thread = threading.Thread(target=self._run, name=f"crawl-{self.date}", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        from .ingest import crawl

        conn = None
        try:
            conn = db.connect(self.db_path)
            self._log(f"开始爬取 {self.date}（{self.label}，delay={self.delay}s，resume={self.resume}）")
            stats = crawl.crawl_date(conn, self.date, ods=self.ods, delay=self.delay,
                                     resume=self.resume,
                                     progress_cb=self._on_progress,
                                     cancel_cb=self.cancelled)
            self.stats = stats or {}
            if self.stats.get("cancelled") or self._cancel.is_set():
                self.state = STATE_CANCELLED
                self._log("已取消（断点已保存，可勾选『续爬』继续）")
            elif self.stats.get("state") == STATE_PARTIAL or self.stats.get("failures", 0):
                self.state = STATE_PARTIAL
                self._log(f"部分完成：入库 {self.stats.get('ingested', 0)} 趟，"
                          f"{self.stats.get('failures', 0)} 项失败，可续爬补齐")
            else:
                self.state = STATE_DONE
                self._log(f"完成：入库 {self.stats.get('ingested', 0)} 趟，"
                          f"发现 {self.stats.get('discovered', 0)} 趟，"
                          f"耗时 {self.stats.get('seconds', 0)}s")
        except Exception as exc:                       # noqa: BLE001
            self.state = STATE_FAILED
            self.error = f"{type(exc).__name__}: {exc}"
            self._log("失败：" + self.error)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:                      # noqa: BLE001
                    pass
            self.finished = dt.datetime.now()

    def cancel(self) -> None:
        if self.state == STATE_RUNNING:
            self._cancel.set()
            self._log("收到取消请求，等当前车次结束…")

    # ---- 快照 ----
    def snapshot(self, *, include_logs: int = 40) -> Dict:
        end = self.finished or dt.datetime.now()
        elapsed = (end - self.started).total_seconds()
        pct = round(self.done / self.total * 100) if self.total else None
        eta = None
        if self.state == STATE_RUNNING and self.done and self.total and not self.extra.get("retry_in"):
            eta = round(elapsed / self.done * (self.total - self.done))
        return {
            "date": self.date,
            "state": self.state,
            "phase": self.phase,
            "label": self.label,
            "pairs": len(self.ods) if self.ods else 0,
            "done": self.done,
            "total": self.total,
            "percent": pct,
            "elapsed": round(elapsed),
            "eta": eta,
            "extra": self.extra,
            "error": self.error,
            "stats": self.stats,
            "logs": self.logs[-include_logs:],
            "finished": self.finished.isoformat(timespec="seconds") if self.finished else "",
        }

    def persisted(self) -> Dict:
        """落盘成 meta，页面刷新/服务重启后还能看出「哪天爬过」。"""
        return {
            "date": self.date,
            "state": self.state,
            "ingested": self.stats.get("ingested", 0),
            "seconds": self.stats.get("seconds", 0),
            "finished": self.finished.isoformat(timespec="seconds") if self.finished else "",
            "error": self.error,
        }


class JobRegistry:
    """进程内的任务表：同一时刻最多一个爬取任务。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, CrawlJob] = {}
        self._current: Optional[CrawlJob] = None

    def start_crawl(self, date: str, *, delay: float = DEFAULT_DELAY, resume: bool = True,
                    db_path: Optional[str] = None, force: bool = False,
                    ods=None, label: str = "") -> Dict:
        with self._lock:
            cur = self._current
            # force 保留为兼容参数，不能让两个爬虫叠加请求频率。
            if cur and cur.state == STATE_RUNNING:
                return {"ok": False,
                        "error": f"已有爬取任务在跑（{cur.date}），先等它结束或取消",
                        "job": cur.snapshot()}
            job = CrawlJob(date, delay=delay, resume=resume, db_path=db_path,
                           ods=ods, label=label)
            self._jobs[date] = job
            self._current = job
        job.start()
        return {"ok": True, "job": job.snapshot()}

    def get(self, date: str) -> Optional[CrawlJob]:
        return self._jobs.get(date)

    def current(self) -> Optional[CrawlJob]:
        return self._current

    def any_running(self) -> bool:
        cur = self._current
        return bool(cur and cur.state == STATE_RUNNING)

    def cancel(self, date: Optional[str] = None) -> Dict:
        job = self._jobs.get(date) if date else self._current
        if not job:
            return {"ok": False, "error": "没有这个爬取任务"}
        job.cancel()
        return {"ok": True, "job": job.snapshot()}

    def snapshots(self, *, include_logs: int = 40) -> List[Dict]:
        return [j.snapshot(include_logs=include_logs)
                for j in sorted(self._jobs.values(), key=lambda x: x.started, reverse=True)]


#: 全局注册表（web.py 与 CLI 共用）
REGISTRY = JobRegistry()


def wait_for(job: CrawlJob, timeout: float = 5.0) -> bool:
    """测试用：等任务收尾。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.state in _FINAL:
            return True
        time.sleep(0.05)
    return job.state in _FINAL
