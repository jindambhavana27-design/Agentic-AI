"""Click recording that is decoupled from the redirect hot path.

A redirect must stay fast and must never fail because analytics failed. Events
are handed to a bounded queue drained by a background worker; when the queue is
full events are dropped and counted rather than blocking the request. Losing a
click count is an acceptable failure, adding latency to every redirect is not.

Set ``queue_size=0`` for synchronous recording (used in tests).
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Optional

from .models import ClickEvent
from .observability import METRICS, get_logger
from .storage.base import LinkStore

_log = get_logger("analytics")

_SENTINEL = object()


class AnalyticsRecorder:
    def __init__(self, store: LinkStore, queue_size: int = 4096, enabled: bool = True) -> None:
        self._store = store
        self._enabled = enabled
        self._synchronous = queue_size <= 0
        self._queue: "queue.Queue" = queue.Queue(maxsize=max(queue_size, 1))
        self._worker: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        if self._enabled and not self._synchronous:
            self._start()

    def _start(self) -> None:
        self._worker = threading.Thread(target=self._drain, name="analytics-writer", daemon=True)
        self._worker.start()

    def record(self, event: ClickEvent) -> None:
        if not self._enabled:
            return
        if self._synchronous:
            self._write(event)
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            METRICS.increment("analytics_events_dropped_total")
            _log.warning("analytics queue full; dropping click event", extra={"code": event.code})

    def _drain(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                self._queue.task_done()
                return
            self._write(item)
            self._queue.task_done()

    def _write(self, event: ClickEvent) -> None:
        try:
            self._store.record_click(event)
            METRICS.increment("analytics_events_written_total")
        except Exception as exc:  # analytics must never break the request path
            METRICS.increment("analytics_events_failed_total")
            _log.error("failed to record click: %s" % exc, extra={"code": event.code})

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until queued events are written.

        Returns True if the queue drained within ``timeout``. ``Queue.join`` has
        no timeout, so we poll ``unfinished_tasks`` instead of risking a hang.
        """
        if self._synchronous or not self._enabled:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.002)
        return self._queue.unfinished_tasks == 0

    def close(self) -> None:
        if self._synchronous or not self._enabled or self._worker is None:
            return
        self.flush()
        try:
            self._queue.put_nowait(_SENTINEL)
        except queue.Full:  # pragma: no cover - best effort shutdown
            return
        self._worker.join(timeout=2.0)


def parse_window(raw: Optional[str], default_days: int = 7, max_days: int = 365) -> int:
    """Parse a ``7d`` / ``24h`` style window into whole days."""
    from .errors import ValidationError

    if not raw:
        return default_days
    text = raw.strip().lower()
    try:
        if text.endswith("d"):
            days = int(text[:-1])
        elif text.endswith("h"):
            days = max(1, (int(text[:-1]) + 23) // 24)
        elif text.endswith("w"):
            days = int(text[:-1]) * 7
        else:
            days = int(text)
    except ValueError as exc:
        raise ValidationError("window must look like '7d', '24h' or '2w'", {"window": raw}) from exc
    if days < 1 or days > max_days:
        raise ValidationError("window must be between 1 and %d days" % max_days, {"window": raw})
    return days
