"""Rate limiting, analytics buffering, observability, config, and the server adapter."""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from service.analytics import AnalyticsRecorder, parse_window
from service.config import Config, ConfigError
from service.errors import ValidationError
from service.models import ClickEvent
from service.observability import (
    JsonFormatter,
    MetricsRegistry,
    get_request_id,
    new_request_id,
    set_request_id,
)
from service.ratelimit import TokenBucketRateLimiter
from service.server import ShortenerServer
from service.storage import MemoryLinkStore

from .support import API_KEY, make_app, make_config


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RateLimiterTests(unittest.TestCase):
    def setUp(self):
        self.clock = _FakeClock()
        self.limiter = TokenBucketRateLimiter(capacity=3, refill_per_sec=1.0, clock=self.clock)

    def test_allows_up_to_capacity(self):
        for _ in range(3):
            self.assertTrue(self.limiter.allow("a")[0])

    def test_refuses_beyond_capacity(self):
        for _ in range(3):
            self.limiter.allow("a")
        allowed, retry_after = self.limiter.allow("a")
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0)

    def test_refills_over_time(self):
        for _ in range(3):
            self.limiter.allow("a")
        self.clock.advance(2.0)
        self.assertTrue(self.limiter.allow("a")[0])
        self.assertTrue(self.limiter.allow("a")[0])
        self.assertFalse(self.limiter.allow("a")[0])

    def test_refill_is_capped_at_capacity(self):
        self.limiter.allow("a")
        self.clock.advance(10_000)
        for _ in range(3):
            self.assertTrue(self.limiter.allow("a")[0])
        self.assertFalse(self.limiter.allow("a")[0])

    def test_identities_are_independent(self):
        for _ in range(3):
            self.limiter.allow("a")
        self.assertTrue(self.limiter.allow("b")[0])

    def test_cost_is_honoured(self):
        self.assertTrue(self.limiter.allow("a", cost=3.0)[0])
        self.assertFalse(self.limiter.allow("a", cost=1.0)[0])

    def test_retry_after_reflects_the_deficit(self):
        self.limiter.allow("a", cost=3.0)
        _, retry_after = self.limiter.allow("a", cost=2.0)
        self.assertAlmostEqual(retry_after, 2.0, places=3)

    def test_idle_buckets_are_reclaimed(self):
        for index in range(20):
            self.limiter.allow("id-%d" % index)
        self.assertEqual(self.limiter.size(), 20)
        self.clock.advance(4000)   # past both the sweep interval and the idle TTL
        self.limiter.allow("fresh")
        self.assertEqual(self.limiter.size(), 1)

    def test_bucket_count_is_bounded(self):
        limiter = TokenBucketRateLimiter(capacity=1, refill_per_sec=1.0, max_buckets=10,
                                         clock=self.clock)
        for index in range(50):
            limiter.allow("id-%d" % index)
        self.assertLessEqual(limiter.size(), 10)

    def test_invalid_configuration_rejected(self):
        for kwargs in ({"capacity": 0}, {"refill_per_sec": 0}):
            with self.assertRaises(ValueError):
                TokenBucketRateLimiter(capacity=kwargs.get("capacity", 1),
                                       refill_per_sec=kwargs.get("refill_per_sec", 1.0))


class AnalyticsRecorderTests(unittest.TestCase):
    def test_synchronous_mode_writes_immediately(self):
        store = MemoryLinkStore()
        recorder = AnalyticsRecorder(store, queue_size=0)
        recorder.record(ClickEvent(code="abc", timestamp=time.time()))
        self.assertEqual(store.stats("abc", 7).total_clicks, 1)

    def test_disabled_recorder_writes_nothing(self):
        store = MemoryLinkStore()
        recorder = AnalyticsRecorder(store, queue_size=0, enabled=False)
        recorder.record(ClickEvent(code="abc", timestamp=time.time()))
        self.assertEqual(store.stats("abc", 7).total_clicks, 0)

    def test_asynchronous_events_are_eventually_written(self):
        store = MemoryLinkStore()
        recorder = AnalyticsRecorder(store, queue_size=128)
        try:
            for _ in range(25):
                recorder.record(ClickEvent(code="abc", timestamp=time.time()))
            self.assertTrue(recorder.flush(timeout=5.0))
            self.assertEqual(store.stats("abc", 7).total_clicks, 25)
        finally:
            recorder.close()

    def test_a_failing_store_never_raises_into_the_caller(self):
        class Exploding(MemoryLinkStore):
            def record_click(self, event):
                raise RuntimeError("disk on fire")

        recorder = AnalyticsRecorder(Exploding(), queue_size=0)
        recorder.record(ClickEvent(code="abc", timestamp=time.time()))  # must not raise

    def test_close_is_safe_in_synchronous_mode(self):
        AnalyticsRecorder(MemoryLinkStore(), queue_size=0).close()


class WindowParsingTests(unittest.TestCase):
    def test_default(self):
        self.assertEqual(parse_window(None), 7)

    def test_days_weeks_hours(self):
        self.assertEqual(parse_window("30d"), 30)
        self.assertEqual(parse_window("2w"), 14)
        self.assertEqual(parse_window("24h"), 1)
        self.assertEqual(parse_window("25h"), 2)

    def test_bare_number(self):
        self.assertEqual(parse_window("5"), 5)

    def test_rejects_garbage(self):
        with self.assertRaises(ValidationError):
            parse_window("banana")

    def test_rejects_out_of_range(self):
        for value in ("0d", "400d"):
            with self.assertRaises(ValidationError):
                parse_window(value)


class MetricsRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = MetricsRegistry()

    def test_counter_accumulates(self):
        self.registry.increment("hits")
        self.registry.increment("hits", 2)
        self.assertEqual(self.registry.snapshot()["counters"]["hits"], 3)

    def test_labels_separate_series(self):
        self.registry.increment("hits", labels={"route": "a"})
        self.registry.increment("hits", labels={"route": "b"})
        counters = self.registry.snapshot()["counters"]
        self.assertEqual(counters['hits{route="a"}'], 1)
        self.assertEqual(counters['hits{route="b"}'], 1)

    def test_histogram_records_count_and_sum(self):
        for value in (0.001, 0.02, 1.5):
            self.registry.observe("latency", value)
        histogram = self.registry.snapshot()["histograms"]["latency"]
        self.assertEqual(histogram["count"], 3)
        self.assertAlmostEqual(histogram["sum"], 1.521, places=6)

    def test_timer_observes_elapsed_time(self):
        with self.registry.timer("timed"):
            pass
        self.assertEqual(self.registry.snapshot()["histograms"]["timed"]["count"], 1)

    def test_gauge_replaces_rather_than_accumulates(self):
        self.registry.set_gauge("depth", 5)
        self.registry.set_gauge("depth", 2)
        self.assertEqual(self.registry.snapshot()["gauges"]["depth"], 2)

    def test_prometheus_rendering_includes_types_and_buckets(self):
        self.registry.increment("requests_total", labels={"code": "200"})
        self.registry.observe("latency_seconds", 0.02)
        rendered = self.registry.render_prometheus()
        self.assertIn("# TYPE requests_total counter", rendered)
        self.assertIn('requests_total{code="200"} 1', rendered)
        self.assertIn("latency_seconds_bucket", rendered)
        self.assertIn('le="+Inf"', rendered)

    def test_reset_clears_everything(self):
        self.registry.increment("x")
        self.registry.reset()
        self.assertEqual(self.registry.snapshot()["counters"], {})

    def test_concurrent_increments_are_not_lost(self):
        def worker():
            for _ in range(500):
                self.registry.increment("concurrent")

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.registry.snapshot()["counters"]["concurrent"], 2000)


class LoggingTests(unittest.TestCase):
    def test_request_id_is_thread_local(self):
        set_request_id("outer")
        seen = {}

        def worker():
            seen["inner"] = get_request_id()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(get_request_id(), "outer")
        self.assertIsNone(seen["inner"])
        set_request_id(None)

    def test_formatter_emits_json_with_extras(self):
        import logging

        record = logging.LogRecord("shortener.test", logging.INFO, __file__, 1,
                                   "hello", (), None)
        record.code = "abc123"
        payload = json.loads(JsonFormatter().format(record))
        self.assertEqual(payload["msg"], "hello")
        self.assertEqual(payload["code"], "abc123")
        self.assertEqual(payload["level"], "INFO")

    def test_request_ids_are_unique(self):
        self.assertEqual(len({new_request_id() for _ in range(200)}), 200)


class ConfigTests(unittest.TestCase):
    def test_auth_required_without_keys_is_rejected(self):
        # Silently running unauthenticated would be the worst possible default.
        with self.assertRaises(ConfigError):
            Config(db_path=":memory:", require_auth=True, api_keys=frozenset())

    def test_short_code_length_is_rejected(self):
        with self.assertRaises(ConfigError):
            Config(db_path=":memory:", require_auth=False, code_length=2)

    def test_unsupported_redirect_status_is_rejected(self):
        with self.assertRaises(ConfigError):
            Config(db_path=":memory:", require_auth=False, redirect_status=418)

    def test_short_url_normalises_a_trailing_slash(self):
        config = Config(db_path=":memory:", require_auth=False, base_url="http://s.test/")
        self.assertEqual(config.short_url("abc"), "http://s.test/abc")

    def test_from_env_reads_overrides(self):
        import os

        os.environ["SHORTENER_PORT"] = "9099"
        os.environ["SHORTENER_API_KEYS"] = "one,two"
        os.environ["SHORTENER_ALLOW_PRIVATE_HOSTS"] = "true"
        try:
            config = Config.from_env()
            self.assertEqual(config.port, 9099)
            self.assertEqual(config.api_keys, frozenset({"one", "two"}))
            self.assertTrue(config.allow_private_hosts)
            self.assertEqual(config.base_url, "http://127.0.0.1:9099")
        finally:
            for key in ("SHORTENER_PORT", "SHORTENER_API_KEYS", "SHORTENER_ALLOW_PRIVATE_HOSTS"):
                os.environ.pop(key, None)

    def test_from_env_rejects_a_non_integer(self):
        import os

        os.environ["SHORTENER_PORT"] = "not-a-port"
        try:
            with self.assertRaises(ConfigError):
                Config.from_env()
        finally:
            os.environ.pop("SHORTENER_PORT", None)


class ServerIntegrationTests(unittest.TestCase):
    """End-to-end over a real socket, so the HTTP adapter is genuinely covered."""

    @classmethod
    def setUpClass(cls):
        cls.config = make_config(host="127.0.0.1", port=0)
        app, _, _ = make_app(cls.config)
        cls.server = ShortenerServer(cls.config, application=app)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.bound_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown_gracefully()

    def request(self, method, path, body=None, headers=None, follow=True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("X-API-Key", API_KEY)
        if data:
            req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        opener = urllib.request.build_opener() if follow else urllib.request.build_opener(
            _NoRedirect())
        try:
            with opener.open(req, timeout=10) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def test_health_over_http(self):
        status, body, _ = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_create_and_redirect_over_http(self):
        status, body, _ = self.request("POST", "/api/v1/links",
                                       {"url": "https://example.com/socket"})
        self.assertEqual(status, 201)
        code = json.loads(body)["code"]
        status, _, headers = self.request("GET", "/" + code, follow=False)
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "https://example.com/socket")

    def test_error_over_http(self):
        status, body, _ = self.request("GET", "/definitelymissing", follow=False)
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")

    def test_server_does_not_advertise_the_python_version(self):
        _, _, headers = self.request("GET", "/healthz")
        self.assertNotIn("Python", headers.get("Server", ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


if __name__ == "__main__":
    unittest.main()
