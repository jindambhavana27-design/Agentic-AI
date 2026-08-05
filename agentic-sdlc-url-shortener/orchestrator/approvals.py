"""Human approval checkpoints.

Three brokers, one interface, chosen by how the run is being operated:

* :class:`AutoApprovalBroker` -- policy-bounded auto-decisions for CI and demos.
  It is *not* a rubber stamp: it refuses anything above a configured risk level,
  so "the demo runs unattended" never quietly means "nothing is gated".
* :class:`InteractiveApprovalBroker` -- prompts a human on the terminal.
* :class:`DeferredApprovalBroker` -- writes the request to disk and stops the
  run. A reviewer decides out of band and the run resumes. This is the mode that
  matches real change control, where approvers are not sitting at the console.

The engine never decides for itself whether something needs a human; it asks the
policy engine and the node declaration, then delegates to a broker.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .types import Severity, severity_at_least


@dataclass
class ApprovalRequest:
    run_id: str
    node_id: str
    stage: str
    summary: str
    risk: Severity = Severity.MEDIUM
    reasons: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)
    requested_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["risk"] = self.risk.value
        return data


@dataclass
class ApprovalDecision:
    approved: bool
    decided_by: str
    reason: str = ""
    pending: bool = False       # run must stop and wait for an out-of-band decision
    decided_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ApprovalBroker:
    name = "broker"

    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        raise NotImplementedError


class AutoApprovalBroker(ApprovalBroker):
    """Auto-approve up to a risk ceiling; escalate anything above it."""

    name = "auto"

    def __init__(self, max_auto_risk: Severity = Severity.MEDIUM,
                 approver: str = "ci-automation") -> None:
        self.max_auto_risk = max_auto_risk
        self.approver = approver
        self.log: List[Dict[str, Any]] = []

    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        # `risk > ceiling` -> refuse. Auto-approval must never be able to wave
        # through the class of change it exists to protect.
        escalate = severity_at_least(request.risk, self.max_auto_risk) and \
            request.risk is not self.max_auto_risk
        decision = ApprovalDecision(
            approved=not escalate,
            decided_by=self.approver,
            reason=(
                "risk %s exceeds the auto-approval ceiling of %s; human sign-off required"
                % (request.risk.value, self.max_auto_risk.value)
                if escalate else
                "auto-approved: risk %s is within the %s ceiling"
                % (request.risk.value, self.max_auto_risk.value)
            ),
        )
        self.log.append({"request": request.to_dict(), "decision": decision.to_dict()})
        return decision


class StaticApprovalBroker(ApprovalBroker):
    """Pre-seeded decisions keyed by node id. Used by tests."""

    name = "static"

    def __init__(self, decisions: Dict[str, bool], default: Optional[bool] = None) -> None:
        self.decisions = decisions
        self.default = default
        self.seen: List[ApprovalRequest] = []

    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        self.seen.append(request)
        verdict = self.decisions.get(request.node_id, self.default)
        if verdict is None:
            return ApprovalDecision(False, "static", "no decision configured", pending=True)
        return ApprovalDecision(bool(verdict), "static", "preconfigured decision")


class InteractiveApprovalBroker(ApprovalBroker):
    """Prompt on the terminal. Falls back to deny when stdin is not a TTY."""

    name = "interactive"

    def __init__(self, stream=None, approver: str = "operator") -> None:
        self.stream = stream or sys.stdin
        self.approver = approver
        self._lock = threading.Lock()

    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        with self._lock:
            print("\n" + "=" * 72, file=sys.stderr)
            print("APPROVAL REQUIRED  run=%s  node=%s  [%s]"
                  % (request.run_id, request.node_id, request.stage), file=sys.stderr)
            print("risk    : %s" % request.risk.value.upper(), file=sys.stderr)
            print("summary : %s" % request.summary, file=sys.stderr)
            for reason in request.reasons:
                print("  - %s" % reason, file=sys.stderr)
            print("=" * 72, file=sys.stderr)
            if not getattr(self.stream, "isatty", lambda: False)():
                # Denying on a non-interactive stream is the safe default; the
                # alternative is an unattended pipeline approving its own changes.
                return ApprovalDecision(False, "system",
                                        "no interactive terminal available; denied by default")
            sys.stderr.write("approve? [y/N]: ")
            sys.stderr.flush()
            answer = (self.stream.readline() or "").strip().lower()
            approved = answer in ("y", "yes")
            return ApprovalDecision(approved, self.approver,
                                    "operator responded %r" % (answer or "<empty>"))


class DeferredApprovalBroker(ApprovalBroker):
    """Persist the request and pause the run until a decision is recorded.

    Decisions are recorded by ``orchestrator approve`` / ``reject``, or directly
    via :meth:`decide`.
    """

    name = "deferred"

    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, Any]:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "r", encoding="utf-8") as handle:
            try:
                return json.load(handle)
            except json.JSONDecodeError:  # pragma: no cover - corrupted store
                return {}

    def _save(self, data: Dict[str, Any]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, default=str)
        os.replace(tmp, self.path)  # atomic; a crash never leaves a partial store

    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        key = "%s::%s" % (request.run_id, request.node_id)
        with self._lock:
            data = self._load()
            record = data.get(key)
            if record and record.get("decision") is not None:
                return ApprovalDecision(
                    approved=bool(record["decision"]),
                    decided_by=record.get("decided_by", "unknown"),
                    reason=record.get("reason", "recorded out of band"),
                )
            data[key] = {
                "request": request.to_dict(),
                "decision": None,
                "decided_by": None,
                "reason": None,
            }
            self._save(data)
        return ApprovalDecision(False, "pending", "awaiting out-of-band approval", pending=True)

    def decide(self, run_id: str, node_id: str, approved: bool,
               decided_by: str = "operator", reason: str = "") -> None:
        key = "%s::%s" % (run_id, node_id)
        with self._lock:
            data = self._load()
            if key not in data:
                raise KeyError("no pending approval for %s" % key)
            data[key].update({
                "decision": bool(approved),
                "decided_by": decided_by,
                "reason": reason or ("approved" if approved else "rejected"),
                "decided_at": time.time(),
            })
            self._save(data)

    def pending(self) -> List[Dict[str, Any]]:
        with self._lock:
            data = self._load()
        return [
            {"key": key, **record}
            for key, record in data.items()
            if record.get("decision") is None
        ]


def risk_for(node: Any, reasons: List[str], severity: Severity = Severity.MEDIUM) -> Severity:
    """Derive a risk level for an approval request.

    Release stages and denials are inherently higher risk than a routine
    change-budget overrun; the broker's ceiling then decides what can proceed
    unattended.
    """
    from .types import Stage

    if getattr(node, "stage", None) is Stage.RELEASE:
        return Severity.HIGH
    joined = " ".join(reasons).lower()
    if any(word in joined for word in ("destructive", "critical", "credential", "secret")):
        return Severity.CRITICAL
    if any(word in joined for word in ("dependency", "security", "high-severity")):
        return Severity.HIGH
    return severity
