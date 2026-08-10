"""Core domain service: create, resolve, inspect, and retire short links.

This layer owns the business rules and knows nothing about HTTP. Everything the
API handler does is translate a request into one of these calls.
"""

from __future__ import annotations

import secrets
from typing import Any, Dict, Optional, Tuple

from .analytics import AnalyticsRecorder
from .config import Config
from .errors import (
    CodeExhaustionError,
    ConflictError,
    GoneError,
    NotFoundError,
    ValidationError,
)
from .models import ClickEvent, Link, LinkStats, utc_now
from .observability import METRICS, get_logger
from .storage.base import LinkStore
from .validation import validate_alias, validate_target_url, validate_ttl

_log = get_logger("shortener")

MAX_METADATA_KEYS = 16
MAX_METADATA_VALUE_LEN = 256


class ShortenerService:
    def __init__(
        self,
        store: LinkStore,
        config: Config,
        recorder: Optional[AnalyticsRecorder] = None,
    ) -> None:
        self.store = store
        self.config = config
        self.recorder = recorder or AnalyticsRecorder(store, enabled=config.analytics_enabled)

    # -- creation -------------------------------------------------------------

    def create_link(
        self,
        url: str,
        alias: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        owner: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[Link, bool]:
        """Create a short link.

        Returns ``(link, created)``. ``created`` is False when an idempotency
        key replayed an earlier request, which lets the caller answer 200
        instead of 201.
        """
        target = validate_target_url(url, self.config)
        ttl = validate_ttl(ttl_seconds if ttl_seconds is not None else self.config.default_ttl_seconds)
        meta = _validate_metadata(metadata)

        if idempotency_key is not None:
            existing = self.store.find_by_idempotency_key(idempotency_key, owner)
            if existing is not None:
                # Replaying a key against a different payload is a client bug;
                # surfacing it beats silently returning the wrong link.
                if existing.target_url != target:
                    raise ConflictError(
                        "idempotency key was already used with a different url",
                        {"code": existing.code},
                    )
                METRICS.increment("links_created_total", labels={"result": "idempotent_replay"})
                return existing, False

        now = utc_now()
        expires_at = now + ttl if ttl else None

        if alias is not None:
            code = validate_alias(alias)
            link = Link(
                code=code,
                target_url=target,
                created_at=now,
                expires_at=expires_at,
                created_by=owner,
                idempotency_key=idempotency_key,
                custom_alias=True,
                metadata=meta,
            )
            try:
                self.store.create(link)
            except ConflictError:
                METRICS.increment("links_created_total", labels={"result": "alias_conflict"})
                raise ConflictError("alias is already taken", {"alias": code})
            METRICS.increment("links_created_total", labels={"result": "created"})
            _log.info("created link", extra={"code": code, "custom_alias": True})
            return link, True

        return self._create_with_generated_code(target, now, expires_at, owner, idempotency_key, meta)

    def _create_with_generated_code(
        self,
        target: str,
        now: float,
        expires_at: Optional[float],
        owner: Optional[str],
        idempotency_key: Optional[str],
        meta: Dict[str, Any],
    ) -> Tuple[Link, bool]:
        # Random codes rather than a counter encoding: sequential codes make the
        # whole corpus enumerable, which leaks both volume and content.
        last_error: Optional[Exception] = None
        for attempt in range(self.config.max_collision_retries):
            code = self.generate_code()
            link = Link(
                code=code,
                target_url=target,
                created_at=now,
                expires_at=expires_at,
                created_by=owner,
                idempotency_key=idempotency_key,
                custom_alias=False,
                metadata=meta,
            )
            try:
                self.store.create(link)
            except ConflictError as exc:
                # Could be a code collision (retryable) or an idempotency-key
                # race (not). Re-check the key before burning another attempt.
                if idempotency_key is not None:
                    existing = self.store.find_by_idempotency_key(idempotency_key, owner)
                    if existing is not None:
                        METRICS.increment("links_created_total", labels={"result": "idempotent_race"})
                        return existing, False
                last_error = exc
                METRICS.increment("code_collisions_total")
                continue
            METRICS.increment("links_created_total", labels={"result": "created"})
            METRICS.observe("code_generation_attempts", attempt + 1)
            _log.info("created link", extra={"code": code, "attempts": attempt + 1})
            return link, True

        METRICS.increment("links_created_total", labels={"result": "exhausted"})
        raise CodeExhaustionError(
            "could not allocate a free short code after %d attempts"
            % self.config.max_collision_retries
        ) from last_error

    def generate_code(self) -> str:
        alphabet = self.config.code_alphabet
        return "".join(secrets.choice(alphabet) for _ in range(self.config.code_length))

    # -- resolution -----------------------------------------------------------

    def resolve(
        self,
        code: str,
        referrer: Optional[str] = None,
        user_agent: Optional[str] = None,
        record: bool = True,
    ) -> Link:
        """Return the active link for ``code`` and count the click.

        Raises :class:`NotFoundError` when the code was never issued and
        :class:`GoneError` when it existed but has expired or been deleted --
        the distinction matters to clients and to abuse investigation.
        """
        link = self.store.get(code)
        if link is None:
            METRICS.increment("redirects_total", labels={"result": "not_found"})
            raise NotFoundError("no such short link", {"code": code})
        if link.is_deleted():
            METRICS.increment("redirects_total", labels={"result": "deleted"})
            raise GoneError("short link has been deleted", {"code": code})
        if link.is_expired():
            METRICS.increment("redirects_total", labels={"result": "expired"})
            raise GoneError("short link has expired", {"code": code})

        if record:
            self.recorder.record(
                ClickEvent(code=code, timestamp=utc_now(), referrer=referrer, user_agent=user_agent)
            )
        METRICS.increment("redirects_total", labels={"result": "ok"})
        return link

    # -- inspection -----------------------------------------------------------

    def get_link(self, code: str) -> Link:
        link = self.store.get(code)
        if link is None:
            raise NotFoundError("no such short link", {"code": code})
        if link.is_deleted():
            raise GoneError("short link has been deleted", {"code": code})
        return link

    def stats(self, code: str, days: int = 7) -> LinkStats:
        self.get_link(code)  # 404/410 before returning empty analytics
        return self.store.stats(code, days)

    def list_links(self, limit: int = 50, cursor: Optional[str] = None):
        if limit < 1 or limit > 200:
            raise ValidationError("limit must be between 1 and 200", {"limit": limit})
        return self.store.list(limit, cursor)

    # -- retirement -----------------------------------------------------------

    def delete_link(self, code: str) -> None:
        """Soft-delete a link.

        Soft delete, not hard: the code must stay burned so it is never reissued
        to a different target, and abuse reports need the history.
        """
        if not self.store.soft_delete(code, utc_now()):
            existing = self.store.get(code)
            if existing is None:
                raise NotFoundError("no such short link", {"code": code})
            raise GoneError("short link has already been deleted", {"code": code})
        METRICS.increment("links_deleted_total")
        _log.info("deleted link", extra={"code": code})

    def close(self) -> None:
        self.recorder.close()
        self.store.close()


def _validate_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise ValidationError("metadata must be an object")
    if len(metadata) > MAX_METADATA_KEYS:
        raise ValidationError("metadata may not exceed %d keys" % MAX_METADATA_KEYS)
    clean: Dict[str, Any] = {}
    for key, value in metadata.items():
        if not isinstance(key, str) or not key:
            raise ValidationError("metadata keys must be non-empty strings")
        if not isinstance(value, (str, int, float, bool)):
            raise ValidationError("metadata values must be scalars", {"key": key})
        if isinstance(value, str) and len(value) > MAX_METADATA_VALUE_LEN:
            raise ValidationError(
                "metadata value exceeds %d characters" % MAX_METADATA_VALUE_LEN, {"key": key}
            )
        clean[key] = value
    return clean
