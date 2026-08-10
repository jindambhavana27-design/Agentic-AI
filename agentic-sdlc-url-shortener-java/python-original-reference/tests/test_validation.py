"""URL and alias validation, including the SSRF controls."""

import unittest

from service.config import Config
from service.errors import UnsafeUrlError, ValidationError
from service.validation import (
    RESERVED_ALIASES,
    validate_alias,
    validate_target_url,
    validate_ttl,
)


class TargetUrlTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(db_path=":memory:", require_auth=False)

    def accept(self, url):
        return validate_target_url(url, self.config)

    def test_accepts_https(self):
        self.assertEqual(self.accept("https://example.com/a"), "https://example.com/a")

    def test_accepts_http(self):
        self.assertEqual(self.accept("http://example.com"), "http://example.com")

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(self.accept("  https://example.com/a  "), "https://example.com/a")

    def test_rejects_non_string(self):
        with self.assertRaises(ValidationError):
            self.accept(12345)

    def test_rejects_empty(self):
        with self.assertRaises(ValidationError):
            self.accept("   ")

    def test_rejects_overlong(self):
        with self.assertRaises(ValidationError):
            self.accept("https://example.com/" + "a" * 4000)

    def test_rejects_control_characters(self):
        # A newline here would let an attacker inject a second header.
        with self.assertRaises(ValidationError):
            self.accept("https://example.com/\r\nX-Injected: 1")

    def test_rejects_javascript_scheme(self):
        with self.assertRaises(UnsafeUrlError):
            self.accept("javascript:alert(1)")

    def test_rejects_file_scheme(self):
        with self.assertRaises(UnsafeUrlError):
            self.accept("file:///etc/passwd")

    def test_rejects_data_scheme(self):
        with self.assertRaises(UnsafeUrlError):
            self.accept("data:text/html,<script>1</script>")

    def test_rejects_missing_host(self):
        with self.assertRaises(ValidationError):
            self.accept("https:///path")

    def test_rejects_embedded_credentials(self):
        with self.assertRaises(UnsafeUrlError):
            self.accept("https://user:pass@example.com/")

    def test_rejects_loopback_ip(self):
        with self.assertRaises(UnsafeUrlError):
            self.accept("http://127.0.0.1:8080/admin")

    def test_rejects_private_ip(self):
        for host in ("10.0.0.5", "192.168.1.1", "172.16.0.1"):
            with self.assertRaises(UnsafeUrlError):
                self.accept("http://%s/" % host)

    def test_rejects_link_local_metadata_address(self):
        # The classic cloud credential-theft target.
        with self.assertRaises(UnsafeUrlError):
            self.accept("http://169.254.169.254/latest/meta-data/")

    def test_rejects_ipv6_loopback(self):
        with self.assertRaises(UnsafeUrlError):
            self.accept("http://[::1]/")

    def test_rejects_localhost(self):
        with self.assertRaises(UnsafeUrlError):
            self.accept("http://localhost/")

    def test_rejects_internal_suffixes(self):
        for host in ("db.internal", "svc.cluster.local", "printer.local"):
            with self.assertRaises(UnsafeUrlError):
                self.accept("http://%s/" % host)

    def test_rejects_metadata_hostname(self):
        with self.assertRaises(UnsafeUrlError):
            self.accept("http://metadata.google.internal/")

    def test_rejects_single_label_host(self):
        with self.assertRaises(UnsafeUrlError):
            self.accept("http://intranet/")

    def test_trailing_dot_is_normalised_before_the_check(self):
        with self.assertRaises(UnsafeUrlError):
            self.accept("http://localhost./")

    def test_private_hosts_allowed_when_configured(self):
        permissive = Config(db_path=":memory:", require_auth=False, allow_private_hosts=True)
        self.assertEqual(validate_target_url("http://127.0.0.1:9/x", permissive),
                         "http://127.0.0.1:9/x")


class AliasTests(unittest.TestCase):
    def test_accepts_valid(self):
        self.assertEqual(validate_alias("my-link_1"), "my-link_1")

    def test_strips_whitespace(self):
        self.assertEqual(validate_alias("  abc  "), "abc")

    def test_rejects_too_short(self):
        with self.assertRaises(ValidationError):
            validate_alias("ab")

    def test_rejects_too_long(self):
        with self.assertRaises(ValidationError):
            validate_alias("a" * 33)

    def test_rejects_illegal_characters(self):
        for bad in ("has space", "sl/ash", "dot.dot", "emoji\U0001f600"):
            with self.assertRaises(ValidationError):
                validate_alias(bad)

    def test_rejects_reserved_aliases(self):
        for reserved in ("api", "metrics", "healthz"):
            self.assertIn(reserved, RESERVED_ALIASES)
            with self.assertRaises(ValidationError):
                validate_alias(reserved)

    def test_reserved_check_is_case_insensitive(self):
        with self.assertRaises(ValidationError):
            validate_alias("API")


class TtlTests(unittest.TestCase):
    def test_none_passes_through(self):
        self.assertIsNone(validate_ttl(None))

    def test_positive_accepted(self):
        self.assertEqual(validate_ttl(60), 60)

    def test_zero_rejected(self):
        with self.assertRaises(ValidationError):
            validate_ttl(0)

    def test_negative_rejected(self):
        with self.assertRaises(ValidationError):
            validate_ttl(-1)

    def test_bool_rejected(self):
        # True is an int in Python; accepting it would store a 1-second TTL.
        with self.assertRaises(ValidationError):
            validate_ttl(True)

    def test_string_rejected(self):
        with self.assertRaises(ValidationError):
            validate_ttl("60")

    def test_absurdly_large_rejected(self):
        with self.assertRaises(ValidationError):
            validate_ttl(10 * 365 * 24 * 3600 + 1)


if __name__ == "__main__":
    unittest.main()
