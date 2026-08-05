"""Audit-grade event log.

Events are appended to JSONL with a hash chain: each record carries the digest
of its predecessor, so any edit or deletion in the middle of the file is
detectable by :func:`verify_chain`. That is the difference between a log and an
audit trail -- a log tells you what someone wrote down, an audit trail tells you
whether it was changed afterwards.

The chain is tamper-*evident*, not tamper-proof: anyone who can rewrite the file
can recompute the chain. Making it tamper-proof requires an append-only sink
outside the machine (see docs/RISKS_AND_TRADEOFFS.md).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterator, List, Optional

GENESIS = "0" * 64


@dataclass
class AuditEvent:
    run_id: str
    sequence: int
    event_type: str
    timestamp: float = field(default_factory=time.time)
    node_id: Optional[str] = None
    actor: str = "orchestrator"
    payload: Dict[str, Any] = field(default_factory=dict)
    prev_hash: str = GENESIS
    hash: str = ""

    def compute_hash(self) -> str:
        body = {
            "run_id": self.run_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "timestamp": round(self.timestamp, 6),
            "node_id": self.node_id,
            "actor": self.actor,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
        }
        encoded = json.dumps(body, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AuditLog:
    def __init__(self, path: Optional[str] = None, run_id: str = "") -> None:
        self.path = path
        self.run_id = run_id
        self._lock = threading.Lock()
        self._sequence = 0
        self._last_hash = GENESIS
        self._events: List[AuditEvent] = []
        if path:
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)

    def record(self, event_type: str, node_id: Optional[str] = None,
               actor: str = "orchestrator", **payload) -> AuditEvent:
        with self._lock:
            self._sequence += 1
            event = AuditEvent(
                run_id=self.run_id,
                sequence=self._sequence,
                event_type=event_type,
                node_id=node_id,
                actor=actor,
                payload=payload,
                prev_hash=self._last_hash,
            )
            event.hash = event.compute_hash()
            self._last_hash = event.hash
            self._events.append(event)
            if self.path:
                self._append(event)
            return event

    def _append(self, event: AuditEvent) -> None:
        line = json.dumps(event.to_dict(), default=str, separators=(",", ":"))
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            # fsync so a crash mid-run cannot lose the tail of the audit trail.
            os.fsync(handle.fileno())

    def events(self, event_type: Optional[str] = None,
               node_id: Optional[str] = None) -> List[AuditEvent]:
        with self._lock:
            items = list(self._events)
        if event_type:
            items = [e for e in items if e.event_type == event_type]
        if node_id:
            items = [e for e in items if e.node_id == node_id]
        return items

    def head_hash(self) -> str:
        with self._lock:
            return self._last_hash

    def verify(self) -> "ChainVerification":
        with self._lock:
            return verify_chain(list(self._events))


@dataclass
class ChainVerification:
    valid: bool
    checked: int
    broken_at: Optional[int] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def verify_chain(events: List[AuditEvent]) -> ChainVerification:
    previous = GENESIS
    for index, event in enumerate(events):
        if event.prev_hash != previous:
            return ChainVerification(False, index, event.sequence,
                                     "prev_hash does not match preceding event")
        if event.compute_hash() != event.hash:
            return ChainVerification(False, index, event.sequence,
                                     "recomputed hash does not match recorded hash")
        previous = event.hash
    return ChainVerification(True, len(events))


def read_log(path: str) -> Iterator[AuditEvent]:
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            yield AuditEvent(**json.loads(line))


def verify_file(path: str) -> ChainVerification:
    """Verify an audit file on disk, per run.

    Runs interleave in a shared file, so each run's chain is verified
    independently.
    """
    by_run: Dict[str, List[AuditEvent]] = {}
    for event in read_log(path):
        by_run.setdefault(event.run_id, []).append(event)
    total = 0
    for run_id, events in by_run.items():
        events.sort(key=lambda e: e.sequence)
        outcome = verify_chain(events)
        total += outcome.checked
        if not outcome.valid:
            outcome.reason = "run %s: %s" % (run_id, outcome.reason)
            return outcome
    return ChainVerification(True, total)
