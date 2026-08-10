"""The workflow dependency graph.

A workflow is an explicit DAG, not an ordered list. Two things follow from that
and both matter:

* Nodes with no path between them run **in parallel**; a node with several
  parents is a **synchronisation barrier** that the engine reaches only when
  every parent is satisfied.
* The graph is **mutable at runtime**. A planning agent can inject nodes into a
  live run (``expand``), which is how decomposition produces real executable
  work rather than a document describing work.

Every structural change is re-validated, so an agent cannot introduce a cycle or
a dangling dependency into a running workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set

from .types import (
    AutonomyLevel,
    FailureAction,
    GraphValidationError,
    RetryPolicy,
    Stage,
)


@dataclass
class Node:
    id: str
    stage: Stage
    agent: Any                                  # orchestrator.agents.base.Agent
    description: str = ""
    depends_on: List[str] = field(default_factory=list)

    # Gates -- see orchestrator/gates.py
    entry_gates: List[Any] = field(default_factory=list)
    exit_gates: List[Any] = field(default_factory=list)

    # Governance
    autonomy: AutonomyLevel = AutonomyLevel.ACT_AND_REPORT
    requires_approval: bool = False
    approval_prompt: str = ""
    policy_tags: List[str] = field(default_factory=list)

    # Reliability
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    on_failure: FailureAction = FailureAction.FAIL_RUN
    fallback_agent: Optional[Any] = None
    compensation: Optional[Callable[[Any], None]] = None
    timeout_seconds: Optional[float] = None

    # Re-planning
    # When False, this node is never invalidated by upstream context drift.
    # Useful for pure side-effect nodes that are expensive and idempotent.
    replan_sensitive: bool = True

    # Provenance for dynamically injected nodes.
    injected_by: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.id:
            raise GraphValidationError("node id must be non-empty")
        if self.compensation is not None and self.on_failure is FailureAction.CONTINUE:
            raise GraphValidationError(
                "node %r declares a compensation but never triggers rollback" % self.id
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "stage": self.stage.value,
            "description": self.description,
            "depends_on": list(self.depends_on),
            "agent": getattr(self.agent, "name", type(self.agent).__name__),
            "autonomy": self.autonomy.name,
            "requires_approval": self.requires_approval,
            "on_failure": self.on_failure.value,
            "max_attempts": self.retry.max_attempts,
            "policy_tags": list(self.policy_tags),
            "injected_by": self.injected_by,
        }


class WorkflowGraph:
    def __init__(self, name: str, nodes: Optional[Sequence[Node]] = None,
                 description: str = "") -> None:
        self.name = name
        self.description = description
        self._nodes: Dict[str, Node] = {}
        for node in nodes or []:
            self._nodes[node.id] = node
        self.validate()

    # -- structure ------------------------------------------------------------

    def add(self, node: Node) -> "WorkflowGraph":
        if node.id in self._nodes:
            raise GraphValidationError("duplicate node id %r" % node.id)
        self._nodes[node.id] = node
        self.validate()
        return self

    def expand(self, nodes: Iterable[Node], injected_by: str,
               attach: Optional[Dict[str, List[str]]] = None) -> List[str]:
        """Inject nodes into a live graph, optionally rewiring existing edges.

        ``attach`` maps an existing node id to extra dependencies, which is how
        an injected subgraph becomes a real synchronisation barrier rather than
        a detached branch: the downstream node genuinely waits for the work the
        planner just created.

        Applied atomically -- if the resulting graph would be invalid, nothing
        is added and nothing is rewired. A half-applied expansion would leave
        the run unschedulable.
        """
        snapshot = {nid: list(n.depends_on) for nid, n in self._nodes.items()}
        original_ids = set(self._nodes)
        added: List[Node] = []
        for node in nodes:
            if node.id in self._nodes:
                raise GraphValidationError("cannot inject duplicate node id %r" % node.id)
            node.injected_by = injected_by
            self._nodes[node.id] = node
            added.append(node)
        for target, extra in (attach or {}).items():
            if target not in self._nodes:
                self._restore(snapshot, original_ids)
                raise GraphValidationError("cannot attach to unknown node %r" % target)
            for dep in extra:
                if dep not in self._nodes[target].depends_on:
                    self._nodes[target].depends_on.append(dep)
        try:
            self.validate()
        except GraphValidationError:
            self._restore(snapshot, original_ids)
            raise
        return [n.id for n in added]

    def _restore(self, snapshot: Dict[str, List[str]], original_ids: Set[str]) -> None:
        for nid in list(self._nodes):
            if nid not in original_ids:
                del self._nodes[nid]
        for nid, deps in snapshot.items():
            self._nodes[nid].depends_on = deps

    def __contains__(self, node_id: str) -> bool:
        return node_id in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)

    def __iter__(self):
        return iter(self._nodes.values())

    @property
    def nodes(self) -> Dict[str, Node]:
        return dict(self._nodes)

    def get(self, node_id: str) -> Node:
        try:
            return self._nodes[node_id]
        except KeyError as exc:
            raise GraphValidationError("unknown node %r" % node_id) from exc

    def node_ids(self) -> List[str]:
        return list(self._nodes)

    # -- relationships --------------------------------------------------------

    def dependents(self, node_id: str) -> List[str]:
        return [n.id for n in self._nodes.values() if node_id in n.depends_on]

    def descendants(self, node_id: str) -> Set[str]:
        """Every node transitively downstream of ``node_id``."""
        seen: Set[str] = set()
        frontier = [node_id]
        while frontier:
            current = frontier.pop()
            for child in self.dependents(current):
                if child not in seen:
                    seen.add(child)
                    frontier.append(child)
        return seen

    def ancestors(self, node_id: str) -> Set[str]:
        seen: Set[str] = set()
        frontier = list(self._nodes[node_id].depends_on)
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            frontier.extend(self._nodes[current].depends_on)
        return seen

    def roots(self) -> List[str]:
        return [n.id for n in self._nodes.values() if not n.depends_on]

    def leaves(self) -> List[str]:
        return [n.id for n in self._nodes.values() if not self.dependents(n.id)]

    # -- validation and ordering ---------------------------------------------

    def validate(self) -> None:
        for node in self._nodes.values():
            for dep in node.depends_on:
                if dep not in self._nodes:
                    raise GraphValidationError(
                        "node %r depends on unknown node %r" % (node.id, dep)
                    )
                if dep == node.id:
                    raise GraphValidationError("node %r depends on itself" % node.id)
        cycle = self._find_cycle()
        if cycle:
            raise GraphValidationError("dependency cycle: %s" % " -> ".join(cycle))

    def _find_cycle(self) -> Optional[List[str]]:
        WHITE, GREY, BLACK = 0, 1, 2
        colour = {nid: WHITE for nid in self._nodes}
        stack: List[str] = []

        def visit(nid: str) -> Optional[List[str]]:
            colour[nid] = GREY
            stack.append(nid)
            for dep in self._nodes[nid].depends_on:
                if colour[dep] == GREY:
                    return stack[stack.index(dep):] + [dep]
                if colour[dep] == WHITE:
                    found = visit(dep)
                    if found:
                        return found
            colour[nid] = BLACK
            stack.pop()
            return None

        for nid in self._nodes:
            if colour[nid] == WHITE:
                found = visit(nid)
                if found:
                    return found
        return None

    def topological_levels(self) -> List[List[str]]:
        """Group nodes into levels; every node in a level may run concurrently.

        Used for plan visualisation and for reasoning about the critical path.
        The engine itself schedules on readiness, not on levels, so a fast
        branch is never held back by a slow sibling.
        """
        indegree = {nid: len(node.depends_on) for nid, node in self._nodes.items()}
        levels: List[List[str]] = []
        remaining = dict(indegree)
        while remaining:
            level = sorted([nid for nid, deg in remaining.items() if deg == 0])
            if not level:
                raise GraphValidationError("dependency cycle detected during levelling")
            levels.append(level)
            for nid in level:
                del remaining[nid]
                for child in self.dependents(nid):
                    if child in remaining:
                        remaining[child] -= 1
        return levels

    def critical_path_length(self) -> int:
        return len(self.topological_levels())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "nodes": [n.to_dict() for n in self._nodes.values()],
            "levels": self.topological_levels(),
        }

    def render_ascii(self) -> str:
        """Human-readable plan, one block per parallel level."""
        lines = ["workflow: %s" % self.name]
        if self.description:
            lines.append("  %s" % self.description)
        for depth, level in enumerate(self.topological_levels()):
            marker = "parallel" if len(level) > 1 else "sequential"
            lines.append("  level %d (%s):" % (depth, marker))
            for nid in level:
                node = self._nodes[nid]
                flags = []
                if node.requires_approval:
                    flags.append("approval")
                if node.retry.max_attempts > 1:
                    flags.append("retry x%d" % node.retry.max_attempts)
                if node.compensation is not None:
                    flags.append("compensable")
                if node.injected_by:
                    flags.append("injected by %s" % node.injected_by)
                suffix = (" [%s]" % ", ".join(flags)) if flags else ""
                deps = (" <- %s" % ", ".join(node.depends_on)) if node.depends_on else ""
                lines.append("    - %-26s %-14s%s%s" % (nid, node.stage.value, deps, suffix))
        return "\n".join(lines)
