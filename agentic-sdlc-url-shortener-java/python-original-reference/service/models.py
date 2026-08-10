"""Domain entities.

These are plain data carriers with no persistence or transport concerns, so the
same objects flow through the store, the service layer, and the API encoder.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


def utc_now() -> float:
    return time.time()


@dataclass
class Link:
    code: str
    target_url: str
    created_at: float
    expires_at: Optional[float] = None
    deleted_at: Optional[float] = None
    created_by: Optional[str] = None
    idempotency_key: Optional[str] = None
    custom_alias: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self, now: Optional[float] = None) -> bool:
        if self.expires_at is None:
            return False
        return (now if now is not None else utc_now()) >= self.expires_at

    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def is_active(self, now: Optional[float] = None) -> bool:
        return not self.is_deleted() and not self.is_expired(now)

    def to_public_dict(self, base_url: str) -> Dict[str, Any]:
        return {
            "code": self.code,
            "short_url": "%s/%s" % (base_url.rstrip("/"), self.code),
            "url": self.target_url,
            "created_at": _iso(self.created_at),
            "expires_at": _iso(self.expires_at),
            "custom_alias": self.custom_alias,
            "metadata": self.metadata,
        }


@dataclass
class ClickEvent:
    code: str
    timestamp: float
    referrer: Optional[str] = None
    user_agent: Optional[str] = None
    country: Optional[str] = None


@dataclass
class LinkStats:
    code: str
    total_clicks: int
    unique_days: int
    first_click_at: Optional[float]
    last_click_at: Optional[float]
    clicks_by_day: Dict[str, int] = field(default_factory=dict)
    top_referrers: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "total_clicks": self.total_clicks,
            "unique_days": self.unique_days,
            "first_click_at": _iso(self.first_click_at),
            "last_click_at": _iso(self.last_click_at),
            "clicks_by_day": self.clicks_by_day,
            "top_referrers": self.top_referrers,
        }


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
