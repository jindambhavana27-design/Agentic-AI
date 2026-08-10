"""HTTP behaviour: routing, status codes, auth, headers, error shape."""

import json
import time
import unittest

from service.app import Request, parse_target
from service.models import Link
from service.observability import METRICS
from service.ratelimit import TokenBucketRateLimiter

from .support import API_KEY, call, create_link, make_app, make_config


class CreateEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app, self.service, self.store = make_app()

    def test_creates_and_returns_201(self):
        status, body, headers = call(self.app, "POST", "/api/v1/links",
                                     {"url": "https://example.com/a"})
        self.assertEqual(status, 201)
        self.assertEqual(body["url"], "https://example.com/a")
        self.assertEqual(headers["Location"], body["short_url"])

    def test_short_url_uses_the_configured_base(self):
        body = create_link(self.app)
        self.assertTrue(body["short_url"].startswith("http://short.test/"))

    def test_rejects_missing_url(self):
        status, body, _ = call(self.app, "POST", "/api/v1/links", {})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_rejects_unsafe_url_with_a_typed_code(self):
        status, body, _ = call(self.app, "POST", "/api/v1/links",
                               {"url": "http://192.168.0.1/"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "unsafe_url")

    def test_rejects_malformed_json(self):
        path, query = parse_target("/api/v1/links")
        response = self.app.handle(Request("POST", path, query,
                                           {"x-api-key": API_KEY}, b"{not json"))
        self.assertEqual(response.status, 400)

    def test_rejects_non_object_body(self):
        path, query = parse_target("/api/v1/links")
        response = self.app.handle(Request("POST", path, query,
                                           {"x-api-key": API_KEY}, b"[1,2,3]"))
        self.assertEqual(response.status, 400)

    def test_rejects_oversized_body(self):
        config = make_config(max_body_bytes=64)
        app, _, _ = make_app(config)
        status, body, _ = call(app, "POST", "/api/v1/links",
                               {"url": "https://example.com/" + "a" * 200})
        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "payload_too_large")

    def test_alias_conflict_returns_409(self):
        create_link(self.app, alias="promo1")
        status, body, _ = call(self.app, "POST", "/api/v1/links",
                               {"url": "https://example.com/b", "alias": "promo1"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "conflict")

    def test_idempotency_key_replay_returns_200(self):
        first = call(self.app, "POST", "/api/v1/links", {"url": "https://example.com/a"},
                     headers={"Idempotency-Key": "abc-123"})
        second = call(self.app, "POST", "/api/v1/links", {"url": "https://example.com/a"},
                      headers={"Idempotency-Key": "abc-123"})
        self.assertEqual(first[0], 201)
        self.assertEqual(second[0], 200)
        self.assertEqual(first[1]["code"], second[1]["code"])

    def test_blank_idempotency_key_rejected(self):
        status, _, _ = call(self.app, "POST", "/api/v1/links",
                            {"url": "https://example.com/a"},
                            headers={"Idempotency-Key": "   "})
        self.assertEqual(status, 400)


class RedirectTests(unittest.TestCase):
    def setUp(self):
        self.app, self.service, self.store = make_app()

    def test_redirects_with_the_configured_status(self):
        body = create_link(self.app, "https://example.com/target")
        status, _, headers = call(self.app, "GET", "/" + body["code"], api_key=None)
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "https://example.com/target")

    def test_redirect_status_is_configurable(self):
        app, _, _ = make_app(make_config(redirect_status=301))
        body = create_link(app)
        status, _, _ = call(app, "GET", "/" + body["code"], api_key=None)
        self.assertEqual(status, 301)

    def test_redirect_does_not_require_auth(self):
        body = create_link(self.app)
        status, _, _ = call(self.app, "GET", "/" + body["code"], api_key=None)
        self.assertEqual(status, 302)

    def test_redirect_suppresses_referrer_and_caching(self):
        # The short code must not leak to the destination, and a cached redirect
        # would outlive a link we later retire.
        body = create_link(self.app)
        _, _, headers = call(self.app, "GET", "/" + body["code"], api_key=None)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("no-store", headers["Cache-Control"])

    def test_unknown_code_returns_404(self):
        status, body, _ = call(self.app, "GET", "/doesnotexist", api_key=None)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_expired_code_returns_410(self):
        self.store.create(Link(code="gone12", target_url="https://example.com/a",
                               created_at=time.time() - 10, expires_at=time.time() - 1))
        status, body, _ = call(self.app, "GET", "/gone12", api_key=None)
        self.assertEqual(status, 410)
        self.assertEqual(body["error"]["code"], "gone")

    def test_api_paths_are_not_shadowed_by_the_catch_all(self):
        status, _, _ = call(self.app, "GET", "/healthz", api_key=None)
        self.assertEqual(status, 200)


class ManagementEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app, self.service, self.store = make_app()

    def test_get_returns_metadata(self):
        created = create_link(self.app, metadata={"campaign": "spring"})
        status, body, _ = call(self.app, "GET", "/api/v1/links/" + created["code"])
        self.assertEqual(status, 200)
        self.assertEqual(body["metadata"], {"campaign": "spring"})

    def test_delete_returns_204_then_410(self):
        created = create_link(self.app)
        self.assertEqual(call(self.app, "DELETE", "/api/v1/links/" + created["code"])[0], 204)
        self.assertEqual(call(self.app, "GET", "/" + created["code"], api_key=None)[0], 410)

    def test_delete_of_unknown_returns_404(self):
        self.assertEqual(call(self.app, "DELETE", "/api/v1/links/unknown1")[0], 404)

    def test_list_returns_items_and_cursor_field(self):
        create_link(self.app, "https://example.com/1")
        create_link(self.app, "https://example.com/2")
        status, body, _ = call(self.app, "GET", "/api/v1/links?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["items"]), 1)
        self.assertIn("next_cursor", body)

    def test_list_rejects_a_non_integer_limit(self):
        self.assertEqual(call(self.app, "GET", "/api/v1/links?limit=abc")[0], 400)

    def test_list_rejects_a_malformed_cursor(self):
        self.assertEqual(call(self.app, "GET", "/api/v1/links?cursor=!!!")[0], 400)

    def test_stats_reports_clicks(self):
        created = create_link(self.app)
        call(self.app, "GET", "/" + created["code"], api_key=None)
        call(self.app, "GET", "/" + created["code"], api_key=None)
        status, body, _ = call(self.app, "GET",
                               "/api/v1/links/%s/stats?window=7d" % created["code"])
        self.assertEqual(status, 200)
        self.assertEqual(body["total_clicks"], 2)
        self.assertEqual(body["window_days"], 7)

    def test_stats_rejects_a_bad_window(self):
        created = create_link(self.app)
        status, _, _ = call(self.app, "GET",
                            "/api/v1/links/%s/stats?window=banana" % created["code"])
        self.assertEqual(status, 400)

    def test_stats_for_unknown_code_is_404(self):
        self.assertEqual(call(self.app, "GET", "/api/v1/links/nothing/stats")[0], 404)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.app, _, _ = make_app()

    def test_missing_key_is_401(self):
        status, body, _ = call(self.app, "POST", "/api/v1/links",
                               {"url": "https://example.com/a"}, api_key=None)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthenticated")

    def test_wrong_key_is_401(self):
        status, _, _ = call(self.app, "POST", "/api/v1/links",
                            {"url": "https://example.com/a"}, api_key="wrong")
        self.assertEqual(status, 401)

    def test_auth_can_be_disabled_for_local_development(self):
        app, _, _ = make_app(make_config(require_auth=False, api_keys=frozenset()))
        self.assertEqual(call(app, "POST", "/api/v1/links",
                              {"url": "https://example.com/a"}, api_key=None)[0], 201)

    def test_owner_is_derived_without_storing_the_key(self):
        created = create_link(self.app)
        owner = self.app.service.store.get(created["code"]).created_by
        self.assertTrue(owner.startswith("key-"))
        self.assertNotIn(API_KEY, owner)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.app, _, _ = make_app()

    def test_unknown_path_is_404(self):
        self.assertEqual(call(self.app, "GET", "/api/v1/nope/deep", api_key=None)[0], 404)

    def test_wrong_method_is_405(self):
        status, body, _ = call(self.app, "PUT", "/api/v1/links", {"url": "https://e.com/a"})
        self.assertEqual(status, 405)
        self.assertEqual(body["error"]["code"], "method_not_allowed")

    def test_security_headers_are_always_present(self):
        _, _, headers = call(self.app, "GET", "/healthz", api_key=None)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_request_id_is_echoed(self):
        _, _, headers = call(self.app, "GET", "/healthz", api_key=None,
                             headers={"X-Request-Id": "trace-123"})
        self.assertEqual(headers["X-Request-Id"], "trace-123")

    def test_hostile_request_id_is_sanitised(self):
        _, _, headers = call(self.app, "GET", "/healthz", api_key=None,
                             headers={"X-Request-Id": "a\r\nInjected: 1"})
        self.assertNotIn("\n", headers["X-Request-Id"])

    def test_error_body_carries_the_request_id(self):
        _, body, headers = call(self.app, "GET", "/nothinghere", api_key=None)
        self.assertEqual(body["error"]["request_id"], headers["X-Request-Id"])

    def test_healthz_and_readyz(self):
        self.assertEqual(call(self.app, "GET", "/healthz", api_key=None)[1]["status"], "ok")
        self.assertEqual(call(self.app, "GET", "/readyz", api_key=None)[1]["status"], "ready")

    def test_metrics_are_exposed_in_prometheus_format(self):
        call(self.app, "GET", "/healthz", api_key=None)
        status, body, headers = call(self.app, "GET", "/metrics", api_key=None)
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        self.assertIn("http_requests_total", body)

    def test_internal_errors_do_not_leak_details(self):
        app, service, _ = make_app()

        def boom(*_args, **_kwargs):
            raise RuntimeError("internal detail that must not escape")

        service.get_link = boom
        status, body, _ = call(app, "GET", "/api/v1/links/anything")
        self.assertEqual(status, 500)
        self.assertNotIn("internal detail", json.dumps(body))


class RateLimitTests(unittest.TestCase):
    def test_over_limit_returns_429_with_retry_after(self):
        limiter = TokenBucketRateLimiter(capacity=2, refill_per_sec=0.01)
        app, _, _ = make_app(make_config(rate_limit_enabled=True), limiter=limiter)
        # Creation costs 2 tokens, so the second call must be refused.
        self.assertEqual(call(app, "POST", "/api/v1/links",
                              {"url": "https://example.com/a"})[0], 201)
        status, body, headers = call(app, "POST", "/api/v1/links",
                                     {"url": "https://example.com/b"})
        self.assertEqual(status, 429)
        self.assertEqual(body["error"]["code"], "rate_limited")
        self.assertIn("Retry-After", headers)

    def test_limits_are_per_identity(self):
        limiter = TokenBucketRateLimiter(capacity=2, refill_per_sec=0.01)
        app, _, _ = make_app(make_config(rate_limit_enabled=True, require_auth=False,
                                         api_keys=frozenset()), limiter=limiter)
        self.assertEqual(call(app, "GET", "/healthz", api_key=None,
                              remote_addr="198.51.100.1")[0], 200)
        self.assertEqual(call(app, "GET", "/healthz", api_key=None,
                              remote_addr="198.51.100.1")[0], 200)
        self.assertEqual(call(app, "GET", "/healthz", api_key=None,
                              remote_addr="198.51.100.1")[0], 429)
        # A different source address has its own untouched bucket.
        self.assertEqual(call(app, "GET", "/healthz", api_key=None,
                              remote_addr="198.51.100.2")[0], 200)


if __name__ == "__main__":
    unittest.main()
