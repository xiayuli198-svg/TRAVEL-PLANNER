"""12306 公开查询接口的最小 HTTP 客户端（个人使用，内置限速与重试）。"""
from __future__ import annotations

import http.cookiejar
import http.client
import json
import math
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from typing import Optional

BASE = "https://kyfw.12306.cn"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

DEFAULT_REFERER = BASE + "/otn/leftTicket/init"
DEFAULT_DELAY = 1.0


class CrawlCancelled(Exception):
    """用户主动取消，包括正在等待限速或重试时。"""


class RateLimited(RuntimeError):
    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


class TemporaryRequestError(RuntimeError):
    """传输重试耗尽，爬取器可冷却后继续当前工作项。"""


def _retry_after(value: Optional[str]) -> float:
    """Retry-After 支持秒数和 HTTP 日期；不缩短服务端要求的等待。"""
    if not value:
        return 0.0
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0.0
    return max(0.0, seconds) if math.isfinite(seconds) else 0.0


class Client12306:
    def __init__(self, delay: float = DEFAULT_DELAY, timeout: float = 15,
                 cancel_cb=None, wait_cb=None):
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("请求间隔必须是非负有限数")
        self.delay = self.min_delay = delay
        self.timeout = timeout
        self.cancel_cb = cancel_cb
        self.wait_cb = wait_cb
        self._successes = 0
        self._last_req = 0.0
        self.reset_session()

    def reset_session(self) -> None:
        """重建 Cookie 会话，但保留已退让的速度和上次请求时间。"""
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def check_cancelled(self) -> None:
        if self.cancel_cb and self.cancel_cb():
            raise CrawlCancelled("已取消，断点已保存")

    def wait(self, seconds: float, phase: str = "", reason: str = "") -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        next_update = 0.0
        while True:
            self.check_cancelled()
            now = time.monotonic()
            remaining = max(0.0, deadline - now)
            if phase and self.wait_cb and (now >= next_update or not remaining):
                self.wait_cb(phase, remaining, reason)
                next_update = now + 5
            if not remaining:
                return
            time.sleep(min(0.25, remaining))

    def slow_down(self) -> None:
        self.delay = max(self.min_delay, min(30.0, max(1.0, self.delay * 2)))
        self._successes = 0

    def _on_success(self) -> None:
        self._successes += 1
        if self._successes >= 20:
            self.delay = max(self.min_delay, self.delay * 0.9)
            self._successes = 0

    def _throttle(self) -> None:
        wait = self.delay - (time.monotonic() - self._last_req)
        self.wait(max(0.0, wait))

    def get(self, path: str, params: Optional[dict] = None,
            referer: Optional[str] = None, retries: int = 3,
            binary: bool = False):
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        last_err: Optional[Exception] = None
        for attempt in range(max(1, retries)):
            self._throttle()
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": referer or DEFAULT_REFERER,
                "X-Requested-With": "XMLHttpRequest",
            })
            self._last_req = time.monotonic()
            try:
                with self.opener.open(req, timeout=self.timeout) as resp:
                    data = resp.read()
                if binary:
                    return data
                text = data.decode("utf-8-sig")
                if text.lstrip()[:1] == "<":
                    raise RateLimited(f"疑似风控页面(HTML, {len(data)} 字节)")
                try:
                    result = json.loads(text)
                except json.JSONDecodeError:
                    raise RateLimited(
                        f"非 JSON 响应 ({len(data)} 字节): {text[:200]!r}")
                self._on_success()
                return result
            except urllib.error.HTTPError as e:
                retry_after = _retry_after(e.headers.get("Retry-After") if e.headers else None)
                e.close()
                if e.code in (403, 429) or (e.code == 503 and retry_after):
                    raise RateLimited(f"HTTP {e.code}，服务端要求减速", retry_after) from e
                if e.code not in (408, 500, 502, 503, 504):
                    raise
                last_err = e
            except (OSError, http.client.HTTPException, ValueError) as e:
                last_err = e
            # 传输重试只在这一层做；末次失败不再额外睡眠。
            if attempt + 1 < max(1, retries):
                self.slow_down()
                self.wait(min(30.0, 2.0 ** attempt) + random.uniform(0, 0.5),
                          "retrying", f"连接暂时失败，重试 {attempt + 2}/{retries}")
        error_type = TemporaryRequestError if isinstance(
            last_err, (OSError, http.client.HTTPException, urllib.error.HTTPError)) else RuntimeError
        raise error_type(f"GET {url} 失败: {last_err!r}") from last_err

    def warmup(self) -> None:
        """访问余票页初始化 Cookie，降低风控概率。"""
        try:
            self.get("/otn/leftTicket/init", binary=True, retries=2)
        except (CrawlCancelled, RateLimited):
            raise
        except Exception:
            pass
