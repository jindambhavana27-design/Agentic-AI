"""The orchestration engine.

Execution model
---------------
The engine is a **readiness scheduler over a mutable DAG**, not a task chain.
Each iteration it asks which nodes have satisfied dependencies, admits them
through entry gates and policy, dispatches them concurrently, and then reacts to
whatever comes back. Reacting is where the interesting behaviour lives:

* a node's output is validated by **exit gates** it does not control;
* **policy** can deny it or escalate it to a human after the fact;
* success **rewrites shared context**, which can invalidate already-completed
  downstream nodes and put them back on the queue (**re-planning**);
* a planning node can **inject new nodes** into the running graph;
* failure walks a declared path -- retry, fallback, rollback, or safe-stop.

Concurrency is deliberately asymmetric: agent bodies run on a thread pool, but
every state transition, gate evaluation, policy check, and approval happens on
the scheduler thread. Governance decisions are therefore totally ordered and the
journal is a faithful serial history, with none of the interleaving bugs that a
fully concurrent state machine invites.
"""

from __future__ import annotations

import os
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .agents.base import AgentContext
from .approvals import (
    ApprovalBroker,
    ApprovalRequest,
    AutoApprovalBroker,
    risk_for,
)
from .audit import AuditLog
from .context import RunContext
from .gates import GateResult, blocking_failure, evaluate_gates, gates_passed
from .graph import Node, WorkflowGraph
from .metrics import RunMetrics, compute_metrics
from .policy import PolicyEngine, PolicyVerdict
from .replan import Replanner
from .rollback import CompensationRegistry, RollbackReport, WorkspaceSnapshot
from .state import Journal, RunState, new_run_id
from .types import (
    AgentOutcome,
    AgentResult,
    Artifact,
    AutonomyLevel,
    FailureAction,
    Finding,
    NodeStatus,
    PolicyEffect,
    RunStatus,
    SATISFIED_STATUSES,
    Severity,
    TERMINAL_STATUSES,
)

NODE_STATUS_KEY = "__node_statuses__"


@dataclass
class EngineConfig:
    max_parallelism: int = 4
    safe_stop_after_consecutive_failures: int = 3
    max_replans_per_node: int = 2
    # Ceiling on scheduler iterations. Purely a liveness backstop -- if a bug in
    # a gate or replanner produced a cycle, the run should stop, not spin.
    max_iterations: int = 500
    poll_interval: float = 0.05
    workspace: Optional[str] = None
    artifacts_dir: str = "artifacts"
    journal_path: Optional[str] = None
    audit_path: Optional[str] = None
    snapshot_workspace: bool = False
    dry_run: bool = False


@dataclass
class RunReport:
    run_id: str
    workflow: str
    status: RunStatus
    state: RunState
    metrics: RunMetrics
    context: RunContext
    graph: WorkflowGraph
    audit: AuditLog
    rollbacks: List[RollbackReport] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    artifacts: List[Artifact] = field(default_factory=list)
    pending_approvals: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status is RunStatus.SUCCEEDED

    @property
    def resumable(self) -> bool:
        return self.status is RunStatus.AWAITING_APPROVAL

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "status": self.status.value,
            "metrics": self.metrics.to_dict(),
            "state": self.state.to_dict(),
            "context": self.context.to_dict(),
            "graph": self.graph.to_dict(),
            "rollbacks": [r.to_dict() for r in self.rollbacks],
            "findings": [f.to_dict() for f in self.findings],
            "artifacts": [a.to_dict() for a in self.artifacts],
            "pending_approvals": self.pending_approvals,
            "audit_head": self.audit.head_hash(),
            "audit_verified": self.audit.verify().to_dict(),
        }


class _Scheduled:
    """Bookkeeping for one in-flight node execution."""

    __slots__ = ("node_id", "attempt", "future", "deadline", "started_at")

    def __init__(self, node_id: str, attempt: int, future: Future,
                 deadline: Optional[float], started_at: float) -> None:
        self.node_id = node_id
        self.attempt = attempt
        self.future = future
        self.deadline = deadline
        self.started_at = started_at


class Orchestrator:
    def __init__(
        self,
        graph: WorkflowGraph,
        config: Optional[EngineConfig] = None,
        policy_engine: Optional[PolicyEngine] = None,
        approval_broker: Optional[ApprovalBroker] = None,
        replanner: Optional[Replanner] = None,
    ) -> None:
        self.graph = graph
        self.config = config or EngineConfig()
        self.policy = policy_engine or PolicyEngine()
        self.approvals = approval_broker or AutoApprovalBroker()
        self.replanner = replanner or Replanner(self.config.max_replans_per_node)

        self.state: Optional[RunState] = None
        self.context: Optional[RunContext] = None
        self.audit: Optional[AuditLog] = None
        self.compensations = CompensationRegistry()
        self.rollbacks: List[RollbackReport] = []
        self._snapshot: Optional[WorkspaceSnapshot] = None
        self._retry_at: Dict[str, float] = {}
        self._attempts: Dict[str, int] = {}
        self._consecutive_failures = 0
        self._halt_reason: Optional[str] = None
        self._terminal_status: Optional[RunStatus] = None
        self._draining = False
        self._awaiting_approval: Set[str] = set()

    # =========================================================================
    # entry point
    # =========================================================================

    def run(self, inputs: Optional[Dict[str, Any]] = None, run_id: Optional[str] = None,
            resume: bool = False) -> RunReport:
        inputs = dict(inputs or {})
        run_id = run_id or new_run_id(self.graph.name.replace("_", "-"))

        journal = (Journal.load(self.config.journal_path)
                   if (resume and self.config.journal_path and os.path.exists(self.config.journal_path))
                   else Journal(self.config.journal_path))

        if resume:
            self.state = RunState.from_events(journal.events(), run_id, self.graph.name)
            self.state.journal = journal
        else:
            self.state = RunState(run_id, self.graph.name, journal, inputs)

        self.audit = AuditLog(self.config.audit_path, run_id)
        self.context = RunContext(inputs)

        if self.config.workspace:
            self.context.put("workspace_root", os.path.abspath(self.config.workspace),
                             "__engine__", "workspace confinement boundary")

        if resume:
            self._rehydrate(journal)

        self.state.emit("run_started", inputs=_redact(inputs))
        self.audit.record("run_started", workflow=self.graph.name, inputs=_redact(inputs),
                          nodes=self.graph.node_ids(), policies=self.policy.describe())

        for node_id in self.graph.node_ids():
            if node_id not in self.state.nodes:
                self.state.emit("node_registered", node_id=node_id)

        if self.config.snapshot_workspace and self.config.workspace:
            self._snapshot = WorkspaceSnapshot(self.config.workspace, label=run_id)
            self.audit.record("workspace_snapshot", **self._snapshot.describe().to_dict())

        try:
            self._schedule_loop()
        except Exception as exc:  # scheduler bug: fail loudly, never silently
            self._halt_reason = "scheduler error: %s" % exc
            self._terminal_status = RunStatus.FAILED
            self.audit.record("scheduler_error", error=str(exc), traceback=traceback.format_exc())
        finally:
            if self.state.status is RunStatus.RUNNING:
                self._finalise()

        return self._build_report()

    # =========================================================================
    # scheduling
    # =========================================================================

    def _schedule_loop(self) -> None:
        inflight: Dict[Future, _Scheduled] = {}
        iterations = 0

        with ThreadPoolExecutor(max_workers=self.config.max_parallelism,
                                thread_name_prefix="agent") as pool:
            while True:
                iterations += 1
                if iterations > self.config.max_iterations:
                    self._halt("scheduler exceeded %d iterations" % self.config.max_iterations)
                if self._halt_reason:
                    break

                # 1. Admit as many ready nodes as capacity allows.
                admitted = self._admit(pool, inflight)

                # 2. Nothing running and nothing admitted -> the run is done, or
                #    it is blocked and we need to say why rather than hang.
                if not inflight:
                    if admitted:
                        continue
                    if self._retry_at:
                        self._sleep_until_next_retry()
                        continue
                    if self._awaiting_approval:
                        break
                    if self._has_unreached_work():
                        self._halt("no runnable nodes remain but work is unfinished "
                                   "(blocked by failed or skipped dependencies)")
                    break

                # 3. React to whatever finishes first.
                timeout = self._next_timeout(inflight)
                done, _ = wait(list(inflight), timeout=timeout, return_when=FIRST_COMPLETED)
                if not done:
                    self._reap_timeouts(inflight)
                    continue
                for future in done:
                    scheduled = inflight.pop(future)
                    self._complete(scheduled, future)
                    if self._halt_reason:
                        break

            # The run has stopped scheduling, but nodes may still be running.
            # Record what they did rather than discarding it: the journal must
            # describe what actually happened, and a partially applied change
            # that is not in the record cannot be rolled back knowingly.
            self._draining = True
            while inflight:
                done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                for future in done:
                    scheduled = inflight.pop(future)
                    self._complete(scheduled, future)

    def _admit(self, pool: ThreadPoolExecutor, inflight: Dict[Future, _Scheduled]) -> bool:
        """Move eligible nodes into execution. Returns True if state changed."""
        changed = False
        now = time.time()
        self._publish_statuses()

        for node_id in self._ready_node_ids():
            if len(inflight) >= self.config.max_parallelism:
                break
            if node_id in self._retry_at and self._retry_at[node_id] > now:
                continue
            self._retry_at.pop(node_id, None)
            node = self.graph.get(node_id)

            # --- entry gates -------------------------------------------------
            entry = evaluate_gates(node.entry_gates, self.context, node, None)
            if entry:
                self.state.emit("gates_evaluated", node_id=node_id, phase="entry",
                                results=[g.to_dict() for g in entry])
                self.audit.record("entry_gates", node_id=node_id,
                                  results=[g.to_dict() for g in entry])
            if not gates_passed(entry):
                self._handle_entry_gate_failure(node, entry)
                changed = True
                continue

            # --- pre-execution policy ----------------------------------------
            verdict = self.policy.evaluate_pre(node, self.context)
            if verdict.decisions:
                self.state.emit("policy_evaluated", node_id=node_id, verdict=verdict.to_dict())
                self.audit.record("policy_pre", node_id=node_id, **verdict.to_dict())
            if verdict.denied:
                self._fail_node(node, "denied by policy: %s" % "; ".join(verdict.blocking_reasons()))
                changed = True
                continue

            # --- human checkpoint --------------------------------------------
            needs_human = node.requires_approval or verdict.needs_approval
            if needs_human and not self.config.dry_run:
                granted = self._request_approval(
                    node,
                    summary=node.approval_prompt or ("execute %s (%s)" % (node.id, node.stage.value)),
                    reasons=verdict.blocking_reasons() or [node.description or "declared checkpoint"],
                    phase="pre",
                )
                if granted is None:      # parked awaiting an out-of-band decision
                    changed = True
                    continue
                if granted is False:
                    self._fail_node(node, "human approval denied")
                    changed = True
                    continue

            # --- dispatch -----------------------------------------------------
            attempt = self._attempts.get(node_id, 0) + 1
            self._attempts[node_id] = attempt
            self.state.emit("attempt_started", node_id=node_id, attempt=attempt)
            self.audit.record("node_started", node_id=node_id, attempt=attempt,
                              agent=getattr(node.agent, "name", "agent"),
                              autonomy=node.autonomy.name)
            started = time.time()
            deadline = started + node.timeout_seconds if node.timeout_seconds else None
            future = pool.submit(self._invoke_agent, node, attempt)
            inflight[future] = _Scheduled(node_id, attempt, future, deadline, started)
            changed = True

        return changed

    def _ready_node_ids(self) -> List[str]:
        """Nodes whose dependencies are satisfied and which are not done."""
        ready: List[str] = []
        for node_id in self.graph.node_ids():
            node_state = self.state.nodes.get(node_id)
            status = node_state.status if node_state else NodeStatus.PENDING
            if status not in (NodeStatus.PENDING, NodeStatus.RETRYING, NodeStatus.INVALIDATED):
                continue
            if node_id in self._awaiting_approval:
                continue
            node = self.graph.get(node_id)
            if all(self._dependency_satisfied(dep) for dep in node.depends_on):
                ready.append(node_id)
        return ready

    def _dependency_satisfied(self, dep_id: str) -> bool:
        """Whether ``dep_id`` has produced enough for its dependents to start.

        A node declared ``on_failure=CONTINUE`` is non-blocking by definition:
        its failure is recorded and surfaced, but the graph is explicitly
        allowed to proceed without it. Gates on the dependent decide whether
        the missing output actually matters.
        """
        node_state = self.state.nodes.get(dep_id)
        if node_state is None:
            return False
        if node_state.status in SATISFIED_STATUSES:
            return True
        return (
            node_state.status is NodeStatus.FAILED
            and self.graph.get(dep_id).on_failure is FailureAction.CONTINUE
        )

    def _has_unreached_work(self) -> bool:
        return any(
            (self.state.nodes.get(nid).status if self.state.nodes.get(nid) else NodeStatus.PENDING)
            in (NodeStatus.PENDING, NodeStatus.RETRYING, NodeStatus.INVALIDATED)
            for nid in self.graph.node_ids()
        )

    def _sleep_until_next_retry(self) -> None:
        soonest = min(self._retry_at.values())
        time.sleep(max(0.0, min(soonest - time.time(), 1.0)))

    def _next_timeout(self, inflight: Dict[Future, _Scheduled]) -> float:
        deadlines = [s.deadline for s in inflight.values() if s.deadline]
        if not deadlines:
            return 1.0
        return max(self.config.poll_interval, min(deadlines) - time.time())

    def _reap_timeouts(self, inflight: Dict[Future, _Scheduled]) -> None:
        """Fail nodes past their deadline.

        The worker thread cannot be killed -- Python has no safe thread
        cancellation -- so it is abandoned and its late result discarded. Agents
        that touch shared state must therefore be idempotent; this is called out
        in docs/RISKS_AND_TRADEOFFS.md.
        """
        now = time.time()
        expired = [f for f, s in inflight.items() if s.deadline and s.deadline <= now]
        for future in expired:
            scheduled = inflight.pop(future)
            future.cancel()
            node = self.graph.get(scheduled.node_id)
            self.state.emit("attempt_finished", node_id=node.id, outcome="timeout",
                            error="exceeded %ss" % node.timeout_seconds)
            self.audit.record("node_timeout", node_id=node.id, attempt=scheduled.attempt,
                              timeout_seconds=node.timeout_seconds)
            self._after_failure(node, "timed out after %ss" % node.timeout_seconds)

    # =========================================================================
    # execution
    # =========================================================================

    def _invoke_agent(self, node: Node, attempt: int) -> AgentResult:
        """Runs on a worker thread. Must not mutate engine state."""
        agent_ctx = AgentContext(
            run_id=self.state.run_id,
            node_id=node.id,
            context=self.context,
            workspace=self.config.workspace or os.getcwd(),
            artifacts_dir=os.path.join(self.config.artifacts_dir, self.state.run_id),
            attempt=attempt,
            dry_run=self.config.dry_run,
        )
        try:
            result = node.agent.execute(agent_ctx)
        except Exception as exc:
            return AgentResult(
                outcome=AgentOutcome.FAILED,
                rationale="agent raised %s: %s" % (type(exc).__name__, exc),
                metrics={"exception": type(exc).__name__},
                findings=[Finding(Severity.HIGH, "agent raised %s: %s" % (type(exc).__name__, exc),
                                  category="execution")],
            )
        if not isinstance(result, AgentResult):  # defensive: agents are pluggable
            return AgentResult(outcome=AgentOutcome.FAILED,
                               rationale="agent returned %r, expected AgentResult" % type(result))
        return result

    def _complete(self, scheduled: _Scheduled, future: Future) -> None:
        node = self.graph.get(scheduled.node_id)
        try:
            result: AgentResult = future.result()
        except Exception as exc:  # pragma: no cover - _invoke_agent already traps
            result = AgentResult(outcome=AgentOutcome.FAILED, rationale=str(exc))

        self.state.emit("attempt_finished", node_id=node.id,
                        outcome=result.outcome.value,
                        error=None if result.ok else result.rationale)

        # --- agent-reported failure ------------------------------------------
        if result.outcome is AgentOutcome.FAILED:
            self.audit.record("node_agent_failed", node_id=node.id, attempt=scheduled.attempt,
                              rationale=result.rationale)
            self._after_failure(node, result.rationale or "agent failed", result)
            return

        # --- agent needs a human answer --------------------------------------
        if result.outcome is AgentOutcome.NEEDS_INPUT:
            self._handle_needs_input(node, result)
            return

        # --- exit gates -------------------------------------------------------
        exit_results = evaluate_gates(node.exit_gates, self.context, node, result)
        if exit_results:
            self.state.emit("gates_evaluated", node_id=node.id, phase="exit",
                            results=[g.to_dict() for g in exit_results])
            self.audit.record("exit_gates", node_id=node.id,
                              results=[g.to_dict() for g in exit_results])
        if not gates_passed(exit_results):
            failed = [g.reason for g in exit_results if not g.passed]
            self._after_failure(node, "exit gate(s) failed: %s" % "; ".join(failed), result)
            return

        # --- post-execution policy -------------------------------------------
        verdict = self.policy.evaluate_post(node, self.context, result)
        if verdict.decisions:
            self.state.emit("policy_evaluated", node_id=node.id, verdict=verdict.to_dict())
            self.audit.record("policy_post", node_id=node.id, **verdict.to_dict())
        if verdict.denied:
            # A policy denial is a governance stop, not a flaky failure. Retrying
            # would just re-run a deterministic violation.
            self._fail_node(node, "denied by policy: %s" % "; ".join(verdict.blocking_reasons()),
                            result, retryable=False)
            return
        if verdict.needs_approval and not self.config.dry_run:
            granted = self._request_approval(
                node,
                summary="accept output of %s (%s)" % (node.id, node.stage.value),
                reasons=verdict.blocking_reasons(),
                phase="post",
                pending_result=result,
            )
            if granted is None:
                return
            if granted is False:
                self._fail_node(node, "human approval denied for produced output",
                                result, retryable=False)
                return

        self._succeed_node(node, result)

    def _handle_needs_input(self, node: Node, result: AgentResult) -> None:
        """An agent stopped because it cannot proceed without a human answer.

        This is a first-class outcome, not a failure. It is the mechanism the
        ambiguous scenario uses to refuse to guess.
        """
        self.audit.record("node_needs_input", node_id=node.id, questions=result.questions,
                          rationale=result.rationale)
        granted = self._request_approval(
            node,
            summary=result.rationale or "clarification required for %s" % node.id,
            reasons=result.questions or ["agent requested human input"],
            phase="clarification",
            risk=Severity.HIGH,
            pending_result=result,
        )
        if granted is None:
            return
        if granted is False:
            self._fail_node(node, "clarification denied; cannot proceed on assumptions",
                            result, retryable=False)
            return
        # Approved: the human accepted the agent's documented assumptions, which
        # become part of the record.
        self.context.put("resolved_assumptions",
                         (self.context.get("resolved_assumptions") or []) + result.questions,
                         node.id, "human accepted documented assumptions")
        result.outcome = AgentOutcome.OK
        self._succeed_node(node, result)

    # =========================================================================
    # outcomes
    # =========================================================================

    def _succeed_node(self, node: Node, result: AgentResult) -> None:
        self._consecutive_failures = 0
        self._persist_artifacts(node, result)

        if result.outputs:
            self.context.put_all(result.outputs, node.id,
                                 result.rationale or "output of %s" % node.id)

        self.state.emit("node_succeeded", node_id=node.id, result=result.to_dict())
        self.audit.record("node_succeeded", node_id=node.id,
                          outputs=sorted(result.outputs.keys()),
                          artifacts=[a.name for a in result.artifacts],
                          findings=len(result.findings), rationale=result.rationale)

        if node.compensation is not None:
            self.compensations.register(node.id, node.compensation)

        self._expand_graph(node, result)
        self._replan_after(node)

    def _fail_node(self, node: Node, reason: str, result: Optional[AgentResult] = None,
                   retryable: bool = True) -> None:
        self.state.emit("node_failed", node_id=node.id, error=reason,
                        result=result.to_dict() if result else None)
        self.audit.record("node_failed", node_id=node.id, reason=reason, retryable=retryable)
        self._consecutive_failures += 1
        self._apply_failure_action(node, reason)

    def _after_failure(self, node: Node, reason: str,
                       result: Optional[AgentResult] = None) -> None:
        """Decide between another attempt and giving up on this node."""
        attempt = self._attempts.get(node.id, 1)
        retryable = result is None or result.outcome in node.retry.retry_on
        if retryable and attempt < node.retry.max_attempts:
            delay = node.retry.delay_for(attempt)
            self._retry_at[node.id] = time.time() + delay
            self.state.emit("node_status", node_id=node.id, status=NodeStatus.RETRYING.value,
                            note="attempt %d failed: %s" % (attempt, reason))
            self.audit.record("node_retry_scheduled", node_id=node.id, attempt=attempt,
                              next_attempt=attempt + 1, delay_seconds=round(delay, 3),
                              reason=reason)
            return
        self._fail_node(node, reason, result, retryable=retryable)

    def _apply_failure_action(self, node: Node, reason: str) -> None:
        action = node.on_failure

        if self._draining:
            # Already stopping. Record the failure; do not start new recovery
            # work that the scheduler will never get a chance to run.
            self.audit.record("failure_during_drain", node_id=node.id, reason=reason)
            return

        if (self._consecutive_failures >= self.config.safe_stop_after_consecutive_failures
                and action is not FailureAction.CONTINUE):
            # Repeated failures usually mean a shared, systemic cause. Continuing
            # to burn attempts across the graph destroys evidence.
            self._halt("safe-stop: %d consecutive node failures (last: %s)"
                       % (self._consecutive_failures, node.id))
            return

        if action is FailureAction.CONTINUE:
            self.audit.record("failure_absorbed", node_id=node.id, reason=reason)
            return

        if action is FailureAction.FALLBACK and node.fallback_agent is not None:
            self.audit.record("fallback_engaged", node_id=node.id,
                              fallback=getattr(node.fallback_agent, "name", "fallback"))
            node.agent = node.fallback_agent
            node.fallback_agent = None
            node.retry.max_attempts = max(node.retry.max_attempts, self._attempts.get(node.id, 1) + 1)
            self._retry_at[node.id] = time.time()
            self.state.emit("node_status", node_id=node.id, status=NodeStatus.RETRYING.value,
                            note="switched to fallback agent")
            return

        if action is FailureAction.SAFE_STOP:
            self._halt("safe-stop requested by %s: %s" % (node.id, reason))
            return

        if action is FailureAction.ROLLBACK:
            self._rollback(node.id, reason)
            self._halt_reason = "rolled back after %s failed: %s" % (node.id, reason)
            self._terminal_status = RunStatus.ROLLED_BACK
            return

        self._halt_reason = "%s failed: %s" % (node.id, reason)
        self._terminal_status = RunStatus.FAILED

    def _handle_entry_gate_failure(self, node: Node, results: List[GateResult]) -> None:
        reasons = "; ".join(g.reason for g in results if not g.passed)
        if blocking_failure(results):
            # A blocking entry gate means a precondition the workflow depends on
            # is absent -- proceeding would produce output built on nothing.
            self.audit.record("entry_gate_blocked", node_id=node.id, reason=reasons)
            self._fail_node(node, "blocked by entry gate: %s" % reasons, retryable=False)
        else:
            self.state.emit("node_skipped", node_id=node.id,
                            reason="entry gate declined: %s" % reasons)
            self.audit.record("node_skipped", node_id=node.id, reason=reasons)

    def _halt(self, reason: str) -> None:
        self._halt_reason = reason
        self._terminal_status = RunStatus.HALTED
        self.audit.record("safe_stop", reason=reason)
        for node_id in self.graph.node_ids():
            node_state = self.state.nodes.get(node_id)
            if node_state and node_state.status in (NodeStatus.PENDING, NodeStatus.RETRYING,
                                                    NodeStatus.INVALIDATED):
                self.state.emit("node_halted", node_id=node_id, reason="run safe-stopped")

    # =========================================================================
    # governance
    # =========================================================================

    def _request_approval(self, node: Node, summary: str, reasons: List[str], phase: str,
                          risk: Optional[Severity] = None,
                          pending_result: Optional[AgentResult] = None) -> Optional[bool]:
        """Ask a human. Returns True/False, or None when the run must pause."""
        assessed = risk or risk_for(node, reasons)
        request = ApprovalRequest(
            run_id=self.state.run_id, node_id=node.id, stage=node.stage.value,
            summary=summary, risk=assessed, reasons=list(reasons),
            details={"phase": phase, "autonomy": node.autonomy.name},
        )
        self.state.emit("approval_requested", node_id=node.id, phase=phase,
                        summary=summary, risk=assessed.value, reasons=list(reasons))
        self.audit.record("approval_requested", node_id=node.id, phase=phase,
                          risk=assessed.value, reasons=list(reasons), summary=summary)

        waited_from = time.time()
        decision = self.approvals.request(request)

        if decision.pending:
            self._awaiting_approval.add(node.id)
            self.state.emit("node_status", node_id=node.id,
                            status=NodeStatus.AWAITING_APPROVAL.value,
                            note="awaiting out-of-band approval")
            self.audit.record("approval_deferred", node_id=node.id, broker=self.approvals.name)
            return None

        self._awaiting_approval.discard(node.id)
        self.state.emit("approval_resolved", node_id=node.id,
                        decision=decision.approved, decided_by=decision.decided_by,
                        reason=decision.reason,
                        waited_seconds=time.time() - waited_from)
        self.audit.record("approval_resolved", node_id=node.id, actor=decision.decided_by,
                          decision="approved" if decision.approved else "rejected",
                          reason=decision.reason)
        return decision.approved

    def _publish_statuses(self) -> None:
        """Expose node statuses to gates without recording a read dependency."""
        self.context.put(NODE_STATUS_KEY, self.state.status_map(), "__engine__",
                         "node status snapshot for gate evaluation")

    # =========================================================================
    # dynamic graph and re-planning
    # =========================================================================

    def _expand_graph(self, node: Node, result: AgentResult) -> None:
        if not result.proposed_nodes and not result.rewire:
            return

        # Rewiring a node that has already started is unsound: it would claim a
        # dependency the node never actually waited for. Drop those edges and
        # say so rather than record a barrier that never existed.
        attach: Dict[str, List[str]] = {}
        for target, extra in (result.rewire or {}).items():
            target_state = self.state.nodes.get(target)
            if target_state and target_state.status is not NodeStatus.PENDING:
                self.audit.record("rewire_rejected", node_id=node.id, target=target,
                                  reason="target is %s, not pending" % target_state.status.value)
                continue
            attach[target] = list(extra)

        try:
            added = self.graph.expand(result.proposed_nodes, injected_by=node.id, attach=attach)
        except Exception as exc:
            # A bad expansion is the planner's bug, not a reason to lose the run.
            self.audit.record("graph_expansion_rejected", node_id=node.id, error=str(exc))
            self.state.emit("node_status", node_id=node.id, status=NodeStatus.SUCCEEDED.value,
                            note="graph expansion rejected: %s" % exc)
            return
        if added:
            self.state.emit("nodes_injected", node_id=node.id, nodes=added)
        self.audit.record("nodes_injected", node_id=node.id, nodes=added,
                          rewired=attach, new_size=len(self.graph))

    def _replan_after(self, node: Node) -> None:
        if self._draining:
            return
        decision = self.replanner.evaluate(node.id, self.graph, self.context, self.state)
        if decision.exceeded_budget:
            self._halt("re-plan budget exhausted for %s; human review required"
                       % ", ".join(decision.exceeded_budget))
            return
        if not decision.invalidated:
            return
        self.audit.record("replan", node_id=node.id, invalidated=decision.invalidated,
                          reason=decision.reason, drift=decision.drift)
        for target in decision.invalidated:
            self.state.emit("node_invalidated", node_id=target, trigger=node.id,
                            drift=decision.drift.get(target, {}))
            self.context.clear_reads(target)
            self._attempts.pop(target, None)
            self._retry_at.pop(target, None)
            self.state.emit("node_reset", node_id=target)

    # =========================================================================
    # rollback and completion
    # =========================================================================

    def _rollback(self, triggered_by: str, reason: str) -> RollbackReport:
        report = RollbackReport(triggered_by=triggered_by, reason=reason)
        self.audit.record("rollback_started", node_id=triggered_by, reason=reason,
                          compensations=self.compensations.node_ids())
        report.compensations = self.compensations.run_all(self.context)
        for outcome in report.compensations:
            if outcome.succeeded:
                self.state.emit("node_compensated", node_id=outcome.node_id)
        if self._snapshot is not None:
            try:
                report.files_restored = self._snapshot.restore()
                report.snapshot_restored = True
            except Exception as exc:
                self.audit.record("rollback_snapshot_failed", error=str(exc))
        report.completed_at = time.time()
        self.rollbacks.append(report)
        self.audit.record("rollback_finished", node_id=triggered_by, **report.to_dict())
        return report

    def _persist_artifacts(self, node: Node, result: AgentResult) -> None:
        if not result.artifacts:
            return
        base = os.path.join(self.config.artifacts_dir, self.state.run_id)
        for artifact in result.artifacts:
            if artifact.content is None or not artifact.path:
                continue
            target = os.path.join(base, artifact.path)
            # Confinement is enforced by policy too; this is defence in depth
            # against a path that never reaches a policy check.
            if not os.path.realpath(target).startswith(os.path.realpath(base)):
                self.audit.record("artifact_rejected", node_id=node.id, path=artifact.path,
                                  reason="path escapes the artifacts directory")
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(artifact.content)

    def _finalise(self) -> None:
        """Emit the single terminal event, after every node has stopped moving.

        Emitting it earlier -- at the moment a node fails -- would stamp the run
        duration before its still-running siblings finished, making end-to-end
        latency shorter than the work it contains.
        """
        failed = [nid for nid, s in self.state.nodes.items()
                  if s.status in (NodeStatus.FAILED, NodeStatus.HALTED)]
        # A node that never reached a terminal state means the run stopped with
        # work outstanding. Reporting SUCCEEDED here would be the single most
        # dangerous lie this system could tell.
        unfinished = [nid for nid in self.graph.node_ids()
                      if self.state.nodes.get(nid)
                      and self.state.nodes[nid].status not in TERMINAL_STATUSES]

        if self._terminal_status is not None:
            status, reason = self._terminal_status, self._halt_reason
        elif self._awaiting_approval:
            status = RunStatus.AWAITING_APPROVAL
            reason = "paused for approval on %s" % ", ".join(sorted(self._awaiting_approval))
        elif failed:
            status, reason = RunStatus.FAILED, "%d node(s) failed: %s" % (len(failed),
                                                                          ", ".join(failed))
        elif unfinished:
            status = RunStatus.HALTED
            reason = "stopped with %d node(s) unfinished: %s" % (len(unfinished),
                                                                 ", ".join(unfinished))
        else:
            status, reason = RunStatus.SUCCEEDED, None

        self.state.emit("run_finished", status=status.value, reason=reason)
        self.audit.record("run_finished", status=self.state.status.value,
                          reason=self.state.failure_reason)

    def _rehydrate(self, journal: Journal) -> None:
        """Rebuild a prior run's context and re-arm its unfinished nodes.

        Two halves. Replaying the outputs of succeeded nodes restores the shared
        context without re-executing expensive work. Resetting every *unfinished*
        node back to PENDING is what makes the resume actually resume: a node
        parked in AWAITING_APPROVAL is not schedulable, so without the reset the
        run would finish reporting success having never executed it.
        """
        replayed = 0
        for event in journal.events():
            if event.get("type") != "node_succeeded":
                continue
            result = (event.get("payload") or {}).get("result") or {}
            outputs = result.get("outputs") or {}
            if outputs:
                self.context.put_all(outputs, event.get("node_id") or "__replay__",
                                     "replayed from journal")
                replayed += 1
            self._attempts[event["node_id"]] = 1

        rearmed = []
        for node_id in self.graph.node_ids():
            node_state = self.state.nodes.get(node_id)
            if node_state is None or node_state.status in SATISFIED_STATUSES:
                continue
            self.state.emit("node_reset", node_id=node_id)
            self._attempts.pop(node_id, None)
            rearmed.append(node_id)
            # The pause is over and the request is about to be re-issued. Leaving
            # the old unresolved record in place would report the same decision
            # as both granted and still pending.
            self.state.approvals[:] = [
                a for a in self.state.approvals
                if not (a["node_id"] == node_id and "decision" not in a)
            ]

        self.audit.record("run_resumed", replayed_nodes=replayed, rearmed_nodes=rearmed)

    def _build_report(self) -> RunReport:
        stage_of = {nid: node.stage.value for nid, node in self.graph.nodes.items()}
        metrics = compute_metrics(self.state, stage_of, self.rollbacks)

        findings: List[Finding] = []
        artifacts: List[Artifact] = []
        for node_state in self.state.nodes.values():
            result = node_state.result or {}
            for raw in result.get("findings", []):
                findings.append(Finding(
                    severity=Severity(raw["severity"]), message=raw["message"],
                    category=raw.get("category", "general"), location=raw.get("location"),
                    remediation=raw.get("remediation"),
                ))
            for raw in result.get("artifacts", []):
                artifacts.append(Artifact(name=raw["name"], kind=raw["kind"], path=raw.get("path"),
                                          digest=raw.get("digest")))

        return RunReport(
            run_id=self.state.run_id, workflow=self.graph.name, status=self.state.status,
            state=self.state, metrics=metrics, context=self.context, graph=self.graph,
            audit=self.audit, rollbacks=self.rollbacks, findings=findings, artifacts=artifacts,
            pending_approvals=self.state.pending_approvals(),
        )


_SENSITIVE_HINTS = ("key", "token", "secret", "password", "credential")


def _redact(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Keep credential-shaped inputs out of the journal and audit log."""
    clean: Dict[str, Any] = {}
    for key, value in payload.items():
        if any(hint in key.lower() for hint in _SENSITIVE_HINTS):
            clean[key] = "<redacted>"
        elif isinstance(value, dict):
            clean[key] = _redact(value)
        else:
            clean[key] = value
    return clean
