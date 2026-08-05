"""Shared vocabulary for the orchestration layer.

Every state an execution can occupy is named here rather than represented by
booleans scattered across the engine. The state machine is the contract between
the scheduler, the journal, the metrics reducer, and the CLI, so it lives in one
file and changes deliberately.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class Stage(str, enum.Enum):
    """SDLC phases. Used for reporting and for stage-scoped policy."""

    REQUIREMENTS = "requirements"
    ARCHITECTURE = "architecture"
    PLANNING = "planning"
    ANALYSIS = "analysis"
    IMPLEMENTATION = "implementation"
    TESTING = "testing"
    SECURITY = "security"
    DOCUMENTATION = "documentation"
    RELEASE = "release"
    GOVERNANCE = "governance"


class NodeStatus(str, enum.Enum):
    PENDING = "pending"                    # not yet eligible
    READY = "ready"                        # dependencies satisfied
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"                    # entry gate declined, not an error
    INVALIDATED = "invalidated"            # upstream changed; must re-run
    COMPENSATED = "compensated"            # rolled back
    HALTED = "halted"                      # safe-stop tripped


TERMINAL_STATUSES = frozenset(
    {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.SKIPPED,
     NodeStatus.COMPENSATED, NodeStatus.HALTED}
)
# A node in one of these has produced usable output for its dependents.
SATISFIED_STATUSES = frozenset({NodeStatus.SUCCEEDED, NodeStatus.SKIPPED})


class RunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    HALTED = "halted"
    ROLLED_BACK = "rolled_back"


class AutonomyLevel(int, enum.Enum):
    """How much an agent may do without a human in the loop.

    The level is declared per node, so a single workflow can mix a fully
    autonomous linter with a propose-only production release.
    """

    PROPOSE_ONLY = 0      # produce a plan; never mutate anything
    ACT_WITH_APPROVAL = 1  # mutate only after an explicit human decision
    ACT_AND_REPORT = 2     # mutate freely, surface everything afterwards
    AUTONOMOUS = 3         # mutate freely within policy, no reporting gate


class Severity(str, enum.Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


_SEVERITY_ORDER = {s: i for i, s in enumerate(
    [Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
)}


def severity_at_least(value: Severity, threshold: Severity) -> bool:
    return _SEVERITY_ORDER[value] >= _SEVERITY_ORDER[threshold]


class FailureAction(str, enum.Enum):
    """What the engine does when a node exhausts its retries."""

    FAIL_RUN = "fail_run"        # stop scheduling, mark run failed
    ROLLBACK = "rollback"        # run compensations, mark run rolled_back
    CONTINUE = "continue"        # record and keep going (non-blocking work)
    FALLBACK = "fallback"        # execute the node's declared fallback agent
    SAFE_STOP = "safe_stop"      # freeze the run intact for human triage


class PolicyEffect(str, enum.Enum):
    ALLOW = "allow"
    WARN = "warn"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class AgentOutcome(str, enum.Enum):
    OK = "ok"
    FAILED = "failed"
    NEEDS_INPUT = "needs_input"   # agent cannot proceed without a human answer
    SKIPPED = "skipped"           # agent determined there is nothing to do


@dataclass
class Finding:
    """A reviewable observation produced by an agent."""

    severity: Severity
    message: str
    category: str = "general"
    location: Optional[str] = None
    remediation: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity.value,
            "category": self.category,
            "message": self.message,
            "location": self.location,
            "remediation": self.remediation,
        }


@dataclass
class Artifact:
    """A concrete engineering output an agent produced."""

    name: str
    kind: str            # "document" | "code" | "test-report" | "schema" | "plan"
    path: Optional[str] = None
    content: Optional[str] = None
    digest: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "path": self.path, "digest": self.digest}


@dataclass
class AgentResult:
    outcome: AgentOutcome = AgentOutcome.OK
    outputs: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[Artifact] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    rationale: str = ""
    questions: List[str] = field(default_factory=list)
    proposed_nodes: List[Any] = field(default_factory=list)  # dynamic graph expansion
    # existing node id -> extra dependencies, so injected work becomes a real
    # barrier for a downstream node instead of a detached branch.
    rewire: Dict[str, List[str]] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome in (AgentOutcome.OK, AgentOutcome.SKIPPED)

    def max_severity(self) -> Severity:
        if not self.findings:
            return Severity.INFO
        return max((f.severity for f in self.findings), key=lambda s: _SEVERITY_ORDER[s])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "outputs": self.outputs,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "findings": [f.to_dict() for f in self.findings],
            "rationale": self.rationale,
            "questions": self.questions,
            "metrics": self.metrics,
        }


@dataclass
class RetryPolicy:
    max_attempts: int = 1
    backoff_seconds: float = 0.0
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 30.0
    # Only retry when the agent signalled a transient condition. Retrying a
    # deterministic failure just burns time and produces identical output.
    retry_on: tuple = (AgentOutcome.FAILED,)

    def delay_for(self, attempt: int) -> float:
        if self.backoff_seconds <= 0:
            return 0.0
        delay = self.backoff_seconds * (self.backoff_multiplier ** max(0, attempt - 1))
        return min(delay, self.max_backoff_seconds)


class OrchestratorError(Exception):
    pass


class GraphValidationError(OrchestratorError):
    pass


class PolicyViolation(OrchestratorError):
    def __init__(self, message: str, policy_id: str) -> None:
        super().__init__(message)
        self.policy_id = policy_id


class ApprovalRejected(OrchestratorError):
    pass


class SafeStop(OrchestratorError):
    """Raised to freeze a run for human triage without rolling anything back."""
