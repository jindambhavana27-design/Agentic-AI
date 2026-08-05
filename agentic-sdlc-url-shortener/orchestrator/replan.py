"""Dynamic re-planning.

Linear pipelines assume upstream stages are final. They are not: a clarification
lands, a design decision is revised, a security review rejects an approach. When
that happens the honest answer is that everything downstream is now built on
stale input.

This module answers one question -- *given that node X rewrote context, which
completed nodes are now invalid?* -- by comparing the versions each node
observed (recorded by :class:`~orchestrator.context.RunContext`) against current
versions. That is precise: a node that never read the changed key is not
disturbed, and a node that read it is re-run even if the change looks cosmetic.

Two safeguards keep re-planning from becoming a loop:

* Content-addressed versions mean an idempotent rewrite is not a change at all.
* ``max_replans_per_node`` caps how often any single node may be invalidated;
  past the cap the run safe-stops for a human instead of spinning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .context import RunContext
from .graph import WorkflowGraph
from .state import RunState
from .types import NodeStatus


@dataclass
class ReplanDecision:
    trigger_node: str
    invalidated: List[str] = field(default_factory=list)
    drift: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    reason: str = ""
    exceeded_budget: List[str] = field(default_factory=list)

    @property
    def any_change(self) -> bool:
        return bool(self.invalidated) or bool(self.exceeded_budget)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trigger_node": self.trigger_node,
            "invalidated": self.invalidated,
            "drift": self.drift,
            "reason": self.reason,
            "exceeded_budget": self.exceeded_budget,
        }


class Replanner:
    def __init__(self, max_replans_per_node: int = 2) -> None:
        self.max_replans_per_node = max_replans_per_node

    def evaluate(self, trigger_node: str, graph: WorkflowGraph, context: RunContext,
                 state: RunState) -> ReplanDecision:
        """Find completed downstream nodes whose inputs have since changed."""
        decision = ReplanDecision(trigger_node=trigger_node)

        # Staleness is judged by observed versions, not by graph position. A
        # node that read a key which has since been rewritten is stale even if
        # it sits on a parallel branch -- that case is precisely what a linear
        # pipeline gets wrong, because it never revisits a completed stage.
        candidates = set(graph.node_ids()) - {trigger_node}

        for node_id in sorted(candidates):
            node = graph.get(node_id)
            if not node.replan_sensitive:
                continue
            node_state = state.nodes.get(node_id)
            if node_state is None or node_state.status is not NodeStatus.SUCCEEDED:
                continue
            drift = context.stale_keys(node_id)
            if not drift:
                continue
            if node_state.replan_count >= self.max_replans_per_node:
                decision.exceeded_budget.append(node_id)
                continue
            decision.invalidated.append(node_id)
            decision.drift[node_id] = drift

        # Invalidating a node invalidates everything built on top of it, even
        # nodes that read nothing directly -- their inputs are about to change.
        cascaded: Set[str] = set(decision.invalidated)
        for node_id in list(decision.invalidated):
            for descendant in graph.descendants(node_id):
                node_state = state.nodes.get(descendant)
                if node_state is None or node_state.status is not NodeStatus.SUCCEEDED:
                    continue
                if not graph.get(descendant).replan_sensitive:
                    continue
                if node_state.replan_count >= self.max_replans_per_node:
                    if descendant not in decision.exceeded_budget:
                        decision.exceeded_budget.append(descendant)
                    continue
                cascaded.add(descendant)

        decision.invalidated = sorted(cascaded)
        if decision.invalidated:
            keys = sorted({k for d in decision.drift.values() for k in d})
            decision.reason = "context drift on %s after %s completed" % (
                ", ".join(keys) or "upstream outputs", trigger_node,
            )
        elif decision.exceeded_budget:
            decision.reason = "re-plan budget exhausted for %s" % ", ".join(decision.exceeded_budget)
        return decision


def summarise_replans(state: RunState) -> List[Dict[str, Any]]:
    """Flatten re-plan history for reporting."""
    return [
        {
            "node_id": event["node_id"],
            "trigger": event.get("trigger"),
            "drifted_keys": sorted((event.get("drift") or {}).keys()),
        }
        for event in state.replan_events
    ]
