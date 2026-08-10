"""Input validation and URL safety checks.

The redirector is a request-forging primitive if it is allowed to point at
internal addresses, so target URLs are validated against scheme, shape, and
address-space rules before they are ever persisted.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Optional
from urllib.parse import urlsplit

from .config import Config
from .errors import UnsafeUrlError, ValidationError

# Codes and aliases share one keyspace, so the alias grammar must be a superset
# of what the generator can emit.
ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")

# Paths the router owns. An alias colliding with one of these would be shadowed
# by the route table and silently never resolve.
RESERVED_ALIASES = frozenset(
    {
        "api", "healthz", "readyz", "metrics", "static", "admin", "login",
        "logout", "favicon.ico", "robots.txt", "docs", "openapi.json", "_next",
    }
)

# Hostnames that resolve inside the deployment perimeter regardless of DNS.
BLOCKED_HOST_SUFFIXES = (
    ".localhost", ".local", ".internal", ".cluster.local", ".consul",
)
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        # Cloud instance-metadata endpoints; the classic SSRF escalation target.
        "metadata.google.internal",
        "instance-data",
    }
)


def validate_alias(alias: str) -> str:
    alias = alias.strip()
    if not ALIAS_RE.match(alias):
        raise ValidationError(
            "alias must be 3-32 characters of [A-Za-z0-9_-]",
            {"alias": alias},
        )
    if alias.lower() in RESERVED_ALIASES:
        raise ValidationError("alias is reserved", {"alias": alias})
    return alias


def validate_ttl(ttl: Optional[int]) -> Optional[int]:
    if ttl is None:
        return None
    if not isinstance(ttl, int) or isinstance(ttl, bool):
        raise ValidationError("ttl_seconds must be an integer", {"ttl_seconds": ttl})
    if ttl <= 0:
        raise ValidationError("ttl_seconds must be positive", {"ttl_seconds": ttl})
    if ttl > 10 * 365 * 24 * 3600:
        raise ValidationError("ttl_seconds exceeds the 10 year maximum", {"ttl_seconds": ttl})
    return ttl


def _is_disallowed_ip(host: str) -> bool:
    """True when ``host`` is a literal IP inside a non-routable range."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_target_url(raw: str, config: Config) -> str:
    """Normalise and safety-check a redirect target.

    Returns the URL to persist. Raises :class:`ValidationError` or
    :class:`UnsafeUrlError` for anything we refuse to shorten.
    """
    if not isinstance(raw, str):
        raise ValidationError("url must be a string", {"url": repr(raw)})

    url = raw.strip()
    if not url:
        raise ValidationError("url must not be empty")
    if len(url) > config.max_url_length:
        raise ValidationError(
            "url exceeds maximum length of %d" % config.max_url_length,
            {"length": len(url)},
        )
    # Control characters enable response splitting once the value is echoed
    # back in a Location header.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise ValidationError("url contains control characters")

    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise ValidationError("url could not be parsed: %s" % exc) from exc

    scheme = parts.scheme.lower()
    if scheme not in config.allowed_schemes:
        raise UnsafeUrlError(
            "url scheme %r is not allowed" % (scheme or "<none>",),
            {"allowed": sorted(config.allowed_schemes)},
        )

    if not parts.hostname:
        raise ValidationError("url must include a host")

    # Embedded credentials get leaked into logs and referrer headers.
    if parts.username or parts.password:
        raise UnsafeUrlError("url must not embed credentials")

    host = parts.hostname.lower().rstrip(".")

    if config.allow_private_hosts:
        return url

    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_HOST_SUFFIXES):
        raise UnsafeUrlError("url host is not publicly routable", {"host": host})

    if _is_disallowed_ip(host):
        raise UnsafeUrlError("url host is not publicly routable", {"host": host})

    # A hostname without a dot cannot be a public FQDN; it would resolve via
    # local search domains.
    if "." not in host:
        raise UnsafeUrlError("url host is not a fully qualified domain name", {"host": host})

    return url
