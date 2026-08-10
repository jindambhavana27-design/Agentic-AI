"""Token-bucket rate limiting.

Per-identity buckets, refilled lazily on access so there is no sweeper thread on
the hot path. Idle buckets are reclaimed opportunistically to bound memory --
without that, an attacker cycling source addresses turns the limiter itself into
the memory-exhaustion vector it was meant to prevent.

This is a single-process limiter. A multi-replica deployment needs a shared
counter (Redis) or a consistent-hash routing layer; see docs/RISKS_AND_TRADEOFFS.md.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class TokenBucketRateLimiter:
    def __init__(
        self,
        capacity: int,
        refill_per_sec: float,
        max_buckets: int = 100_000,
        idle_ttl: float = 3600.0,
        clock=time.monotonic,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_per_sec <= 0:
            raise ValueError("refill_per_sec must be positive")
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self.max_buckets = max_buckets
        self.idle_ttl = idle_ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: Dict[str, _Bucket] = {}
        self._last_sweep = clock()

    def allow(self, identity: str, cost: float = 1.0) -> Tuple[bool, float]:
        """Consume ``cost`` tokens for ``identity``.

        Returns ``(allowed, retry_after_seconds)``. ``retry_after`` is 0 when
        allowed.
        """
        now = self._clock()
        with self._lock:
            self._maybe_sweep(now)
            bucket = self._buckets.get(identity)
            if bucket is None:
                bucket = _Bucket(tokens=self.capacity, last_refill=now)
                self._buckets[identity] = bucket
            else:
                elapsed = max(0.0, now - bucket.last_refill)
                bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_sec)
                bucket.last_refill = now

            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True, 0.0

            deficit = cost - bucket.tokens
            return False, deficit / self.refill_per_sec

    def _maybe_sweep(self, now: float) -> None:
        # Sweep at most once a minute, and force one if we are at the cap.
        if now - self._last_sweep < 60.0 and len(self._buckets) < self.max_buckets:
            return
        self._last_sweep = now
        cutoff = now - self.idle_ttl
        stale = [k for k, b in self._buckets.items() if b.last_refill < cutoff]
        for key in stale:
            del self._buckets[key]
        if len(self._buckets) >= self.max_buckets:
            # Still over budget: evict the least recently touched buckets. A
            # full bucket is indistinguishable from a fresh one, so dropping
            # them is safe.
            ordered = sorted(self._buckets.items(), key=lambda kv: kv[1].last_refill)
            for key, _ in ordered[: len(self._buckets) - self.max_buckets + 1]:
                del self._buckets[key]

    def size(self) -> int:
        with self._lock:
            return len(self._buckets)
