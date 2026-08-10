"""Entry and exit gates.

A gate is a predicate the engine evaluates around a node:

* **Entry gate** -- may this node start? Failing an entry gate is normally not
  an error; it means the work is not applicable and the node is skipped.
* **Exit gate** -- is this node's output acceptable? Failing an exit gate turns
  an "agent said OK" into a real failure, which is the point: the agent is not
  the judge of its own work.

Gates are the mechanism that stops an agent's self-assessment from being the
only quality signal in the system.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .context import RunContext
from .types import AgentResult, Severity, severity_at_least


@dataclass
class GateResult:
    passed: bool
    reason: str
    gate: str = ""
    # On entry-gate failure: skip the node (default) or fail it outright.
    blocking: bool = False
    details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gate": self.gate,
            "passed": self.passed,
            "reason": self.reason,
            "blocking": self.blocking,
            "details": self.details or {},
        }


class Gate:
    """Base gate. Subclasses implement :meth:`check`."""

    name = "gate"
    blocking = False

    def check(self, context: RunContext, node: Any, result: Optional[AgentResult]) -> GateResult:
        raise NotImplementedError

    def __call__(self, context: RunContext, node: Any, result: Optional[AgentResult] = None) -> GateResult:
        outcome = self.check(context, node, result)
        outcome.gate = outcome.gate or self.name
        return outcome

    def _pass(self, reason: str, **details) -> GateResult:
        return GateResult(True, reason, self.name, self.blocking, details or None)

    def _fail(self, reason: str, **details) -> GateResult:
        return GateResult(False, reason, self.name, self.blocking, details or None)


class PredicateGate(Gate):
    """Adapter for ad-hoc lambdas, so simple gates need no subclass."""

    def __init__(self, name: str, predicate: Callable[[RunContext], bool],
                 reason: str = "", blocking: bool = False) -> None:
        self.name = name
        self._predicate = predicate
        self._reason = reason or name
        self.blocking = blocking

    def check(self, context: RunContext, node: Any, result: Optional[AgentResult]) -> GateResult:
        return self._pass("%s satisfied" % self._reason) if self._predicate(context) \
            else self._fail("%s not satisfied" % self._reason)


class ContextKeysPresent(Gate):
    """Entry gate: required upstream context exists and is non-empty."""

    name = "context_keys_present"
    blocking = True

    def __init__(self, *keys: str) -> None:
        self.keys = keys

    def check(self, context: RunContext, node: Any, result: Optional[AgentResult]) -> GateResult:
        missing = [k for k in self.keys if not context.has(k) or context.get(k) in (None, [], {}, "")]
        if missing:
            return self._fail("missing required context", missing=missing)
        return self._pass("all required context present", keys=list(self.keys))


class NoOpenQuestions(Gate):
    """Entry gate: refuse to build while requirements are still ambiguous.

    This is what makes the ambiguous scenario stop instead of guessing.
    """

    name = "no_open_questions"
    blocking = True

    def __init__(self, key: str = "open_questions", blocking_only: bool = True) -> None:
        self.key = key
        # Advisory questions (unstated NFR dimensions, optional scope) are
        # recorded for the reader but must not stall the build; only questions
        # marked blocking represent input the work genuinely cannot proceed on.
        self.blocking_only = blocking_only

    def check(self, context: RunContext, node: Any, result: Optional[AgentResult]) -> GateResult:
        open_questions = context.get(self.key) or []
        unresolved = [
            q for q in open_questions
            if not q.get("resolved") and (q.get("blocking") or not self.blocking_only)
        ]
        if unresolved:
            return self._fail(
                "%d unresolved %srequirement question(s)"
                % (len(unresolved), "blocking " if self.blocking_only else ""),
                questions=[q.get("question") for q in unresolved],
            )
        return self._pass("no unresolved blocking requirement questions")


class AgentSucceeded(Gate):
    """Exit gate: the agent itself reported success."""

    name = "agent_succeeded"

    def check(self, context: RunContext, node: Any, result: Optional[AgentResult]) -> GateResult:
        if result is None:
            return self._fail("no agent result")
        if result.ok:
            return self._pass("agent reported %s" % result.outcome.value)
        return self._fail("agent reported %s" % result.outcome.value)


class ProducedOutputs(Gate):
    """Exit gate: the agent actually produced the outputs it is contracted to."""

    name = "produced_outputs"

    def __init__(self, *keys: str) -> None:
        self.keys = keys

    def check(self, context: RunContext, node: Any, result: Optional[AgentResult]) -> GateResult:
        if result is None:
            return self._fail("no agent result")
        missing = [k for k in self.keys if k not in result.outputs or result.outputs[k] in (None, "")]
        if missing:
            return self._fail("agent did not produce required outputs", missing=missing)
        return self._pass("produced %s" % ", ".join(self.keys))


class NoFindingsAtOrAbove(Gate):
    """Exit gate: quality/security bar expressed as a severity ceiling."""

    name = "severity_ceiling"

    def __init__(self, threshold: Severity = Severity.HIGH) -> None:
        self.threshold = threshold
        self.name = "no_findings_at_or_above_%s" % threshold.value

    def check(self, context: RunContext, node: Any, result: Optional[AgentResult]) -> GateResult:
        if result is None:
            return self._fail("no agent result")
        breaches = [f for f in result.findings if severity_at_least(f.severity, self.threshold)]
        if breaches:
            return self._fail(
                "%d finding(s) at or above %s" % (len(breaches), self.threshold.value),
                findings=[f.message for f in breaches[:5]],
            )
        return self._pass("no findings at or above %s" % self.threshold.value)


class MetricThreshold(Gate):
    """Exit gate: a numeric result metric clears a floor (or stays under a cap)."""

    def __init__(self, metric: str, minimum: Optional[float] = None,
                 maximum: Optional[float] = None) -> None:
        if minimum is None and maximum is None:
            raise ValueError("MetricThreshold needs a minimum or a maximum")
        self.metric = metric
        self.minimum = minimum
        self.maximum = maximum
        self.name = "metric_threshold:%s" % metric

    def check(self, context: RunContext, node: Any, result: Optional[AgentResult]) -> GateResult:
        if result is None or self.metric not in result.metrics:
            return self._fail("metric %r was not reported" % self.metric)
        value = result.metrics[self.metric]
        if self.minimum is not None and value < self.minimum:
            return self._fail(
                "%s = %s is below the floor of %s" % (self.metric, value, self.minimum),
                value=value, minimum=self.minimum,
            )
        if self.maximum is not None and value > self.maximum:
            return self._fail(
                "%s = %s exceeds the cap of %s" % (self.metric, value, self.maximum),
                value=value, maximum=self.maximum,
            )
        return self._pass("%s = %s within bounds" % (self.metric, value), value=value)


class AllUpstreamSucceeded(Gate):
    """Entry gate for release-style barriers: every ancestor genuinely passed.

    Dependency satisfaction alone permits SKIPPED parents. A release gate must
    not treat "we skipped the security scan" as "security passed".
    """

    name = "all_upstream_succeeded"
    blocking = True

    def __init__(self, statuses_key: str = "__node_statuses__") -> None:
        self.statuses_key = statuses_key

    def check(self, context: RunContext, node: Any, result: Optional[AgentResult]) -> GateResult:
        statuses: Dict[str, str] = context.get(self.statuses_key) or {}
        not_succeeded = [
            dep for dep in getattr(node, "depends_on", [])
            if statuses.get(dep) not in (None, "succeeded")
        ]
        if not_succeeded:
            return self._fail("upstream node(s) did not succeed", nodes=not_succeeded)
        return self._pass("all upstream nodes succeeded")


def evaluate_gates(gates: List[Gate], context: RunContext, node: Any,
                   result: Optional[AgentResult] = None) -> List[GateResult]:
    """Evaluate every gate. All gates run even after the first failure so the
    operator sees the complete picture in one pass rather than one per re-run."""
    return [gate(context, node, result) for gate in gates]


def gates_passed(results: List[GateResult]) -> bool:
    return all(r.passed for r in results)


def blocking_failure(results: List[GateResult]) -> bool:
    return any((not r.passed) and r.blocking for r in results)
