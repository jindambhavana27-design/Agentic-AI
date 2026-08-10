"""Event-sourced run state.

The in-memory state is a *projection* of an append-only event journal, never the
source of truth. Everything else follows from that:

* A run that dies mid-flight is rebuilt by replaying its journal
  (:meth:`RunState.from_journal`), so an interrupted approval wait resumes
  instead of restarting.
* Metrics are derived from the same events the auditor reads, so the reliability
  numbers and the audit trail can never disagree.
* "What happened?" is answerable exactly, including attempts that were later
  superseded by a retry.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .types import NodeStatus, RunStatus


def new_run_id(prefix: str = "run") -> str:
    return "%s-%s-%s" % (prefix, time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])


@dataclass
class Attempt:
    number: int
    started_at: float
    finished_at: Optional[float] = None
    outcome: Optional[str] = None
    error: Optional[str] = None

    @property
    def duration(self) -> float:
        return (self.finished_at or time.time()) - self.started_at


@dataclass
class NodeState:
    node_id: str
    status: NodeStatus = NodeStatus.PENDING
    attempts: List[Attempt] = field(default_factory=list)
    first_started_at: Optional[float] = None
    finished_at: Optional[float] = None
    # Wall-clock time spent parked on a human decision. Excluded from execution
    # latency, otherwise "how fast is the system" measures how fast people are.
    approval_wait_seconds: float = 0.0
    result: Optional[Dict[str, Any]] = None
    entry_gates: List[Dict[str, Any]] = field(default_factory=list)
    exit_gates: List[Dict[str, Any]] = field(default_factory=list)
    policy: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    replan_count: int = 0
    rolled_back: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def retry_count(self) -> int:
        """Attempts caused by *failure*, excluding re-executions after a re-plan.

        Each invalidation legitimately buys one fresh attempt. Counting those as
        retries would make a healthy re-planning run look unreliable, which is
        the opposite of what the metric is for.
        """
        return max(0, len(self.attempts) - 1 - self.replan_count)

    @property
    def execution_seconds(self) -> float:
        if self.first_started_at is None:
            return 0.0
        end = self.finished_at or time.time()
        return max(0.0, end - self.first_started_at - self.approval_wait_seconds)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["execution_seconds"] = round(self.execution_seconds, 4)
        return data


class Journal:
    """Append-only event sink with an optional file backing."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._events: List[Dict[str, Any]] = []
        if path:
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)

    def append(self, event: Dict[str, Any]) -> Dict[str, Any]:
        event.setdefault("ts", time.time())
        with self._lock:
            event["seq"] = len(self._events) + 1
            self._events.append(event)
            if self.path:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, default=str, separators=(",", ":")) + "\n")
        return event

    def events(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._events)

    @classmethod
    def load(cls, path: str) -> "Journal":
        journal = cls(path)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                journal._events = [json.loads(l) for l in handle if l.strip()]
        return journal


class RunState:
    def __init__(self, run_id: str, workflow: str, journal: Optional[Journal] = None,
                 inputs: Optional[Dict[str, Any]] = None) -> None:
        self.run_id = run_id
        self.workflow = workflow
        self.journal = journal or Journal()
        self.status = RunStatus.PENDING
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.inputs = inputs or {}
        self.nodes: Dict[str, NodeState] = {}
        self.failure_reason: Optional[str] = None
        self.replan_events: List[Dict[str, Any]] = []
        self.approvals: List[Dict[str, Any]] = []
        self._lock = threading.RLock()

    # -- event emission -------------------------------------------------------

    def emit(self, event_type: str, node_id: Optional[str] = None, **payload) -> Dict[str, Any]:
        event = {"type": event_type, "run_id": self.run_id, "node_id": node_id, "payload": payload}
        self.journal.append(event)
        self._apply(event)
        return event

    def _apply(self, event: Dict[str, Any]) -> None:
        """Fold an event into the projection. Pure with respect to the journal."""
        etype = event["type"]
        node_id = event.get("node_id")
        payload = event.get("payload", {})
        ts = event.get("ts", time.time())

        with self._lock:
            if etype == "run_started":
                self.status = RunStatus.RUNNING
                self.started_at = ts
                self.inputs = payload.get("inputs", self.inputs)
                return
            if etype == "run_finished":
                self.status = RunStatus(payload["status"])
                self.finished_at = ts
                self.failure_reason = payload.get("reason")
                return
            if etype == "node_registered":
                self.nodes.setdefault(node_id, NodeState(node_id=node_id))
                return

            node = self.nodes.setdefault(node_id, NodeState(node_id=node_id)) if node_id else None

            if etype == "node_status":
                node.status = NodeStatus(payload["status"])
                if payload.get("note"):
                    node.notes.append(payload["note"])
            elif etype == "attempt_started":
                node.status = NodeStatus.RUNNING
                node.attempts.append(Attempt(number=payload["attempt"], started_at=ts))
                if node.first_started_at is None:
                    node.first_started_at = ts
            elif etype == "attempt_finished":
                if node.attempts:
                    attempt = node.attempts[-1]
                    attempt.finished_at = ts
                    attempt.outcome = payload.get("outcome")
                    attempt.error = payload.get("error")
            elif etype == "node_succeeded":
                node.status = NodeStatus.SUCCEEDED
                node.finished_at = ts
                node.result = payload.get("result")
                node.error = None
            elif etype == "node_failed":
                node.status = NodeStatus.FAILED
                node.finished_at = ts
                node.error = payload.get("error")
                node.result = payload.get("result", node.result)
            elif etype == "node_skipped":
                node.status = NodeStatus.SKIPPED
                node.finished_at = ts
                node.notes.append(payload.get("reason", "skipped"))
            elif etype == "node_halted":
                node.status = NodeStatus.HALTED
                node.finished_at = ts
                node.error = payload.get("reason")
            elif etype == "gates_evaluated":
                target = node.entry_gates if payload.get("phase") == "entry" else node.exit_gates
                target.extend(payload.get("results", []))
            elif etype == "policy_evaluated":
                node.policy.append(payload.get("verdict", {}))
            elif etype == "approval_requested":
                node.status = NodeStatus.AWAITING_APPROVAL
                self.approvals.append({"node_id": node_id, "requested_at": ts, **payload})
            elif etype == "approval_resolved":
                node.approval_wait_seconds += float(payload.get("waited_seconds", 0.0))
                for record in self.approvals:
                    if record["node_id"] == node_id and "decision" not in record:
                        record["decision"] = payload.get("decision")
                        record["decided_by"] = payload.get("decided_by")
                        record["resolved_at"] = ts
                        break
            elif etype == "node_invalidated":
                node.status = NodeStatus.INVALIDATED
                node.replan_count += 1
                node.finished_at = None
                self.replan_events.append({
                    "node_id": node_id, "ts": ts,
                    "trigger": payload.get("trigger"), "drift": payload.get("drift"),
                })
            elif etype == "node_reset":
                node.status = NodeStatus.PENDING
                node.finished_at = None
                node.error = None
            elif etype == "node_compensated":
                node.status = NodeStatus.COMPENSATED
                node.rolled_back = True
            elif etype == "nodes_injected":
                for injected in payload.get("nodes", []):
                    self.nodes.setdefault(injected, NodeState(node_id=injected))

    # -- queries --------------------------------------------------------------

    def node(self, node_id: str) -> NodeState:
        with self._lock:
            return self.nodes.setdefault(node_id, NodeState(node_id=node_id))

    def status_map(self) -> Dict[str, str]:
        with self._lock:
            return {nid: n.status.value for nid, n in self.nodes.items()}

    def pending_approvals(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [a for a in self.approvals if "decision" not in a]

    @property
    def duration_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "run_id": self.run_id,
                "workflow": self.workflow,
                "status": self.status.value,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "duration_seconds": round(self.duration_seconds, 4),
                "failure_reason": self.failure_reason,
                "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
                "approvals": list(self.approvals),
                "replans": list(self.replan_events),
            }

    # -- reconstruction -------------------------------------------------------

    @classmethod
    def from_events(cls, events: Iterable[Dict[str, Any]], run_id: str,
                    workflow: str = "") -> "RunState":
        state = cls(run_id, workflow)
        for event in events:
            if event.get("run_id") not in (None, run_id):
                continue
            state._apply(event)
        return state

    @classmethod
    def from_journal(cls, path: str, run_id: str, workflow: str = "") -> "RunState":
        journal = Journal.load(path)
        state = cls.from_events(journal.events(), run_id, workflow)
        state.journal = journal
        return state
