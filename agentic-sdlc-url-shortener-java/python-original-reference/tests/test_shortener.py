"""Domain rules: code allocation, resolution, expiry, idempotency, deletion."""

import time
import unittest
from unittest import mock

from service.analytics import AnalyticsRecorder
from service.config import Config
from service.errors import (
    CodeExhaustionError,
    ConflictError,
    GoneError,
    NotFoundError,
    UnsafeUrlError,
    ValidationError,
)
from service.models import Link
from service.shortener import ShortenerService
from service.storage import MemoryLinkStore


def build_service(**overrides):
    config = Config(db_path=":memory:", require_auth=False,
                    base_url="http://short.test", **overrides)
    store = MemoryLinkStore()
    return ShortenerService(store, config, AnalyticsRecorder(store, queue_size=0)), store


class CreateTests(unittest.TestCase):
    def setUp(self):
        self.service, self.store = build_service()

    def test_generates_a_code_of_configured_length(self):
        link, created = self.service.create_link("https://example.com/a")
        self.assertTrue(created)
        self.assertEqual(len(link.code), self.service.config.code_length)

    def test_generated_codes_are_distinct(self):
        codes = {self.service.create_link("https://example.com/%d" % i)[0].code
                 for i in range(50)}
        self.assertEqual(len(codes), 50)

    def test_codes_use_only_the_configured_alphabet(self):
        link, _ = self.service.create_link("https://example.com/a")
        self.assertTrue(set(link.code) <= set(self.service.config.code_alphabet))

    def test_custom_alias_is_honoured(self):
        link, _ = self.service.create_link("https://example.com/a", alias="promo")
        self.assertEqual(link.code, "promo")
        self.assertTrue(link.custom_alias)

    def test_duplicate_alias_conflicts(self):
        self.service.create_link("https://example.com/a", alias="promo")
        with self.assertRaises(ConflictError):
            self.service.create_link("https://example.com/b", alias="promo")

    def test_conflicting_alias_does_not_overwrite(self):
        self.service.create_link("https://example.com/a", alias="promo")
        with self.assertRaises(ConflictError):
            self.service.create_link("https://example.com/b", alias="promo")
        self.assertEqual(self.store.get("promo").target_url, "https://example.com/a")

    def test_unsafe_url_rejected_before_persistence(self):
        with self.assertRaises(UnsafeUrlError):
            self.service.create_link("http://169.254.169.254/")
        self.assertEqual(self.service.list_links()[0], [])

    def test_ttl_sets_expiry(self):
        link, _ = self.service.create_link("https://example.com/a", ttl_seconds=60)
        self.assertIsNotNone(link.expires_at)
        self.assertAlmostEqual(link.expires_at - link.created_at, 60, delta=1)

    def test_default_ttl_is_applied(self):
        service, _ = build_service(default_ttl_seconds=30)
        link, _ = service.create_link("https://example.com/a")
        self.assertAlmostEqual(link.expires_at - link.created_at, 30, delta=1)

    def test_explicit_ttl_overrides_the_default(self):
        service, _ = build_service(default_ttl_seconds=30)
        link, _ = service.create_link("https://example.com/a", ttl_seconds=90)
        self.assertAlmostEqual(link.expires_at - link.created_at, 90, delta=1)

    def test_metadata_is_stored(self):
        link, _ = self.service.create_link("https://example.com/a",
                                           metadata={"campaign": "spring", "n": 3})
        self.assertEqual(link.metadata, {"campaign": "spring", "n": 3})

    def test_metadata_must_be_an_object(self):
        with self.assertRaises(ValidationError):
            self.service.create_link("https://example.com/a", metadata=["a"])

    def test_metadata_key_limit_enforced(self):
        with self.assertRaises(ValidationError):
            self.service.create_link("https://example.com/a",
                                     metadata={str(i): i for i in range(17)})

    def test_metadata_rejects_nested_values(self):
        with self.assertRaises(ValidationError):
            self.service.create_link("https://example.com/a", metadata={"a": {"b": 1}})

    def test_metadata_rejects_overlong_values(self):
        with self.assertRaises(ValidationError):
            self.service.create_link("https://example.com/a", metadata={"a": "x" * 300})

    def test_code_exhaustion_surfaces_as_an_error(self):
        # Force every generated code to collide with an existing link.
        self.service.create_link("https://example.com/a", alias="fixed")
        with mock.patch.object(self.service, "generate_code", return_value="fixed"):
            with self.assertRaises(CodeExhaustionError):
                self.service.create_link("https://example.com/b")

    def test_collision_is_retried(self):
        self.service.create_link("https://example.com/a", alias="taken1")
        with mock.patch.object(self.service, "generate_code",
                               side_effect=["taken1", "fresh1"]):
            link, created = self.service.create_link("https://example.com/b")
        self.assertTrue(created)
        self.assertEqual(link.code, "fresh1")


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.service, self.store = build_service()

    def test_replay_returns_the_same_link(self):
        first, created_first = self.service.create_link(
            "https://example.com/a", owner="u1", idempotency_key="k1")
        second, created_second = self.service.create_link(
            "https://example.com/a", owner="u1", idempotency_key="k1")
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.code, second.code)

    def test_replay_with_a_different_url_conflicts(self):
        self.service.create_link("https://example.com/a", owner="u1", idempotency_key="k1")
        with self.assertRaises(ConflictError):
            self.service.create_link("https://example.com/b", owner="u1", idempotency_key="k1")

    def test_keys_are_scoped_per_owner(self):
        first, _ = self.service.create_link("https://example.com/a", owner="u1",
                                            idempotency_key="k1")
        second, created = self.service.create_link("https://example.com/a", owner="u2",
                                                   idempotency_key="k1")
        self.assertTrue(created)
        self.assertNotEqual(first.code, second.code)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.service, self.store = build_service()

    def test_resolves_an_active_link(self):
        link, _ = self.service.create_link("https://example.com/a", alias="live1")
        self.assertEqual(self.service.resolve("live1").target_url, "https://example.com/a")

    def test_unknown_code_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.resolve("missing")

    def test_expired_link_is_gone_not_missing(self):
        # 410 vs 404 is the difference between "was retired" and "never existed".
        self.store.create(Link(code="oldie", target_url="https://example.com/a",
                               created_at=time.time() - 100, expires_at=time.time() - 1))
        with self.assertRaises(GoneError):
            self.service.resolve("oldie")

    def test_deleted_link_is_gone(self):
        self.service.create_link("https://example.com/a", alias="bye12")
        self.service.delete_link("bye12")
        with self.assertRaises(GoneError):
            self.service.resolve("bye12")

    def test_resolution_records_a_click(self):
        self.service.create_link("https://example.com/a", alias="clik1")
        self.service.resolve("clik1")
        self.service.resolve("clik1")
        self.assertEqual(self.service.stats("clik1").total_clicks, 2)

    def test_recording_can_be_suppressed(self):
        self.service.create_link("https://example.com/a", alias="quiet")
        self.service.resolve("quiet", record=False)
        self.assertEqual(self.service.stats("quiet").total_clicks, 0)

    def test_referrer_is_reduced_to_an_origin(self):
        # Retaining the full referrer would store other sites' path and query data.
        self.service.create_link("https://example.com/a", alias="refer")
        self.service.resolve("refer", referrer="https://news.example.org/story?id=99")
        self.assertEqual(self.service.stats("refer").top_referrers,
                         {"https://news.example.org": 1})


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.service, self.store = build_service()

    def test_deletes_an_existing_link(self):
        self.service.create_link("https://example.com/a", alias="del01")
        self.service.delete_link("del01")
        self.assertIsNotNone(self.store.get("del01").deleted_at)

    def test_delete_is_soft_so_the_code_is_never_reissued(self):
        self.service.create_link("https://example.com/a", alias="del02")
        self.service.delete_link("del02")
        self.assertIsNotNone(self.store.get("del02"))
        with self.assertRaises(ConflictError):
            self.service.create_link("https://example.com/b", alias="del02")

    def test_deleting_twice_reports_gone(self):
        self.service.create_link("https://example.com/a", alias="del03")
        self.service.delete_link("del03")
        with self.assertRaises(GoneError):
            self.service.delete_link("del03")

    def test_deleting_unknown_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.delete_link("nope12")


class ListingAndStatsTests(unittest.TestCase):
    def setUp(self):
        self.service, self.store = build_service()

    def test_listing_excludes_deleted(self):
        self.service.create_link("https://example.com/a", alias="keep1")
        self.service.create_link("https://example.com/b", alias="drop1")
        self.service.delete_link("drop1")
        codes = [l.code for l in self.service.list_links()[0]]
        self.assertEqual(codes, ["keep1"])

    def test_listing_paginates_with_a_cursor(self):
        for index in range(5):
            self.store.create(Link(code="c%d" % index,
                                   target_url="https://example.com/%d" % index,
                                   created_at=1000.0 + index))
        first, cursor = self.service.list_links(limit=2)
        self.assertEqual([l.code for l in first], ["c4", "c3"])
        self.assertIsNotNone(cursor)
        second, _ = self.service.list_links(limit=2, cursor=cursor)
        self.assertEqual([l.code for l in second], ["c2", "c1"])

    def test_listing_limit_is_bounded(self):
        for limit in (0, 201):
            with self.assertRaises(ValidationError):
                self.service.list_links(limit=limit)

    def test_stats_for_unknown_code_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.stats("nobody")

    def test_get_link_returns_metadata(self):
        self.service.create_link("https://example.com/a", alias="meta1",
                                 metadata={"x": "y"})
        self.assertEqual(self.service.get_link("meta1").metadata, {"x": "y"})


class PublicRepresentationTests(unittest.TestCase):
    def test_public_dict_builds_the_short_url(self):
        service, _ = build_service()
        link, _ = service.create_link("https://example.com/a", alias="pub01")
        payload = link.to_public_dict("http://short.test/")
        self.assertEqual(payload["short_url"], "http://short.test/pub01")
        self.assertIsNone(payload["expires_at"])

    def test_expiry_is_rendered_as_iso(self):
        service, _ = build_service()
        link, _ = service.create_link("https://example.com/a", ttl_seconds=60)
        self.assertRegex(link.to_public_dict("http://s")["expires_at"],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


if __name__ == "__main__":
    unittest.main()
