"""Storage adapters.

Both adapters are driven through the same test body. Divergence between the
in-memory store used by tests and the SQLite store used in production would make
the rest of the suite prove nothing, so the contract is asserted against both.
"""

import os
import shutil
import tempfile
import time
import unittest

from service.errors import ConflictError, ValidationError
from service.models import ClickEvent, Link
from service.storage import MemoryLinkStore
from service.storage.sqlite_store import (
    SQLiteLinkStore,
    _decode_cursor,
    _encode_cursor,
    _normalise_referrer,
)


class StoreContractMixin:
    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()
        self.now = time.time()

    def link(self, code, **overrides):
        payload = {
            "code": code,
            "target_url": "https://example.com/%s" % code,
            "created_at": self.now,
        }
        payload.update(overrides)
        return Link(**payload)

    # -- links ---------------------------------------------------------------

    def test_create_and_get(self):
        self.store.create(self.link("aaa"))
        self.assertEqual(self.store.get("aaa").target_url, "https://example.com/aaa")

    def test_get_unknown_is_none(self):
        self.assertIsNone(self.store.get("nope"))

    def test_duplicate_code_conflicts(self):
        self.store.create(self.link("dup"))
        with self.assertRaises(ConflictError):
            self.store.create(self.link("dup"))

    def test_metadata_round_trips(self):
        self.store.create(self.link("meta", metadata={"a": 1, "b": "two", "c": True}))
        self.assertEqual(self.store.get("meta").metadata, {"a": 1, "b": "two", "c": True})

    def test_expiry_round_trips(self):
        self.store.create(self.link("exp", expires_at=self.now + 60))
        self.assertAlmostEqual(self.store.get("exp").expires_at, self.now + 60, places=3)

    def test_custom_alias_flag_round_trips(self):
        self.store.create(self.link("alias", custom_alias=True))
        self.assertTrue(self.store.get("alias").custom_alias)

    def test_get_returns_deleted_rows_so_callers_can_answer_410(self):
        self.store.create(self.link("del"))
        self.store.soft_delete("del", self.now)
        self.assertIsNotNone(self.store.get("del"))
        self.assertIsNotNone(self.store.get("del").deleted_at)

    def test_soft_delete_is_idempotent(self):
        self.store.create(self.link("del2"))
        self.assertTrue(self.store.soft_delete("del2", self.now))
        self.assertFalse(self.store.soft_delete("del2", self.now))

    def test_soft_delete_of_unknown_is_false(self):
        self.assertFalse(self.store.soft_delete("ghost", self.now))

    # -- idempotency ---------------------------------------------------------

    def test_idempotency_lookup(self):
        self.store.create(self.link("idem", idempotency_key="k1", created_by="u1"))
        self.assertEqual(self.store.find_by_idempotency_key("k1", "u1").code, "idem")

    def test_idempotency_is_scoped_to_the_owner(self):
        self.store.create(self.link("idem2", idempotency_key="k1", created_by="u1"))
        self.assertIsNone(self.store.find_by_idempotency_key("k1", "u2"))

    def test_duplicate_idempotency_key_conflicts(self):
        self.store.create(self.link("idem3", idempotency_key="k9", created_by="u1"))
        with self.assertRaises(ConflictError):
            self.store.create(self.link("idem4", idempotency_key="k9", created_by="u1"))

    def test_null_keys_do_not_collide(self):
        self.store.create(self.link("n1"))
        self.store.create(self.link("n2"))
        self.assertIsNotNone(self.store.get("n2"))

    # -- pagination ----------------------------------------------------------

    def test_listing_is_newest_first(self):
        for index in range(3):
            self.store.create(self.link("p%d" % index, created_at=self.now + index))
        page, _ = self.store.list(10, None)
        self.assertEqual([l.code for l in page], ["p2", "p1", "p0"])

    def test_listing_excludes_deleted(self):
        self.store.create(self.link("keep"))
        self.store.create(self.link("drop"))
        self.store.soft_delete("drop", self.now)
        page, _ = self.store.list(10, None)
        self.assertEqual([l.code for l in page], ["keep"])

    def test_cursor_pagination_covers_every_row_exactly_once(self):
        for index in range(7):
            self.store.create(self.link("q%d" % index, created_at=self.now + index))
        seen, cursor = [], None
        while True:
            page, cursor = self.store.list(3, cursor)
            seen.extend(l.code for l in page)
            if not cursor:
                break
        self.assertEqual(sorted(seen), sorted("q%d" % i for i in range(7)))
        self.assertEqual(len(seen), len(set(seen)))

    def test_no_cursor_on_the_last_page(self):
        self.store.create(self.link("solo"))
        _, cursor = self.store.list(10, None)
        self.assertIsNone(cursor)

    # -- analytics -----------------------------------------------------------

    def test_clicks_aggregate_per_day(self):
        self.store.create(self.link("clk"))
        for _ in range(3):
            self.store.record_click(ClickEvent(code="clk", timestamp=self.now))
        stats = self.store.stats("clk", 7)
        self.assertEqual(stats.total_clicks, 3)
        self.assertEqual(stats.unique_days, 1)

    def test_clicks_without_a_referrer_are_direct(self):
        self.store.create(self.link("clk2"))
        self.store.record_click(ClickEvent(code="clk2", timestamp=self.now))
        self.assertEqual(self.store.stats("clk2", 7).top_referrers, {"direct": 1})

    def test_referrers_are_reduced_to_origins(self):
        self.store.create(self.link("clk3"))
        self.store.record_click(ClickEvent(code="clk3", timestamp=self.now,
                                           referrer="https://a.example/x?y=1"))
        self.store.record_click(ClickEvent(code="clk3", timestamp=self.now,
                                           referrer="https://a.example/z"))
        self.assertEqual(self.store.stats("clk3", 7).top_referrers, {"https://a.example": 2})

    def test_stats_window_excludes_old_days(self):
        self.store.create(self.link("clk4"))
        self.store.record_click(ClickEvent(code="clk4", timestamp=self.now - 40 * 86400))
        self.assertEqual(self.store.stats("clk4", 7).total_clicks, 0)
        self.assertEqual(self.store.stats("clk4", 60).total_clicks, 1)

    def test_stats_for_a_link_with_no_clicks(self):
        self.store.create(self.link("quiet"))
        stats = self.store.stats("quiet", 7)
        self.assertEqual(stats.total_clicks, 0)
        self.assertIsNone(stats.first_click_at)

    def test_health(self):
        self.assertTrue(self.store.health())


class MemoryStoreTests(StoreContractMixin, unittest.TestCase):
    def make_store(self):
        return MemoryLinkStore()


class SQLiteStoreTests(StoreContractMixin, unittest.TestCase):
    def make_store(self):
        self.tmp = tempfile.mkdtemp(prefix="shortener-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        return SQLiteLinkStore(os.path.join(self.tmp, "links.db"))

    def test_schema_migrations_are_idempotent(self):
        path = os.path.join(self.tmp, "again.db")
        first = SQLiteLinkStore(path)
        first.create(Link(code="pre", target_url="https://example.com/", created_at=time.time()))
        first.close()
        second = SQLiteLinkStore(path)
        self.assertIsNotNone(second.get("pre"))
        second.close()

    def test_creates_the_parent_directory(self):
        nested = os.path.join(self.tmp, "deep", "nested", "links.db")
        store = SQLiteLinkStore(nested)
        self.assertTrue(os.path.exists(os.path.dirname(nested)))
        store.close()


class CursorTests(unittest.TestCase):
    def test_round_trip(self):
        cursor = _encode_cursor(1234.5678, "abc")
        self.assertEqual(_decode_cursor(cursor), (1234.5678, "abc"))

    def test_cursor_is_url_safe(self):
        cursor = _encode_cursor(1.0, "a-b_c")
        self.assertNotIn("+", cursor)
        self.assertNotIn("/", cursor)

    def test_malformed_cursor_raises_validation_error(self):
        with self.assertRaises(ValidationError):
            _decode_cursor("!!!not base64!!!")


class ReferrerNormalisationTests(unittest.TestCase):
    def test_empty_is_direct(self):
        self.assertEqual(_normalise_referrer(None), "direct")
        self.assertEqual(_normalise_referrer(""), "direct")

    def test_origin_is_extracted(self):
        self.assertEqual(_normalise_referrer("https://x.example/a/b?c=d"), "https://x.example")

    def test_garbage_is_unknown(self):
        self.assertEqual(_normalise_referrer("not a url"), "unknown")


if __name__ == "__main__":
    unittest.main()
