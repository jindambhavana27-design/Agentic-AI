"""Reliability metrics derived from the run journal.

Nothing here is instrumented separately. Every number is a fold over the same
events the audit log records, which means the metrics cannot drift from the
trace and cannot be inflated by an agent reporting its own success.

Definitions worth stating explicitly, because they are usually fudged:

* **success rate** -- succeeded nodes over nodes that were *attempted*. Skipped
  nodes are excluded; counting a skip as a success flatters the number.
* **MTTR** -- mean time from a node's first failed attempt to its eventual
  success. Nodes that never recovered are excluded from the mean and reported
  separately as ``unrecovered_failures``; folding them in as zero or infinity
  both lie.
* **execution latency** -- wall clock minus time parked awaiting human approval.
  Otherwise the metric measures reviewer availability, not the system.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .state import RunState
from .types import NodeStatus


@dataclass
class RunMetrics:
    run_id: str
    workflow: str
    status: str

    nodes_total: int = 0
    nodes_attempted: int = 0
    nodes_succeeded: int = 0
    nodes_failed: int = 0
    nodes_skipped: int = 0
    nodes_compensated: int = 0

    success_rate: float = 0.0
    first_pass_rate: float = 0.0

    total_attempts: int = 0
    total_retries: int = 0
    retry_rate: float = 0.0
    nodes_retried: int = 0

    rollback_count: int = 0
    rollback_frequency: float = 0.0

    replan_count: int = 0
    replanned_nodes: int = 0

    mttr_seconds: Optional[float] = None
    unrecovered_failures: int = 0

    e2e_latency_seconds: float = 0.0
    execution_seconds: float = 0.0
    approval_wait_seconds: float = 0.0
    critical_path_seconds: float = 0.0
    parallel_efficiency: float = 0.0

    approvals_requested: int = 0
    approvals_granted: int = 0
    approvals_denied: int = 0
    approvals_pending: int = 0

    gates_evaluated: int = 0
    gates_failed: int = 0
    gate_pass_rate: float = 0.0

    policy_denials: int = 0
    policy_approvals_required: int = 0
    policy_warnings: int = 0

    per_stage_seconds: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def render(self) -> str:
        lines = [
            "run %s [%s] -- %s" % (self.run_id, self.workflow, self.status),
            "  nodes            : %d total, %d attempted, %d succeeded, %d failed, %d skipped"
            % (self.nodes_total, self.nodes_attempted, self.nodes_succeeded,
               self.nodes_failed, self.nodes_skipped),
            "  success rate     : %.1f%%  (first-pass %.1f%%)"
            % (self.success_rate * 100, self.first_pass_rate * 100),
            "  retries          : %d across %d node(s), retry rate %.2f/node"
            % (self.total_retries, self.nodes_retried, self.retry_rate),
            "  rollbacks        : %d  (frequency %.2f/run-node)"
            % (self.rollback_count, self.rollback_frequency),
            "  re-plans         : %d event(s) over %d node(s)"
            % (self.replan_count, self.replanned_nodes),
            "  MTTR             : %s"
            % ("%.2fs" % self.mttr_seconds if self.mttr_seconds is not None else "n/a"),
            "  unrecovered      : %d" % self.unrecovered_failures,
            "  e2e latency      : %.2fs  (execution %.2fs, approval wait %.2fs)"
            % (self.e2e_latency_seconds, self.execution_seconds, self.approval_wait_seconds),
            "  parallel gain    : %.2fx" % self.parallel_efficiency,
            "  approvals        : %d requested, %d granted, %d denied, %d pending"
            % (self.approvals_requested, self.approvals_granted,
               self.approvals_denied, self.approvals_pending),
            "  gates            : %d evaluated, %d failed (pass rate %.1f%%)"
            % (self.gates_evaluated, self.gates_failed, self.gate_pass_rate * 100),
            "  policy           : %d denials, %d approval escalations, %d warnings"
            % (self.policy_denials, self.policy_approvals_required, self.policy_warnings),
        ]
        if self.per_stage_seconds:
            lines.append("  stage timings    :")
            for stage, seconds in sorted(self.per_stage_seconds.items(),
                                         key=lambda kv: kv[1], reverse=True):
                lines.append("      %-16s %.2fs" % (stage, seconds))
        return "\n".join(lines)


def compute_metrics(state: RunState, stage_of: Optional[Dict[str, str]] = None,
                    rollback_reports: Optional[List[Any]] = None) -> RunMetrics:
    metrics = RunMetrics(run_id=state.run_id, workflow=state.workflow, status=state.status.value)
    nodes = list(state.nodes.values())
    metrics.nodes_total = len(nodes)

    # "Attempted" counts every node the engine ruled on, including one blocked
    # by an entry gate before its agent ever ran. Excluding those would let a
    # run that failed at the gate report a 100% success rate.
    attempted = [
        n for n in nodes
        if n.attempt_count > 0 or n.status in (NodeStatus.FAILED, NodeStatus.HALTED)
    ]
    metrics.nodes_attempted = len(attempted)
    metrics.nodes_succeeded = sum(1 for n in nodes if n.status is NodeStatus.SUCCEEDED)
    metrics.nodes_failed = sum(1 for n in nodes if n.status is NodeStatus.FAILED)
    metrics.nodes_skipped = sum(1 for n in nodes if n.status is NodeStatus.SKIPPED)
    metrics.nodes_compensated = sum(1 for n in nodes if n.rolled_back)

    if metrics.nodes_attempted:
        metrics.success_rate = metrics.nodes_succeeded / metrics.nodes_attempted
        first_pass = sum(
            1 for n in attempted
            if n.status is NodeStatus.SUCCEEDED and n.attempt_count == 1 and n.replan_count == 0
        )
        metrics.first_pass_rate = first_pass / metrics.nodes_attempted

    metrics.total_attempts = sum(n.attempt_count for n in nodes)
    metrics.total_retries = sum(n.retry_count for n in nodes)
    metrics.nodes_retried = sum(1 for n in nodes if n.retry_count > 0)
    if metrics.nodes_attempted:
        metrics.retry_rate = metrics.total_retries / metrics.nodes_attempted

    reports = rollback_reports or []
    metrics.rollback_count = len(reports)
    if metrics.nodes_attempted:
        metrics.rollback_frequency = metrics.rollback_count / metrics.nodes_attempted

    metrics.replan_count = len(state.replan_events)
    metrics.replanned_nodes = sum(1 for n in nodes if n.replan_count > 0)

    # -- MTTR -----------------------------------------------------------------
    recovery_times: List[float] = []
    for node in nodes:
        failed_attempts = [a for a in node.attempts if a.outcome not in (None, "ok")]
        if not failed_attempts:
            continue
        if node.status is NodeStatus.SUCCEEDED and node.finished_at:
            first_failure = min(a.started_at for a in failed_attempts)
            recovery_times.append(max(0.0, node.finished_at - first_failure))
        elif node.status in (NodeStatus.FAILED, NodeStatus.HALTED, NodeStatus.COMPENSATED):
            metrics.unrecovered_failures += 1
    if recovery_times:
        metrics.mttr_seconds = sum(recovery_times) / len(recovery_times)

    # -- latency --------------------------------------------------------------
    metrics.e2e_latency_seconds = state.duration_seconds
    metrics.execution_seconds = sum(n.execution_seconds for n in nodes)
    metrics.approval_wait_seconds = sum(n.approval_wait_seconds for n in nodes)
    if metrics.e2e_latency_seconds > 0:
        # >1 means work genuinely overlapped; ~1 means the graph ran serially.
        metrics.parallel_efficiency = metrics.execution_seconds / metrics.e2e_latency_seconds
    metrics.critical_path_seconds = max((n.execution_seconds for n in nodes), default=0.0)

    # -- governance -----------------------------------------------------------
    metrics.approvals_requested = len(state.approvals)
    metrics.approvals_granted = sum(1 for a in state.approvals if a.get("decision") is True)
    metrics.approvals_denied = sum(1 for a in state.approvals if a.get("decision") is False)
    metrics.approvals_pending = sum(1 for a in state.approvals if "decision" not in a)

    all_gates = [g for n in nodes for g in (n.entry_gates + n.exit_gates)]
    metrics.gates_evaluated = len(all_gates)
    metrics.gates_failed = sum(1 for g in all_gates if not g.get("passed", True))
    if metrics.gates_evaluated:
        metrics.gate_pass_rate = 1.0 - (metrics.gates_failed / metrics.gates_evaluated)

    for node in nodes:
        for verdict in node.policy:
            for decision in verdict.get("decisions", []):
                effect = decision.get("effect")
                if effect == "deny":
                    metrics.policy_denials += 1
                elif effect == "require_approval":
                    metrics.policy_approvals_required += 1
                elif effect == "warn":
                    metrics.policy_warnings += 1

    if stage_of:
        for node in nodes:
            stage = stage_of.get(node.node_id, "unknown")
            metrics.per_stage_seconds[stage] = round(
                metrics.per_stage_seconds.get(stage, 0.0) + node.execution_seconds, 4
            )

    return metrics
