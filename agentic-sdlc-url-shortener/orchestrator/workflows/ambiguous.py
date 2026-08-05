"""Scenario 3 -- Ambiguous: a requirement that cannot responsibly be built.

Requirement, as received: *"Make the shortener better for our marketing team --
they need to see how links are doing and it should be fast. Add campaign support
and make sure it scales."*

What this scenario is meant to demonstrate:

* **refusing to guess** -- the requirements agent returns ``NEEDS_INPUT`` rather
  than inventing thresholds, and the run stops at a human checkpoint. Run it
  without clarifications and it will not proceed;
* **non-linear, stateful re-planning** -- a stakeholder review runs *in parallel*
  with architecture and finishes later. When it rewrites the requirements, the
  already-completed architecture and decomposition nodes are detected as stale
  through their recorded read versions and are re-executed. A linear pipeline
  cannot notice this, because it never revisits a finished stage;
* **controlled autonomy** -- the correct output for an ambiguous requirement is
  a scoped plan awaiting sign-off, not code. Every node here is PROPOSE_ONLY,
  and the policy engine denies any of them that produces a mutation.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..agents import ArchitectAgent, PlannerAgent
from ..agents.base import Agent, AgentContext
from ..agents.requirements import RequirementsAgent
from ..gates import (
    AgentSucceeded,
    ContextKeysPresent,
    MetricThreshold,
    NoOpenQuestions,
    ProducedOutputs,
)
from ..graph import Node, WorkflowGraph
from ..types import AgentResult, AutonomyLevel, FailureAction, RetryPolicy, Severity, Stage

REQUIREMENT = """
Make the shortener better for our marketing team.
They need to see how links are doing and it should be fast.
Add support for campaigns and make sure it scales.
We should probably handle load better too.
"""

CONSTRAINTS = ["no third-party runtime dependencies", "must not break existing short links"]

# The answers a product owner gives once the questions are put to them. Passing
# these in is what unblocks the run -- the point of the scenario is that the
# system will not manufacture them.
CLARIFICATIONS = {
    "Q-TERM-FAST": "'fast' means link statistics render in under 300 ms p95 for a 90-day window",
    "Q-TERM-SCALABLE": "'scalable' means 1M links and 50M clicks per year on a single node",
    "Q-TERM-HANDLE-LOAD": "'handle load' means 500 redirects per second sustained",
    "Q-NFR-PERFORMANCE": "redirect p99 under 50 ms; statistics p95 under 300 ms",
    "Q-NFR-AVAILABILITY": "99.5% monthly availability, single region",
    "Q-NFR-SECURITY": "campaign data is tenant-scoped; API keys already carry tenant identity",
    "Q-NFR-SCALE": "1M links, 50M clicks per year",
    "Q-NFR-DATA_RETENTION": "per-day click aggregates retained 24 months, then rolled up to monthly",
    "Q-NFR-OBSERVABILITY": "campaign dashboards read the existing /metrics endpoint plus the stats API",
}

# The requirement the stakeholder review discovers is missing. Adding it is what
# invalidates the already-completed architecture and decomposition.
DERIVED_REQUIREMENT = {
    "id": "FR-SR1",
    "text": ("Campaign attribution must survive link deletion: retiring a link must not "
             "remove its historical click attribution from campaign reporting."),
    "kind": "functional",
    "priority": "must",
    "source": "derived",
    "acceptance": [
        "deleting a link leaves its campaign aggregates queryable",
        "a campaign report over a deleted link's lifetime returns the same totals as before deletion",
    ],
}


class StakeholderReviewAgent(Agent):
    """A slower, parallel review that can change the requirements underneath.

    Deliberately idempotent: it adds the derived requirement only if it is not
    already present. Without that, every re-plan would rewrite the requirements
    and the run would never converge -- which is exactly why the agent contract
    requires idempotence.
    """

    name = "stakeholder-reviewer"
    description = "Reviews normalised requirements with stakeholders and files gaps."
    reads = ("requirements", "open_questions", "assumptions")
    writes = ("requirements", "stakeholder_review")

    def __init__(self, delay: float = 0.35) -> None:
        self.delay = delay

    def execute(self, ctx: AgentContext) -> AgentResult:
        requirements: List[Dict[str, Any]] = list(ctx.read("requirements", []) or [])
        questions = ctx.read("open_questions", []) or []
        assumptions = ctx.read("assumptions", []) or []

        # Stands in for the round-trip to a human stakeholder. It is what makes
        # this branch finish after architecture, which is the whole point.
        time.sleep(self.delay)

        already_present = any(r.get("id") == DERIVED_REQUIREMENT["id"] for r in requirements)
        if not already_present:
            requirements.append(dict(DERIVED_REQUIREMENT))

        review = {
            "reviewed_requirements": len(requirements),
            "questions_reviewed": len(questions),
            "assumptions_challenged": assumptions,
            "gap_identified": DERIVED_REQUIREMENT["id"],
            "already_present": already_present,
            "verdict": "requirements amended" if not already_present else "no further change",
        }

        findings = []
        if not already_present:
            findings.append(self.finding(
                Severity.MEDIUM,
                "stakeholder review added %s after design had already completed"
                % DERIVED_REQUIREMENT["id"],
                category="requirements",
                remediation="downstream design and decomposition must be re-derived"))

        return self.ok(
            outputs={"requirements": requirements, "stakeholder_review": review},
            artifacts=[self.document("stakeholder-review", "docs/stakeholder-review.md",
                                     _render_review(review, requirements))],
            findings=findings,
            metrics={"requirements_after_review": len(requirements),
                     "amended": 0 if already_present else 1},
            rationale=review["verdict"],
        )


class ScopeProposalAgent(Agent):
    """Produces the deliverable an ambiguous requirement actually warrants.

    Not code -- a scoped proposal with phases, explicit non-goals, and the
    assumptions that would have been silently baked in had the run guessed.
    """

    name = "scope-proposer"
    description = "Drafts a phased scope proposal for human sign-off."
    reads = ("requirements", "task_plan", "open_questions", "assumptions",
             "stakeholder_review", "design")
    writes = ("scope_proposal",)

    def execute(self, ctx: AgentContext) -> AgentResult:
        requirements = ctx.read("requirements", []) or []
        tasks = ctx.read("task_plan", []) or []
        questions = ctx.read("open_questions", []) or []
        review = ctx.read("stakeholder_review", {}) or {}

        unresolved = [q for q in questions if q.get("blocking") and not q.get("resolved")]
        resolved = [q for q in questions if q.get("resolved")]

        proposal = {
            "phases": [
                {"phase": 1, "goal": "Campaign tagging and attribution on link creation",
                 "requirements": [r["id"] for r in requirements
                                  if "campaign" in r["text"].lower()][:5]},
                {"phase": 2, "goal": "Marketing statistics view within the stated latency budget",
                 "requirements": [r["id"] for r in requirements
                                  if any(w in r["text"].lower()
                                         for w in ("stat", "see", "report", "fast"))][:5]},
                {"phase": 3, "goal": "Capacity work to meet the stated scale target",
                 "requirements": [r["id"] for r in requirements
                                  if any(w in r["text"].lower()
                                         for w in ("scale", "load", "capacity"))][:5]},
            ],
            "non_goals": [
                "no multi-region deployment in this scope",
                "no changes to existing short-code semantics",
                "no bulk import or export",
            ],
            "requirements_total": len(requirements),
            "questions_resolved": len(resolved),
            "questions_outstanding": len(unresolved),
            "stakeholder_verdict": review.get("verdict"),
            "tasks": len(tasks),
        }

        return self.ok(
            outputs={"scope_proposal": proposal},
            artifacts=[self.document("scope-proposal", "docs/scope-proposal.md",
                                     _render_proposal(proposal, requirements, questions))],
            metrics={"phases": len(proposal["phases"]),
                     "questions_outstanding": len(unresolved)},
            rationale="drafted a %d-phase proposal from %d requirement(s); %d question(s) outstanding"
                      % (len(proposal["phases"]), len(requirements), len(unresolved)),
        )


def build(inputs: Optional[Dict[str, Any]] = None) -> WorkflowGraph:
    nodes: List[Node] = [
        Node(
            id="requirements", stage=Stage.REQUIREMENTS,
            # A low threshold on purpose: this requirement should not slip past.
            agent=RequirementsAgent(ambiguity_threshold=0.15, escalate_on_ambiguity=True),
            description="Normalise the request and refuse to proceed while it is ambiguous.",
            entry_gates=[ContextKeysPresent("requirement")],
            exit_gates=[AgentSucceeded(), ProducedOutputs("requirements", "open_questions")],
            autonomy=AutonomyLevel.PROPOSE_ONLY,
            # Ambiguity is not transient; retrying produces the same questions.
            retry=RetryPolicy(max_attempts=1),
            on_failure=FailureAction.SAFE_STOP,
        ),
        Node(
            id="architecture", stage=Stage.ARCHITECTURE, agent=ArchitectAgent(),
            description="Derive a component model. Re-runs if the requirements change.",
            depends_on=["requirements"],
            entry_gates=[NoOpenQuestions()],
            exit_gates=[AgentSucceeded(), ProducedOutputs("design", "components")],
            autonomy=AutonomyLevel.PROPOSE_ONLY,
        ),
        Node(
            id="stakeholder-review", stage=Stage.GOVERNANCE,
            agent=StakeholderReviewAgent(),
            description="Parallel review that may amend the requirements after design completes.",
            depends_on=["requirements"],
            exit_gates=[AgentSucceeded(), ProducedOutputs("stakeholder_review")],
            autonomy=AutonomyLevel.PROPOSE_ONLY,
        ),
        Node(
            id="decompose", stage=Stage.PLANNING, agent=PlannerAgent(inject=False),
            description="Sequence the work. Re-runs whenever upstream inputs change.",
            depends_on=["architecture"],
            exit_gates=[AgentSucceeded(), ProducedOutputs("task_plan")],
            autonomy=AutonomyLevel.PROPOSE_ONLY,
        ),
        Node(
            id="scope-proposal", stage=Stage.GOVERNANCE, agent=ScopeProposalAgent(),
            description="Draft the phased proposal that goes to a human for sign-off.",
            depends_on=["decompose", "stakeholder-review"],
            entry_gates=[ContextKeysPresent("task_plan", "stakeholder_review")],
            exit_gates=[AgentSucceeded(), ProducedOutputs("scope_proposal"),
                        MetricThreshold("questions_outstanding", maximum=0)],
            requires_approval=True,
            approval_prompt="accept the phased scope proposal and commit to build it",
            autonomy=AutonomyLevel.PROPOSE_ONLY,
            on_failure=FailureAction.SAFE_STOP,
            policy_tags=["governance"],
        ),
    ]
    return WorkflowGraph(
        name="ambiguous",
        description="Turn a vague request into a scoped, signed-off proposal without guessing.",
        nodes=nodes,
    )


def default_inputs(with_clarifications: bool = True) -> Dict[str, Any]:
    inputs: Dict[str, Any] = {
        "requirement": REQUIREMENT.strip(),
        "constraints": list(CONSTRAINTS),
    }
    if with_clarifications:
        inputs["clarifications"] = dict(CLARIFICATIONS)
    return inputs


def _render_review(review: Dict[str, Any], requirements: List[Dict[str, Any]]) -> str:
    lines = ["# Stakeholder Review", "",
             "- requirements reviewed: %d" % review["reviewed_requirements"],
             "- verdict: **%s**" % review["verdict"], ""]
    if not review["already_present"]:
        lines += ["## Gap identified", "",
                  "`%s` -- %s" % (DERIVED_REQUIREMENT["id"], DERIVED_REQUIREMENT["text"]), "",
                  "This was filed *after* the architecture stage had already completed. ",
                  "The orchestrator detects that the design read an earlier version of the ",
                  "requirements and re-executes the affected stages rather than shipping a ",
                  "design that no longer matches its inputs.", ""]
    if review.get("assumptions_challenged"):
        lines += ["## Assumptions challenged", ""]
        lines += ["- %s" % a for a in review["assumptions_challenged"]]
    return "\n".join(lines) + "\n"


def _render_proposal(proposal: Dict[str, Any], requirements: List[Dict[str, Any]],
                     questions: List[Dict[str, Any]]) -> str:
    lines = ["# Scope Proposal", "",
             "The original request was not buildable as written. It has been normalised into "
             "%d requirement(s); %d clarification(s) were answered by a human before any design "
             "work was accepted." % (proposal["requirements_total"], proposal["questions_resolved"]),
             "", "## Phases", ""]
    for phase in proposal["phases"]:
        lines.append("### Phase %d -- %s" % (phase["phase"], phase["goal"]))
        lines.append("")
        lines.append("Requirements: %s" % (", ".join("`%s`" % r for r in phase["requirements"])
                                           or "_to be assigned_"))
        lines.append("")
    lines += ["## Explicit non-goals", ""] + ["- %s" % n for n in proposal["non_goals"]]
    lines += ["", "## Clarifications that unblocked this proposal", ""]
    for question in questions:
        if question.get("resolved"):
            lines.append("- **%s** %s" % (question["id"], question.get("answer")))
    outstanding = [q for q in questions if q.get("blocking") and not q.get("resolved")]
    if outstanding:
        lines += ["", "## Still outstanding", ""]
        lines += ["- %s" % q["question"] for q in outstanding]
    return "\n".join(lines) + "\n"
