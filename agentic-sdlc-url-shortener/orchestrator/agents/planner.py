"""Task decomposition with runtime graph expansion.

The planner does two things. It writes a task plan -- ordered work items with
explicit dependencies and a critical path -- and, when given a node factory, it
**injects those tasks into the running workflow graph** as real executable
nodes.

That second part is what separates decomposition from documentation. A plan that
only describes work is a document; a plan that becomes schedulable nodes with
their own gates, retries, and approvals is an execution structure the engine can
actually reason about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..types import AgentResult, Severity
from .base import Agent, AgentContext


@dataclass
class Task:
    id: str
    title: str
    kind: str                                   # implement | test | document | verify
    requirement_ids: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)
    estimate: str = "S"                         # S | M | L
    risk: str = "low"                           # low | medium | high
    rationale: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "kind": self.kind,
            "requirement_ids": self.requirement_ids, "depends_on": self.depends_on,
            "estimate": self.estimate, "risk": self.risk, "rationale": self.rationale,
        }


NodeFactory = Callable[[Task], Optional[Any]]


class PlannerAgent(Agent):
    name = "planner"
    description = "Decomposes requirements and design into sequenced, dependency-aware tasks."
    reads = ("requirements", "components", "impact_analysis", "design")
    writes = ("task_plan", "task_sequence", "critical_path")

    def __init__(self, node_factory: Optional[NodeFactory] = None,
                 inject: bool = True, barrier: Optional[str] = None) -> None:
        self.node_factory = node_factory
        self.inject = inject
        # When set, the named downstream node gains a dependency on every
        # injected node, turning the expansion into a real synchronisation
        # barrier instead of a branch that runs beside the main flow.
        self.barrier = barrier

    def execute(self, ctx: AgentContext) -> AgentResult:
        requirements: List[Dict[str, Any]] = ctx.read("requirements", []) or []
        components: List[Dict[str, Any]] = ctx.read("components", []) or []
        impact: Dict[str, Any] = ctx.read("impact_analysis", {}) or {}
        if not requirements:
            return self.failed("cannot decompose without normalised requirements")

        tasks = self._decompose(requirements, components, impact)
        levels = _levels(tasks)
        cycle = _find_cycle(tasks)
        if cycle:
            # A plan the engine cannot schedule is worse than no plan; fail here
            # rather than inject an unschedulable subgraph.
            return self.failed("task plan contains a dependency cycle: %s" % " -> ".join(cycle))

        findings: List[Any] = []
        for task in tasks:
            if task.risk == "high":
                findings.append(self.finding(
                    Severity.MEDIUM, "high-risk task %s: %s" % (task.id, task.title),
                    category="planning",
                    remediation="stage behind a checkpoint and keep it independently revertible"))

        proposed: List[Any] = []
        if self.inject and self.node_factory is not None:
            for task in tasks:
                node = self.node_factory(task)
                if node is not None:
                    proposed.append(node)
        rewire = ({self.barrier: [n.id for n in proposed]}
                  if self.barrier and proposed else {})

        document = _render_plan(tasks, levels, impact)

        result = self.ok(
            outputs={
                "task_plan": [t.to_dict() for t in tasks],
                "task_sequence": levels,
                "critical_path": len(levels),
            },
            artifacts=[self.document("task-plan", "docs/task-plan.md", document)],
            findings=findings,
            metrics={
                "tasks": len(tasks),
                "parallel_levels": len(levels),
                "max_parallel_width": max((len(l) for l in levels), default=0),
                "high_risk_tasks": sum(1 for t in tasks if t.risk == "high"),
                "nodes_injected": len(proposed),
            },
            proposed_nodes=proposed,
            rationale="decomposed into %d task(s) across %d level(s); injected %d node(s)"
                      % (len(tasks), len(levels), len(proposed)),
        )
        result.rewire = rewire
        return result

    # -- decomposition --------------------------------------------------------

    def _decompose(self, requirements: List[Dict[str, Any]],
                   components: List[Dict[str, Any]],
                   impact: Dict[str, Any]) -> List[Task]:
        """Group requirements by owning component, then sequence by dependency.

        Component ownership is the grouping axis because it is the axis along
        which changes actually conflict; grouping by requirement produces tasks
        that all touch the same files.
        """
        owner: Dict[str, str] = {}
        component_deps: Dict[str, List[str]] = {}
        for component in components:
            component_deps[component["name"]] = component.get("depends_on", [])
            for rid in component.get("satisfies", []):
                owner.setdefault(rid, component["name"])

        functional = [r for r in requirements if r.get("kind") != "constraint"]
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for requirement in functional:
            grouped.setdefault(owner.get(requirement["id"], "domain"), []).append(requirement)

        hot_paths = set(impact.get("impacted_modules", []) or [])

        tasks: List[Task] = []
        task_for_component: Dict[str, str] = {}
        for index, (component, reqs) in enumerate(sorted(grouped.items()), start=1):
            task_id = "T%02d" % index
            task_for_component[component] = task_id
            touches_hot_path = any(component in module for module in hot_paths)
            tasks.append(Task(
                id=task_id,
                title="Implement %s behaviour" % component,
                kind="implement",
                requirement_ids=[r["id"] for r in reqs],
                estimate="M" if len(reqs) > 2 else "S",
                risk="high" if touches_hot_path else ("medium" if len(reqs) > 3 else "low"),
                rationale="covers %s" % ", ".join(r["id"] for r in reqs),
            ))

        # Component dependencies become task dependencies: you cannot implement
        # a caller before the interface it calls exists.
        for component, task_id in task_for_component.items():
            task = next(t for t in tasks if t.id == task_id)
            for dependency in component_deps.get(component, []):
                if dependency in task_for_component:
                    task.depends_on.append(task_for_component[dependency])

        implement_ids = [t.id for t in tasks]
        if implement_ids:
            tasks.append(Task(
                id="T%02d" % (len(tasks) + 1), title="Add and run automated tests",
                kind="test", depends_on=list(implement_ids),
                requirement_ids=[rid for t in tasks for rid in t.requirement_ids],
                estimate="M", risk="low",
                rationale="every requirement needs an executable check before release",
            ))
            test_id = tasks[-1].id
            tasks.append(Task(
                id="T%02d" % (len(tasks) + 1), title="Update API documentation and OpenAPI schema",
                kind="document", depends_on=[test_id], estimate="S", risk="low",
                rationale="documentation drift is detected by the docs gate, so it is real work",
            ))
        return tasks


def _levels(tasks: List[Task]) -> List[List[str]]:
    remaining = {t.id: set(t.depends_on) for t in tasks}
    levels: List[List[str]] = []
    while remaining:
        ready = sorted([tid for tid, deps in remaining.items() if not deps])
        if not ready:
            return levels
        levels.append(ready)
        for tid in ready:
            del remaining[tid]
        for deps in remaining.values():
            deps.difference_update(ready)
    return levels


def _find_cycle(tasks: List[Task]) -> Optional[List[str]]:
    graph = {t.id: list(t.depends_on) for t in tasks}
    colour: Dict[str, int] = {tid: 0 for tid in graph}
    stack: List[str] = []

    def visit(tid: str) -> Optional[List[str]]:
        colour[tid] = 1
        stack.append(tid)
        for dep in graph.get(tid, []):
            if dep not in colour:
                continue
            if colour[dep] == 1:
                return stack[stack.index(dep):] + [dep]
            if colour[dep] == 0:
                found = visit(dep)
                if found:
                    return found
        colour[tid] = 2
        stack.pop()
        return None

    for tid in graph:
        if colour[tid] == 0:
            found = visit(tid)
            if found:
                return found
    return None


def _render_plan(tasks: List[Task], levels: List[List[str]],
                 impact: Dict[str, Any]) -> str:
    lines = ["# Task Plan", "",
             "| Task | Kind | Depends on | Requirements | Est | Risk |",
             "| --- | --- | --- | --- | --- | --- |"]
    for task in tasks:
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            task.id, task.kind, ", ".join(task.depends_on) or "-",
            ", ".join(task.requirement_ids) or "-", task.estimate, task.risk,
        ))
    lines += ["", "## Execution order", ""]
    for depth, level in enumerate(levels):
        lines.append("%d. %s%s" % (depth + 1, ", ".join(level),
                                   "  _(parallel)_" if len(level) > 1 else ""))
    if impact.get("impacted_modules"):
        lines += ["", "## Blast radius from impact analysis", ""]
        lines += ["- `%s`" % module for module in impact["impacted_modules"]]
    return "\n".join(lines) + "\n"
