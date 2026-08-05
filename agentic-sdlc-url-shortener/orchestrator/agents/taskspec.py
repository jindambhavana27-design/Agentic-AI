"""Per-task specification agent.

Instantiated by the planner for each decomposed task and injected into the
running graph. Its job is narrow and useful: turn one task into a written
specification with explicit acceptance criteria, and **fail** if the task cannot
be traced back to a requirement.

That failure matters. An implementation task with no requirement behind it is
either scope creep or a dropped requirement, and both are cheaper to catch here
than in review.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..types import AgentResult, Severity
from .base import Agent, AgentContext


class TaskSpecAgent(Agent):
    name = "task-specifier"
    description = "Writes an implementation specification for a single planned task."

    def __init__(self, task: Any) -> None:
        self.task = task
        self.name = "task-specifier[%s]" % task.id
        self.reads = ("requirements", "components", "acceptance_criteria")
        self.writes = ("task_spec_%s" % task.id,)

    def execute(self, ctx: AgentContext) -> AgentResult:
        requirements: List[Dict[str, Any]] = ctx.read("requirements", []) or []
        by_id = {r["id"]: r for r in requirements}

        linked = [by_id[rid] for rid in self.task.requirement_ids if rid in by_id]
        orphans = [rid for rid in self.task.requirement_ids if rid not in by_id]

        if self.task.kind == "implement" and not linked:
            return self.failed(
                "task %s implements no traceable requirement" % self.task.id,
                findings=[self.finding(
                    Severity.HIGH,
                    "task %s has no requirement behind it" % self.task.id,
                    category="traceability",
                    remediation="link it to a requirement or drop it from the plan")],
            )

        criteria = [c for r in linked for c in r.get("acceptance", [])] or [
            "the change is covered by at least one automated test"
        ]
        spec = _render_spec(self.task, linked, criteria, orphans)

        findings = [
            self.finding(Severity.MEDIUM,
                         "task %s references unknown requirement %s" % (self.task.id, rid),
                         category="traceability")
            for rid in orphans
        ]

        return self.ok(
            outputs={
                "task_spec_%s" % self.task.id: {
                    "task": self.task.to_dict(),
                    "requirements": [r["id"] for r in linked],
                    "acceptance": criteria,
                }
            },
            artifacts=[self.document("spec-%s" % self.task.id,
                                     "docs/specs/%s.md" % self.task.id, spec)],
            findings=findings,
            metrics={"requirements_linked": len(linked), "acceptance_criteria": len(criteria)},
            rationale="specified %s against %d requirement(s)" % (self.task.id, len(linked)),
        )


def _render_spec(task: Any, linked: List[Dict[str, Any]], criteria: List[str],
                 orphans: List[str]) -> str:
    lines = ["# %s -- %s" % (task.id, task.title), "",
             "- kind: `%s`" % task.kind,
             "- estimate: %s, risk: %s" % (task.estimate, task.risk),
             "- depends on: %s" % (", ".join(task.depends_on) or "nothing"),
             "", "## Requirements covered", ""]
    lines += ["- **%s** %s" % (r["id"], r["text"]) for r in linked] or ["- _none_"]
    lines += ["", "## Acceptance criteria", ""] + ["- %s" % c for c in criteria]
    if orphans:
        lines += ["", "## Unresolved references", ""] + ["- `%s`" % o for o in orphans]
    lines += ["", "## Rationale", "", task.rationale or "_not recorded_"]
    return "\n".join(lines) + "\n"
