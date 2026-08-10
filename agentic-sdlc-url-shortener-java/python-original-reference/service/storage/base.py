"""Storage port.

The service layer depends only on this interface. Swapping SQLite for Postgres
or Redis is a matter of adding an adapter, not touching domain logic.
"""

from __future__ import annotations

import abc
from typing import List, Optional, Tuple

from ..models import ClickEvent, Link, LinkStats


class LinkStore(abc.ABC):
    @abc.abstractmethod
    def create(self, link: Link) -> Link:
        """Persist a new link.

        Raises :class:`~service.errors.ConflictError` if the code is taken.
        """

    @abc.abstractmethod
    def get(self, code: str) -> Optional[Link]:
        """Return the link for ``code`` including expired/deleted rows.

        Callers distinguish 404 from 410, so the store must not filter here.
        """

    @abc.abstractmethod
    def find_by_idempotency_key(self, key: str, owner: Optional[str]) -> Optional[Link]:
        """Return a prior link created with the same key by the same owner."""

    @abc.abstractmethod
    def soft_delete(self, code: str, at: float) -> bool:
        """Mark a link deleted. Returns False when it did not exist."""

    @abc.abstractmethod
    def list(self, limit: int, cursor: Optional[str]) -> Tuple[List[Link], Optional[str]]:
        """Return a page of links plus the cursor for the next page."""

    @abc.abstractmethod
    def record_click(self, event: ClickEvent) -> None:
        """Record a resolution. Must never raise into the redirect path."""

    @abc.abstractmethod
    def stats(self, code: str, days: int) -> LinkStats:
        """Aggregate click analytics for the trailing ``days`` window."""

    @abc.abstractmethod
    def health(self) -> bool:
        """Cheap liveness probe against the backing store."""

    def close(self) -> None:  # pragma: no cover - default no-op
        return None
