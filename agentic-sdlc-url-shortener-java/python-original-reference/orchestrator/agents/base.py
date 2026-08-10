"""Agent contract.

An agent is a unit of SDLC work with a declared interface: what it reads from
shared context, what it writes back, and what it may do. Declaring reads and
writes is not documentation -- the engine uses the recorded reads to decide
which completed nodes a later change has invalidated.

Agents must be **idempotent**: the engine retries them, re-plans them, and can
abandon a timed-out one whose thread is still running. Any agent that is not
safe to run twice will eventually corrupt a run.

An agent must not import the engine. Everything it needs arrives on
:class:`AgentContext`, which keeps agents unit-testable without a scheduler.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..context import RunContext
from ..types import AgentOutcome, AgentResult, Artifact, Finding, Severity


@dataclass
class AgentContext:
    run_id: str
    node_id: str
    context: RunContext
    workspace: str
    artifacts_dir: str
    attempt: int = 1
    dry_run: bool = False

    def read(self, key: str, default: Any = None) -> Any:
        """Read shared context *and* record the dependency for re-planning."""
        return self.context.read(key, self.node_id, default)

    def peek(self, key: str, default: Any = None) -> Any:
        """Read without creating a dependency. Use for logging and diagnostics."""
        return self.context.get(key, default)

    def workspace_path(self, *parts: str) -> str:
        return os.path.join(self.workspace, *parts)


class Agent:
    """Base class. Subclasses implement :meth:`execute`."""

    name: str = "agent"
    description: str = ""
    reads: Sequence[str] = ()
    writes: Sequence[str] = ()

    def execute(self, ctx: AgentContext) -> AgentResult:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def ok(outputs: Optional[Dict[str, Any]] = None, rationale: str = "",
           artifacts: Optional[List[Artifact]] = None,
           findings: Optional[List[Finding]] = None,
           metrics: Optional[Dict[str, Any]] = None,
           proposed_nodes: Optional[List[Any]] = None) -> AgentResult:
        return AgentResult(
            outcome=AgentOutcome.OK, outputs=outputs or {}, rationale=rationale,
            artifacts=artifacts or [], findings=findings or [], metrics=metrics or {},
            proposed_nodes=proposed_nodes or [],
        )

    @staticmethod
    def failed(rationale: str, findings: Optional[List[Finding]] = None,
               metrics: Optional[Dict[str, Any]] = None,
               outputs: Optional[Dict[str, Any]] = None) -> AgentResult:
        return AgentResult(outcome=AgentOutcome.FAILED, rationale=rationale,
                           findings=findings or [], metrics=metrics or {}, outputs=outputs or {})

    @staticmethod
    def needs_input(rationale: str, questions: List[str],
                    outputs: Optional[Dict[str, Any]] = None,
                    artifacts: Optional[List[Artifact]] = None) -> AgentResult:
        return AgentResult(outcome=AgentOutcome.NEEDS_INPUT, rationale=rationale,
                           questions=questions, outputs=outputs or {}, artifacts=artifacts or [])

    @staticmethod
    def skipped(rationale: str, outputs: Optional[Dict[str, Any]] = None) -> AgentResult:
        return AgentResult(outcome=AgentOutcome.SKIPPED, rationale=rationale, outputs=outputs or {})

    @staticmethod
    def document(name: str, path: str, content: str) -> Artifact:
        return Artifact(name=name, kind="document", path=path, content=content,
                        digest=hashlib.sha256(content.encode("utf-8")).hexdigest()[:16])

    @staticmethod
    def finding(severity: Severity, message: str, category: str = "general",
                location: Optional[str] = None, remediation: Optional[str] = None) -> Finding:
        return Finding(severity=severity, message=message, category=category,
                       location=location, remediation=remediation)


class CallableAgent(Agent):
    """Wrap a plain function as an agent. Keeps trivial steps subclass-free."""

    def __init__(self, name: str, fn, description: str = "",
                 reads: Sequence[str] = (), writes: Sequence[str] = ()) -> None:
        self.name = name
        self._fn = fn
        self.description = description
        self.reads = reads
        self.writes = writes

    def execute(self, ctx: AgentContext) -> AgentResult:
        return self._fn(ctx)


class FlakyAgentWrapper(Agent):
    """Deterministically fails the first ``fail_times`` attempts.

    Exists so retry, backoff, and MTTR can be demonstrated and *tested* against
    real engine behaviour instead of being asserted in prose.
    """

    def __init__(self, inner: Agent, fail_times: int = 1,
                 reason: str = "simulated transient failure") -> None:
        self.inner = inner
        self.name = "flaky(%s)" % inner.name
        self.description = inner.description
        self.reads = inner.reads
        self.writes = inner.writes
        self.fail_times = fail_times
        self.reason = reason

    def execute(self, ctx: AgentContext) -> AgentResult:
        if ctx.attempt <= self.fail_times:
            return self.failed("%s (attempt %d of %d)" % (self.reason, ctx.attempt,
                                                          self.fail_times + 1))
        return self.inner.execute(ctx)
