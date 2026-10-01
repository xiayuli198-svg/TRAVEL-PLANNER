"""高德地理编码的负缓存语义测试（不联网，用假 key + 打桩 URL 层）。

为什么单独立这个：`geo_cache` 会把查不到的名字记成 `failed=1` 免得反复撞，
但这也让「换 key 之后重试失败项」变成一个**看起来在跑、其实一个请求都没发**的空转 ——
实测日志是「新增成功 0 失败 685」，很容易被误读成「新 key 也不能用」。

所以这里的断言就两条：
1. 默认行为：负缓存命中时**不发请求**（这是缓存该有的样子）；
2. `retry_failed=True`：**必须真的发请求**，成功就把这一行改回可用坐标。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from travel_planner import db
from travel_planner.ingest import amap

GOOD_JSON = ('{"status":"1","info":"OK","infocode":"10000",'
             '"geocodes":[{"location":"116.378059,39.867679","formatted_address":"北京南站"}]}')
FAIL_JSON = '{"status":"0","info":"INVALID_USER_KEY","infocode":"10001","geocodes":[]}'


class _FakeResponse:
    def __init__(self, payload: str):
        self.payload = payload.encode("utf-8")

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestNegativeCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn: sqlite3.Connection = db.connect(Path(self.tmp.name) / "t.db")
        self.calls: list[str] = []

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _fake_urlopen(self, payload: str):
        def _open(url, timeout=0):
            self.calls.append(str(url))
            return _FakeResponse(payload)
        return _open

    def _seed_failed(self, name: str = "轮台站", city: str = "库尔勒") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO geo_cache(name,city,lon,lat,fetched_at,failed) "
                "VALUES(?,?,0,0,'2026-01-01T00:00:00',1)", (name, city))

    def test_negative_cache_short_circuits_by_default(self):
        self._seed_failed()
        with patch("urllib.request.urlopen", self._fake_urlopen(GOOD_JSON)):
            got = amap.geocode(self.conn, "k", "轮台站", "库尔勒")
        self.assertIsNone(got, "负缓存命中时应当直接返回 None")
        self.assertEqual(self.calls, [], "负缓存命中时不该发请求（这正是缓存的意义）")

    def test_retry_failed_really_calls_the_api_and_updates(self):
        self._seed_failed()
        with patch("urllib.request.urlopen", self._fake_urlopen(GOOD_JSON)):
            got = amap.geocode(self.conn, "k", "轮台站", "库尔勒", retry_failed=True)
        self.assertIsNotNone(got, "retry_failed=True 时必须真的去问高德")
        self.assertEqual(len(self.calls), 1, f"应当正好发一次请求，实际 {len(self.calls)} 次")
        row = self.conn.execute(
            "SELECT lon,lat,failed FROM geo_cache WHERE name='轮台站' AND city='库尔勒'"
        ).fetchone()
        self.assertEqual(row["failed"], 0, "成功后要把负缓存改回可用")
        self.assertAlmostEqual(row["lon"], 116.378059, places=5)
        self.assertAlmostEqual(row["lat"], 39.867679, places=5)

    def test_retry_failed_keeps_failed_state_when_still_bad(self):
        """换 key 也没救回来的名字要保持 failed=1，别把负缓存弄丢（否则每次都白撞一次）。"""
        self._seed_failed("不存在的站", "不存在")
        with patch("urllib.request.urlopen", self._fake_urlopen(FAIL_JSON)):
            got = amap.geocode(self.conn, "k", "不存在的站", "不存在", retry_failed=True)
        self.assertIsNone(got)
        self.assertEqual(len(self.calls), 1)
        row = self.conn.execute(
            "SELECT failed FROM geo_cache WHERE name='不存在的站'").fetchone()
        self.assertEqual(row["failed"], 1, "仍然失败就要继续记成失败")

    def test_successful_cache_is_not_refetched_even_with_retry_failed(self):
        """已经成功的行不该被 retry_failed 反复重查（那是 --force 的事）。"""
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO geo_cache(name,city,lon,lat,fetched_at,failed) "
                "VALUES('北京南站','北京',116.378,39.867,'2026-01-01T00:00:00',0)")
        with patch("urllib.request.urlopen", self._fake_urlopen(GOOD_JSON)):
            got = amap.geocode(self.conn, "k", "北京南站", "北京", retry_failed=True)
        self.assertIsNotNone(got)
        self.assertEqual(self.calls, [], "已成功的缓存不该因为 retry_failed 而重查")


if __name__ == "__main__":
    unittest.main()
