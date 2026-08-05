"""Cross-stage context with decision lineage.

Agents never talk to each other directly. They read and write a versioned
blackboard, and every read is recorded against the version it observed. That
single mechanism buys three things the orchestrator needs:

1. **Traceability** -- for any decision you can walk back to the inputs and the
   agent that produced them.
2. **Re-planning** -- if a key is rewritten with different content, every node
   that read the old version is provably stale and can be invalidated.
3. **Auditability** -- the lineage log is an append-only record of who changed
   what, when, and why.

Rewriting a key with an *identical* value does not bump the version. Without
that check, an idempotent re-run of an upstream node would cascade a pointless
re-run through the entire downstream graph.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


def fingerprint(value: Any) -> str:
    """Stable content hash for arbitrary JSON-serialisable values."""
    try:
        encoded = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        encoded = repr(value)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


@dataclass
class LineageEntry:
    key: str
    version: int
    node_id: str
    digest: str
    rationale: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "version": self.version,
            "node_id": self.node_id,
            "digest": self.digest,
            "rationale": self.rationale,
            "timestamp": self.timestamp,
        }


@dataclass
class ContextEntry:
    key: str
    value: Any
    version: int
    digest: str
    written_by: str
    updated_at: float


class RunContext:
    """Thread-safe versioned blackboard shared by every node in a run."""

    def __init__(self, initial: Optional[Dict[str, Any]] = None) -> None:
        self._lock = threading.RLock()
        self._entries: Dict[str, ContextEntry] = {}
        self._lineage: List[LineageEntry] = []
        # node_id -> {key: version_observed}
        self._reads: Dict[str, Dict[str, int]] = {}
        for key, value in (initial or {}).items():
            self.put(key, value, node_id="__seed__", rationale="initial run input")

    # -- writes ---------------------------------------------------------------

    def put(self, key: str, value: Any, node_id: str, rationale: str = "") -> int:
        """Write ``key``; returns the resulting version.

        The version only advances when the content actually changes.
        """
        digest = fingerprint(value)
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None and existing.digest == digest:
                existing.updated_at = time.time()
                return existing.version
            version = (existing.version + 1) if existing else 1
            self._entries[key] = ContextEntry(
                key=key, value=value, version=version, digest=digest,
                written_by=node_id, updated_at=time.time(),
            )
            self._lineage.append(
                LineageEntry(key=key, version=version, node_id=node_id,
                             digest=digest, rationale=rationale)
            )
            return version

    def put_all(self, values: Dict[str, Any], node_id: str, rationale: str = "") -> Dict[str, int]:
        return {k: self.put(k, v, node_id, rationale) for k, v in values.items()}

    # -- reads ----------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Read without recording a dependency (reporting, gates, logging)."""
        with self._lock:
            entry = self._entries.get(key)
            return entry.value if entry else default

    def read(self, key: str, node_id: str, default: Any = None) -> Any:
        """Read *and* record that ``node_id`` depends on the observed version."""
        with self._lock:
            entry = self._entries.get(key)
            observed = entry.version if entry else 0
            self._reads.setdefault(node_id, {})[key] = observed
            return entry.value if entry else default

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._entries

    def version(self, key: str) -> int:
        with self._lock:
            entry = self._entries.get(key)
            return entry.version if entry else 0

    def keys(self) -> Set[str]:
        with self._lock:
            return set(self._entries)

    # -- staleness ------------------------------------------------------------

    def observed_versions(self, node_id: str) -> Dict[str, int]:
        with self._lock:
            return dict(self._reads.get(node_id, {}))

    def stale_keys(self, node_id: str) -> Dict[str, Any]:
        """Keys whose current version differs from what ``node_id`` last read."""
        with self._lock:
            observed = self._reads.get(node_id, {})
            drift = {}
            for key, seen in observed.items():
                entry = self._entries.get(key)
                current = entry.version if entry else 0
                if current != seen:
                    drift[key] = {"observed": seen, "current": current}
            return drift

    def clear_reads(self, node_id: str) -> None:
        """Drop recorded reads so a re-run starts from a clean dependency set."""
        with self._lock:
            self._reads.pop(node_id, None)

    # -- introspection --------------------------------------------------------

    def lineage(self, key: Optional[str] = None) -> List[LineageEntry]:
        with self._lock:
            if key is None:
                return list(self._lineage)
            return [e for e in self._lineage if e.key == key]

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {k: e.value for k, e in self._entries.items()}

    def digest_map(self) -> Dict[str, str]:
        with self._lock:
            return {k: e.digest for k, e in self._entries.items()}

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "entries": {
                    k: {"version": e.version, "digest": e.digest, "written_by": e.written_by}
                    for k, e in self._entries.items()
                },
                "lineage": [e.to_dict() for e in self._lineage],
                "reads": {n: dict(v) for n, v in self._reads.items()},
            }
