"""Runtime configuration for the URL shortener service.

Configuration is read once from the environment and frozen. Nothing in the
service reads ``os.environ`` directly -- that keeps tests hermetic and makes the
full configuration surface visible in one place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import FrozenSet, Optional

_TRUE = {"1", "true", "yes", "on"}


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError("%s must be an integer, got %r" % (name, raw)) from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError("%s must be a number, got %r" % (name, raw)) from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in _TRUE


class ConfigError(Exception):
    """Raised when the environment holds an unusable configuration."""


@dataclass(frozen=True)
class Config:
    # --- HTTP ---------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8080
    base_url: str = "http://127.0.0.1:8080"
    max_body_bytes: int = 16 * 1024

    # --- Storage ------------------------------------------------------------
    # ":memory:" selects the in-process store; any other value is an SQLite path.
    db_path: str = "shortener.db"

    # --- Shortening ---------------------------------------------------------
    code_length: int = 7
    code_alphabet: str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    max_collision_retries: int = 5
    max_url_length: int = 2048
    redirect_status: int = 302
    default_ttl_seconds: Optional[int] = None

    # --- Safety -------------------------------------------------------------
    # Allowing private hosts is only safe in local development; in any shared
    # environment it turns the redirector into an SSRF pivot.
    allow_private_hosts: bool = False
    allowed_schemes: FrozenSet[str] = frozenset({"http", "https"})
    api_keys: FrozenSet[str] = field(default_factory=frozenset)
    require_auth: bool = True

    # --- Rate limiting ------------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_capacity: int = 60
    rate_limit_refill_per_sec: float = 1.0

    # --- Analytics ----------------------------------------------------------
    analytics_enabled: bool = True
    analytics_raw_retention: int = 10_000

    # --- Observability ------------------------------------------------------
    log_level: str = "INFO"
    service_name: str = "url-shortener"

    def __post_init__(self) -> None:
        if self.code_length < 4:
            raise ConfigError("SHORTENER_CODE_LENGTH must be >= 4 to keep the keyspace sparse")
        if self.redirect_status not in (301, 302, 307, 308):
            raise ConfigError("SHORTENER_REDIRECT_STATUS must be one of 301/302/307/308")
        if self.require_auth and not self.api_keys:
            raise ConfigError(
                "SHORTENER_API_KEYS must be set when auth is required; "
                "set SHORTENER_REQUIRE_AUTH=false only for local development"
            )
        if self.max_url_length < 16:
            raise ConfigError("SHORTENER_MAX_URL_LENGTH is implausibly small")

    @property
    def public_base(self) -> str:
        return self.base_url.rstrip("/")

    def short_url(self, code: str) -> str:
        return "%s/%s" % (self.public_base, code)

    @classmethod
    def from_env(cls) -> "Config":
        keys = _env_str("SHORTENER_API_KEYS", "")
        api_keys = frozenset(k.strip() for k in keys.split(",") if k.strip())
        ttl_raw = _env_int("SHORTENER_DEFAULT_TTL_SECONDS", 0)
        port = _env_int("SHORTENER_PORT", 8080)
        return cls(
            host=_env_str("SHORTENER_HOST", "127.0.0.1"),
            port=port,
            base_url=_env_str("SHORTENER_BASE_URL", "http://127.0.0.1:%d" % port),
            max_body_bytes=_env_int("SHORTENER_MAX_BODY_BYTES", 16 * 1024),
            db_path=_env_str("SHORTENER_DB_PATH", "shortener.db"),
            code_length=_env_int("SHORTENER_CODE_LENGTH", 7),
            max_url_length=_env_int("SHORTENER_MAX_URL_LENGTH", 2048),
            redirect_status=_env_int("SHORTENER_REDIRECT_STATUS", 302),
            default_ttl_seconds=ttl_raw if ttl_raw > 0 else None,
            allow_private_hosts=_env_bool("SHORTENER_ALLOW_PRIVATE_HOSTS", False),
            api_keys=api_keys,
            require_auth=_env_bool("SHORTENER_REQUIRE_AUTH", True),
            rate_limit_enabled=_env_bool("SHORTENER_RATE_LIMIT_ENABLED", True),
            rate_limit_capacity=_env_int("SHORTENER_RATE_LIMIT_CAPACITY", 60),
            rate_limit_refill_per_sec=_env_float("SHORTENER_RATE_LIMIT_REFILL", 1.0),
            analytics_enabled=_env_bool("SHORTENER_ANALYTICS_ENABLED", True),
            analytics_raw_retention=_env_int("SHORTENER_ANALYTICS_RAW_RETENTION", 10_000),
            log_level=_env_str("SHORTENER_LOG_LEVEL", "INFO"),
        )
