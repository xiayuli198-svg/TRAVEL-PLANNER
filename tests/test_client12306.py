"""离线验证传输重试、限速退让和可取消等待，不请求真实 12306。"""
import datetime as dt
import io
import unittest
from email.message import Message
from email.utils import format_datetime
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from travel_planner.ingest.client12306 import (
    Client12306, CrawlCancelled, RateLimited, TemporaryRequestError, _retry_after,
)
from travel_planner.ingest.schedule import query_left_ticket


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class TestClient12306(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        for name, value in (("time.monotonic", self.clock.monotonic),
                            ("time.sleep", self.clock.sleep), ("random.uniform", lambda *args: 0)):
            stub = patch("travel_planner.ingest.client12306." + name, value)
            stub.start()
            self.addCleanup(stub.stop)
        self.client = Client12306(delay=1)
        self.client.opener = Mock()

    def test_connection_reset_retries_and_preserves_minimum_spacing(self):
        sent = []

        def send(*args, **kwargs):
            sent.append(self.clock.now)
            if len(sent) == 1:
                raise ConnectionResetError("reset")
            return io.BytesIO(b'{"status": true}')

        self.client.opener.open.side_effect = send
        self.assertTrue(self.client.get("/test")["status"])
        self.assertEqual(len(sent), 2)
        self.assertGreaterEqual(sent[1] - sent[0], 2)
        self.assertGreaterEqual(self.client.delay, 2)

    def test_retry_exhaustion_has_no_extra_sleep_after_last_request(self):
        sent = []

        def send(*args, **kwargs):
            sent.append(self.clock.now)
            raise TimeoutError("timeout")

        self.client.opener.open.side_effect = send
        with self.assertRaises(TemporaryRequestError):
            self.client.get("/test", retries=3)
        self.assertEqual(len(sent), 3)
        self.assertEqual(self.clock.now, sent[-1])

    def test_rate_limit_is_propagated_without_short_retries(self):
        for code in (403, 429, 503):
            with self.subTest(code=code):
                headers = Message()
                headers["Retry-After"] = "120"
                body = io.BytesIO(b"busy")
                self.client.opener.open.reset_mock()
                self.client.opener.open.side_effect = HTTPError("https://example.test", code, "busy", headers, body)
                with self.assertRaises(RateLimited) as raised:
                    self.client.get("/test")
                self.assertEqual(raised.exception.retry_after, 120)
                self.client.opener.open.assert_called_once()
                self.assertTrue(body.closed)

    def test_html_is_treated_as_cooldown_not_three_fast_requests(self):
        self.client.opener.open.return_value = io.BytesIO(b"  <html>blocked</html>")
        with self.assertRaises(RateLimited):
            self.client.get("/test")
        self.client.opener.open.assert_called_once()

    def test_malformed_json_is_treated_as_cooldown(self):
        self.client.opener.open.return_value = io.BytesIO(b"not-json")
        with self.assertRaises(RateLimited):
            self.client.get("/test")
        self.client.opener.open.assert_called_once()

    def test_permanent_http_error_is_not_retried(self):
        self.client.opener.open.side_effect = HTTPError("https://example.test", 404, "missing", Message(), io.BytesIO())
        with self.assertRaises(HTTPError):
            self.client.get("/test")
        self.client.opener.open.assert_called_once()

    def test_cancellation_interrupts_long_wait_and_warmup(self):
        initial = self.clock.now
        self.client.cancel_cb = lambda: self.clock.now >= initial + 0.5
        updates = []
        self.client.wait_cb = lambda *args: updates.append(args)
        with self.assertRaises(CrawlCancelled):
            self.client.wait(300, "cooldown", "限速")
        self.assertLess(self.clock.now - initial, 1)
        self.assertEqual(updates[0][0], "cooldown")
        with self.assertRaises(CrawlCancelled):
            self.client.warmup()
        self.client.opener.open.assert_not_called()

    def test_session_reset_and_success_do_not_erase_adaptive_delay(self):
        self.client.slow_down()
        self.client.reset_session()
        self.assertEqual(self.client.delay, 2)
        self.client.opener = Mock()
        self.client.opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(b"{}")
        for _ in range(20):
            self.client.get("/test")
        self.assertAlmostEqual(self.client.delay, 1.8)
        self.assertGreaterEqual(self.client.delay, self.client.min_delay)

    def test_retry_after_seconds_dates_and_invalid_values(self):
        timestamp = 1801000000
        future = dt.datetime.fromtimestamp(timestamp + 90, dt.timezone.utc)
        with patch("travel_planner.ingest.client12306.time.time", return_value=timestamp):
            self.assertEqual(_retry_after(format_datetime(future, usegmt=True)), 90)
        self.assertEqual(_retry_after("4000"), 4000)
        for value in (None, "invalid", "-1", "nan", "inf"):
            self.assertEqual(_retry_after(value), 0)


class TestTicketResponse(unittest.TestCase):
    def test_valid_empty_result_is_allowed(self):
        client = Mock()
        client.get.return_value = {"status": True, "data": {"result": []}}
        self.assertEqual(query_left_ticket(client, "2099-01-01", "AAA", "BBB"), [])

    def test_error_json_must_not_mark_od_as_completed(self):
        client = Mock()
        for payload in ({"data": {}}, {"status": False, "data": {"result": []}},
                        [], {"data": {"result": ["broken"]}}):
            with self.subTest(payload=payload):
                client.get.return_value = payload
                with self.assertRaises(ValueError):
                    query_left_ticket(client, "2099-01-01", "AAA", "BBB")


if __name__ == "__main__":
    unittest.main()
