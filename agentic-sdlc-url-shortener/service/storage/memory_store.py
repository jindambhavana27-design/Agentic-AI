"""In-process :class:`LinkStore`.

Used by the test suite and by ``SHORTENER_DB_PATH=:memory:``. It implements the
same semantics as the SQLite adapter -- including idempotency uniqueness and
keyset pagination -- so tests written against it stay meaningful.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from ..errors import ConflictError
from ..models import ClickEvent, Link, LinkStats
from .base import LinkStore
from .sqlite_store import _decode_cursor, _encode_cursor, _normalise_referrer, _day_key


class MemoryLinkStore(LinkStore):
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._links: Dict[str, Link] = {}
        self._idem: Dict[Tuple[Optional[str], str], str] = {}
        self._daily: Dict[Tuple[str, str], Dict[str, float]] = {}
        self._referrers: Dict[str, Dict[str, int]] = defaultdict(dict)

    def create(self, link: Link) -> Link:
        with self._lock:
            if link.code in self._links:
                raise ConflictError("code already exists", {"code": link.code})
            if link.idempotency_key is not None:
                key = (link.created_by, link.idempotency_key)
                if key in self._idem:
                    raise ConflictError("idempotency key already used")
                self._idem[key] = link.code
            self._links[link.code] = link
            return link

    def get(self, code: str) -> Optional[Link]:
        with self._lock:
            return self._links.get(code)

    def find_by_idempotency_key(self, key: str, owner: Optional[str]) -> Optional[Link]:
        with self._lock:
            code = self._idem.get((owner, key))
            return self._links.get(code) if code else None

    def soft_delete(self, code: str, at: float) -> bool:
        with self._lock:
            link = self._links.get(code)
            if link is None or link.deleted_at is not None:
                return False
            link.deleted_at = at
            return True

    def list(self, limit: int, cursor: Optional[str]) -> Tuple[List[Link], Optional[str]]:
        with self._lock:
            items = [l for l in self._links.values() if l.deleted_at is None]
        items.sort(key=lambda l: (l.created_at, l.code), reverse=True)
        if cursor:
            created_at, code = _decode_cursor(cursor)
            items = [l for l in items if (l.created_at, l.code) < (created_at, code)]
        page = items[: limit + 1]
        has_more = len(page) > limit
        page = page[:limit]
        next_cursor = _encode_cursor(page[-1].created_at, page[-1].code) if has_more and page else None
        return page, next_cursor

    def record_click(self, event: ClickEvent) -> None:
        day = _day_key(event.timestamp)
        referrer = _normalise_referrer(event.referrer)
        with self._lock:
            bucket = self._daily.setdefault(
                (event.code, day), {"clicks": 0, "first_at": event.timestamp, "last_at": event.timestamp}
            )
            bucket["clicks"] += 1
            bucket["last_at"] = event.timestamp
            refs = self._referrers[event.code]
            refs[referrer] = refs.get(referrer, 0) + 1

    def stats(self, code: str, days: int) -> LinkStats:
        cutoff = _day_key(time.time() - days * 86400)
        with self._lock:
            buckets = {
                day: b for (c, day), b in self._daily.items() if c == code and day >= cutoff
            }
            refs = dict(self._referrers.get(code, {}))
        clicks_by_day = {day: int(b["clicks"]) for day, b in sorted(buckets.items())}
        top = dict(sorted(refs.items(), key=lambda kv: kv[1], reverse=True)[:10])
        return LinkStats(
            code=code,
            total_clicks=sum(clicks_by_day.values()),
            unique_days=len(clicks_by_day),
            first_click_at=min((b["first_at"] for b in buckets.values()), default=None),
            last_click_at=max((b["last_at"] for b in buckets.values()), default=None),
            clicks_by_day=clicks_by_day,
            top_referrers=top,
        )

    def health(self) -> bool:
        return True
