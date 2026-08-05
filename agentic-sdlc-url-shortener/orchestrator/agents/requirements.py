"""Requirement understanding and ambiguity detection.

Turns a prose requirement into a normalised, testable specification and -- more
importantly -- refuses to normalise what it cannot honestly interpret.

The ambiguity detector is a lexical rule set, not a model. That is a deliberate
trade-off: it is deterministic, explainable, and cannot hallucinate a
requirement that was never written. It will miss semantic ambiguity that only a
domain expert would catch, which is why unresolved questions escalate to a human
rather than being resolved by the agent (see docs/RISKS_AND_TRADEOFFS.md).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..types import AgentResult, Severity
from .base import Agent, AgentContext

# Terms that describe an intent without defining a testable threshold.
VAGUE_TERMS: Dict[str, str] = {
    "fast": "what latency target, at what percentile?",
    "quick": "what latency target, at what percentile?",
    "slow": "measured against what baseline?",
    "scalable": "to what request rate and data volume?",
    "high performance": "what throughput and latency budget?",
    "real-time": "what end-to-end delay is acceptable?",
    "reliable": "what availability target and over what window?",
    "secure": "against which threat model and which controls?",
    "user-friendly": "measured how, by whom?",
    "efficient": "efficient in what resource, against what budget?",
    "robust": "robust against which specific failure modes?",
    "many": "how many, exactly?",
    "several": "how many, exactly?",
    "some": "how many, exactly?",
    "appropriate": "defined by which rule?",
    "as needed": "triggered by what condition?",
    "etc": "what is the complete list?",
    "and so on": "what is the complete list?",
    "large": "what size, in what unit?",
    "small": "what size, in what unit?",
    "soon": "by when, exactly?",
    "later": "by when, exactly?",
    "handle load": "what request rate, what concurrency?",
    "best effort": "what is the acceptable failure rate?",
    "if possible": "is this in scope or not?",
    "properly": "defined by which acceptance criterion?",
    "optimize": "which metric, to what target?",
}

# Non-functional dimensions a service specification should not leave silent.
NFR_DIMENSIONS: Dict[str, Tuple[str, ...]] = {
    "performance": ("latency", "throughput", "rps", "qps", "p95", "p99", "response time"),
    "availability": ("availability", "uptime", "sla", "slo", "downtime", "failover"),
    "security": ("auth", "authn", "authz", "encrypt", "tls", "api key", "permission", "abuse"),
    "scale": ("scale", "volume", "concurrent", "capacity", "growth", "shard", "replica"),
    "data_retention": ("retention", "expiry", "expire", "ttl", "delete", "purge", "archive"),
    "observability": ("metric", "log", "trace", "monitor", "alert", "dashboard"),
}

MODAL_STRENGTH = re.compile(r"(?i)\b(must|shall|should|may|might|could|would be nice)\b")
WEAK_MODALS = {"should", "may", "might", "could", "would be nice"}


@dataclass
class Requirement:
    id: str
    text: str
    kind: str = "functional"          # functional | non_functional | constraint
    priority: str = "must"            # must | should | may
    acceptance: List[str] = field(default_factory=list)
    source: str = "stated"            # stated | derived | assumption

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "text": self.text, "kind": self.kind,
            "priority": self.priority, "acceptance": self.acceptance, "source": self.source,
        }


@dataclass
class OpenQuestion:
    id: str
    question: str
    why: str
    blocking: bool = True
    resolved: bool = False
    answer: Optional[str] = None
    default_assumption: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "question": self.question, "why": self.why,
            "blocking": self.blocking, "resolved": self.resolved,
            "answer": self.answer, "default_assumption": self.default_assumption,
        }


class RequirementsAgent(Agent):
    name = "requirements-analyst"
    description = "Normalises a requirement into testable statements and surfaces ambiguity."
    reads = ("requirement", "clarifications", "constraints")
    writes = ("requirements", "open_questions", "assumptions", "acceptance_criteria",
              "ambiguity_score", "requirement_ids")

    def __init__(self, ambiguity_threshold: float = 0.30,
                 escalate_on_ambiguity: bool = True) -> None:
        self.ambiguity_threshold = ambiguity_threshold
        self.escalate_on_ambiguity = escalate_on_ambiguity

    def execute(self, ctx: AgentContext) -> AgentResult:
        raw = ctx.read("requirement")
        if not raw:
            return self.failed("no 'requirement' provided in run inputs")
        clarifications: Dict[str, str] = ctx.read("clarifications", {}) or {}
        constraints: List[str] = ctx.read("constraints", []) or []

        statements = _split_statements(raw if isinstance(raw, str) else json.dumps(raw))
        requirements = self._normalise(statements, constraints)
        questions = self._detect_ambiguity(statements, raw if isinstance(raw, str) else str(raw))

        # Fold in any answers supplied for this run.
        unresolved: List[OpenQuestion] = []
        for question in questions:
            answer = clarifications.get(question.id) or clarifications.get(question.question)
            if answer:
                question.resolved = True
                question.answer = answer
                requirements.append(Requirement(
                    id="FR-C%d" % (len(requirements) + 1),
                    text="%s -> %s" % (question.question, answer),
                    kind="functional", priority="must", source="derived",
                    acceptance=["behaviour matches the clarified answer: %s" % answer],
                ))
            elif question.blocking:
                unresolved.append(question)

        blocking_total = sum(1 for q in questions if q.blocking)
        score = (len(unresolved) / max(1, len(statements)))
        assumptions = [q.default_assumption for q in unresolved if q.default_assumption]

        document = _render_document(raw, requirements, questions, assumptions)
        artifacts = [self.document("requirements-spec", "docs/requirements.md", document)]

        findings = [
            self.finding(Severity.MEDIUM, "unresolved requirement ambiguity: %s" % q.question,
                         category="requirements", remediation=q.why)
            for q in unresolved
        ]

        outputs = {
            "requirements": [r.to_dict() for r in requirements],
            "open_questions": [q.to_dict() for q in questions],
            "assumptions": assumptions,
            "acceptance_criteria": [a for r in requirements for a in r.acceptance],
            "ambiguity_score": round(score, 3),
            "requirement_ids": [r.id for r in requirements],
        }
        metrics = {
            "statements": len(statements),
            "requirements": len(requirements),
            "questions_total": len(questions),
            "questions_blocking": blocking_total,
            "questions_unresolved": len(unresolved),
            "ambiguity_score": round(score, 3),
        }

        if unresolved and self.escalate_on_ambiguity and score >= self.ambiguity_threshold:
            # Stop rather than guess. Every guess here becomes a silent
            # assumption baked into code that nobody reviews.
            return self.needs_input(
                rationale=(
                    "requirement is ambiguous (score %.2f over threshold %.2f); "
                    "%d blocking question(s) need a human answer"
                    % (score, self.ambiguity_threshold, len(unresolved))
                ),
                questions=["%s: %s (default if unanswered: %s)"
                           % (q.id, q.question, q.default_assumption or "none")
                           for q in unresolved],
                outputs=outputs,
                artifacts=artifacts,
            )

        return self.ok(
            outputs=outputs, artifacts=artifacts, findings=findings, metrics=metrics,
            rationale="normalised %d statement(s) into %d requirement(s); %d question(s) resolved"
                      % (len(statements), len(requirements), len(questions) - len(unresolved)),
        )

    # -- internals ------------------------------------------------------------

    def _normalise(self, statements: List[str], constraints: List[str]) -> List[Requirement]:
        requirements: List[Requirement] = []
        functional = 0
        non_functional = 0
        for statement in statements:
            kind = "non_functional" if _is_non_functional(statement) else "functional"
            if kind == "functional":
                functional += 1
                rid = "FR-%d" % functional
            else:
                non_functional += 1
                rid = "NFR-%d" % non_functional
            requirements.append(Requirement(
                id=rid, text=statement, kind=kind,
                priority=_priority_of(statement),
                acceptance=_acceptance_for(statement),
            ))
        for index, constraint in enumerate(constraints, start=1):
            requirements.append(Requirement(
                id="CON-%d" % index, text=constraint, kind="constraint",
                priority="must", source="stated",
                acceptance=["design does not violate: %s" % constraint],
            ))
        return requirements

    def _detect_ambiguity(self, statements: List[str], full_text: str) -> List[OpenQuestion]:
        questions: List[OpenQuestion] = []
        lowered = full_text.lower()
        seen: set = set()

        for index, statement in enumerate(statements, start=1):
            low = statement.lower()
            for term, why in VAGUE_TERMS.items():
                if _mentions(term, low) and term not in seen:
                    seen.add(term)
                    questions.append(OpenQuestion(
                        # Content-derived, not positional: an id that shifts when
                        # an unrelated sentence is edited cannot be answered in a
                        # stored clarification set.
                        id="Q-TERM-%s" % _slug(term),
                        question="statement %d uses %r without a measurable definition -- %s"
                                 % (index, term, why),
                        why="unmeasurable requirements cannot be validated or gated",
                        blocking=True,
                        default_assumption=_default_for(term),
                    ))
            weak = [m.lower() for m in MODAL_STRENGTH.findall(statement) if m.lower() in WEAK_MODALS]
            if weak and ("weak-modal-%d" % index) not in seen:
                seen.add("weak-modal-%d" % index)
                questions.append(OpenQuestion(
                    id="Q-MODAL-%d" % index,
                    question="statement %d is qualified with %r -- is it in scope for this change?"
                             % (index, weak[0]),
                    why="optional scope silently becomes unowned scope",
                    blocking=False,
                    default_assumption="treated as out of scope for this iteration",
                ))

        for dimension, keywords in NFR_DIMENSIONS.items():
            if not any(keyword in lowered for keyword in keywords):
                questions.append(OpenQuestion(
                    id="Q-NFR-%s" % dimension.upper(),
                    question="no %s requirement stated -- what is the target?" % dimension,
                    why="unstated non-functional requirements surface as production incidents",
                    blocking=False,
                    default_assumption=_NFR_DEFAULTS.get(dimension),
                ))
        return questions


_NFR_DEFAULTS = {
    "performance": "redirect p99 under 50ms at 100 rps on a single node",
    "availability": "single-node service; availability bounded by the host",
    "security": "API-key auth on management endpoints, public read on redirects",
    "scale": "10k links and 100k clicks per day",
    "data_retention": "click aggregates retained indefinitely; no raw event storage",
    "observability": "structured JSON logs plus Prometheus metrics on /metrics",
}

_TERM_DEFAULTS = {
    "fast": "p99 under 50ms",
    "quick": "p99 under 50ms",
    "scalable": "single-node, 100 rps sustained",
    "real-time": "under one second end to end",
    "reliable": "best-effort single node, no HA guarantee",
    "secure": "API-key auth plus SSRF-safe URL validation",
    "many": "up to 100",
    "several": "3 to 5",
    "some": "at least 1",
    "large": "up to 2048 bytes",
    "handle load": "100 requests per second",
}


def _default_for(term: str) -> Optional[str]:
    return _TERM_DEFAULTS.get(term)


def _slug(term: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "-", term.upper()).strip("-")


# Word-boundary matching, cached per term. Substring matching flagged "many"
# inside "Germany" and "some" inside "somewhere", and a detector with obvious
# false positives is one people learn to ignore.
_TERM_PATTERNS = {
    term: re.compile(r"(?<![a-z])%s(?![a-z])" % re.escape(term))
    for term in VAGUE_TERMS
}

# "how many events are buffered" describes a question the system answers; it is
# not an undefined quantity in the requirement. Only the interrogative reading
# is excluded -- "many events" is still flagged.
_INTERROGATIVE = re.compile(r"\bhow\s+$")


def _mentions(term: str, text: str) -> bool:
    for match in _TERM_PATTERNS[term].finditer(text):
        if _INTERROGATIVE.search(text[: match.start()]):
            continue
        return True
    return False


def _split_statements(text: str) -> List[str]:
    """Split a requirement blob into individual statements.

    Handles bulleted lists and prose. Numbered/bulleted lines are preferred when
    present because they usually reflect the author's own decomposition.
    """
    lines = [l.strip() for l in text.splitlines()]
    bullets = [
        re.sub(r"^(?:[-*•]|\d+[.)])\s*", "", l)
        for l in lines
        if re.match(r"^(?:[-*•]|\d+[.)])\s+", l)
    ]
    if len(bullets) >= 2:
        return [b for b in bullets if b]
    sentences = re.split(r"(?<=[.;])\s+", " ".join(l for l in lines if l))
    return [s.strip(" .;") for s in sentences if len(s.strip(" .;")) > 3]


def _is_non_functional(statement: str) -> bool:
    low = statement.lower()
    return any(k in low for group in NFR_DIMENSIONS.values() for k in group)


def _priority_of(statement: str) -> str:
    low = statement.lower()
    if re.search(r"\b(must|shall|required)\b", low):
        return "must"
    if re.search(r"\b(should|recommended)\b", low):
        return "should"
    if re.search(r"\b(may|optional|nice to have)\b", low):
        return "may"
    return "must"


def _acceptance_for(statement: str) -> List[str]:
    """Derive a testable acceptance criterion.

    Mechanical by design: the value is forcing every requirement to carry a
    check, not producing elegant prose.
    """
    low = statement.lower()
    criteria: List[str] = []
    if "redirect" in low:
        criteria.append("GET /{code} returns a 3xx with the stored target in Location")
    if "expire" in low or "ttl" in low:
        criteria.append("a link past its expiry resolves to 410, not 404 or 3xx")
    if "analytics" in low or "click" in low:
        criteria.append("click counts increment on resolution and are queryable per link")
    if "custom" in low and ("alias" in low or "code" in low):
        criteria.append("a taken alias returns 409 and does not overwrite the existing link")
    if "rate" in low and "limit" in low:
        criteria.append("requests beyond the configured rate return 429 with Retry-After")
    if "auth" in low or "api key" in low:
        criteria.append("management endpoints reject requests without a valid API key (401)")
    if not criteria:
        criteria.append("automated test asserts the stated behaviour end to end")
    return criteria


def _render_document(raw: Any, requirements: List[Requirement],
                     questions: List[OpenQuestion], assumptions: List[str]) -> str:
    lines = ["# Requirements Specification", "",
             "Generated by the requirements agent. Every entry below is either stated "
             "in the source requirement, derived from it, or an explicitly flagged assumption.",
             "", "## Source", "", "```", str(raw).strip(), "```", "", "## Normalised requirements", ""]
    lines.append("| ID | Kind | Priority | Requirement |")
    lines.append("| --- | --- | --- | --- |")
    for requirement in requirements:
        lines.append("| %s | %s | %s | %s |" % (
            requirement.id, requirement.kind, requirement.priority,
            requirement.text.replace("|", "\\|"),
        ))
    lines += ["", "## Acceptance criteria", ""]
    for requirement in requirements:
        for criterion in requirement.acceptance:
            lines.append("- **%s**: %s" % (requirement.id, criterion))
    lines += ["", "## Open questions", ""]
    if questions:
        for question in questions:
            status = "RESOLVED" if question.resolved else ("BLOCKING" if question.blocking else "open")
            lines.append("- `%s` **[%s]** %s" % (question.id, status, question.question))
            lines.append("  - why it matters: %s" % question.why)
            if question.answer:
                lines.append("  - answer: %s" % question.answer)
            elif question.default_assumption:
                lines.append("  - default if unanswered: %s" % question.default_assumption)
    else:
        lines.append("None.")
    lines += ["", "## Assumptions carried forward", ""]
    lines += ["- %s" % a for a in assumptions] or ["None."]
    return "\n".join(lines) + "\n"
