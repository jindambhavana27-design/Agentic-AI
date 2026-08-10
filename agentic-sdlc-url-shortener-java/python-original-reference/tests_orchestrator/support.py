"""Helpers for engine tests.

The engine is exercised with trivial in-memory agents rather than the real SDLC
agents. That keeps these tests about *orchestration* -- scheduling, gating,
policy, retry, rollback, re-planning -- instead of about whether a scanner found
a particular finding.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from orchestrator.agents.base import Agent, AgentContext
from orchestrator.graph import Node, WorkflowGraph
from orchestrator.types import (
    AgentOutcome,
    AgentResult,
    Artifact,
    AutonomyLevel,
    Finding,
    Severity,
    Stage,
)


class RecordingAgent(Agent):
    """Records the order and timing of executions across a run."""

    def __init__(self, name: str, outputs: Optional[Dict[str, Any]] = None,
                 delay: float = 0.0, log: Optional[List[str]] = None,
                 findings: Optional[List[Finding]] = None,
                 metrics: Optional[Dict[str, Any]] = None,
                 artifacts: Optional[List[Artifact]] = None) -> None:
        self.name = name
        self._outputs = outputs or {}
        self._delay = delay
        self.log = log if log is not None else []
        self._findings = findings or []
        self._metrics = metrics or {}
        self._artifacts = artifacts or []
        self.calls = 0
        self.windows: List[tuple] = []
        self._lock = threading.Lock()

    def execute(self, ctx: AgentContext) -> AgentResult:
        started = time.time()
        with self._lock:
            self.calls += 1
            self.log.append("%s:start" % self.name)
        if self._delay:
            time.sleep(self._delay)
        with self._lock:
            self.log.append("%s:end" % self.name)
            self.windows.append((started, time.time()))
        return self.ok(outputs=dict(self._outputs), rationale="%s ran" % self.name,
                       findings=list(self._findings), metrics=dict(self._metrics),
                       artifacts=list(self._artifacts))


class FailingAgent(Agent):
    def __init__(self, name: str = "failing", reason: str = "boom",
                 fail_times: Optional[int] = None) -> None:
        self.name = name
        self.reason = reason
        self.fail_times = fail_times
        self.calls = 0

    def execute(self, ctx: AgentContext) -> AgentResult:
        self.calls += 1
        if self.fail_times is not None and self.calls > self.fail_times:
            return self.ok(outputs={"recovered": True}, rationale="recovered")
        return self.failed(self.reason)


class RaisingAgent(Agent):
    name = "raising"

    def execute(self, ctx: AgentContext) -> AgentResult:
        raise RuntimeError("agent exploded")


class NeedsInputAgent(Agent):
    name = "needs-input"

    def __init__(self, questions: Optional[List[str]] = None) -> None:
        self.questions = questions or ["what does 'fast' mean?"]

    def execute(self, ctx: AgentContext) -> AgentResult:
        return self.needs_input("cannot proceed", self.questions,
                                outputs={"partial": True})


class WritingAgent(Agent):
    """Writes a context key whose value can change between runs."""

    def __init__(self, name: str, key: str, values: List[Any]) -> None:
        self.name = name
        self.key = key
        self.values = list(values)
        self.calls = 0

    def execute(self, ctx: AgentContext) -> AgentResult:
        index = min(self.calls, len(self.values) - 1)
        self.calls += 1
        return self.ok(outputs={self.key: self.values[index]},
                       rationale="wrote %s" % self.key)


class ReadingAgent(Agent):
    """Reads a context key, recording the dependency for re-planning."""

    def __init__(self, name: str, key: str) -> None:
        self.name = name
        self.key = key
        self.observed: List[Any] = []

    def execute(self, ctx: AgentContext) -> AgentResult:
        value = ctx.read(self.key)
        self.observed.append(value)
        return self.ok(outputs={"%s_seen" % self.name: value},
                       rationale="read %s" % self.key)


class CallableAgentAdapter(Agent):
    def __init__(self, name: str, fn: Callable[[AgentContext], AgentResult]) -> None:
        self.name = name
        self._fn = fn

    def execute(self, ctx: AgentContext) -> AgentResult:
        return self._fn(ctx)


def node(node_id: str, agent: Agent, **kwargs) -> Node:
    kwargs.setdefault("stage", Stage.IMPLEMENTATION)
    return Node(id=node_id, agent=agent, **kwargs)


def graph(*nodes: Node, name: str = "test") -> WorkflowGraph:
    return WorkflowGraph(name=name, nodes=list(nodes))
