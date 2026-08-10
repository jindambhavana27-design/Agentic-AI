"""Policy guardrails: security, compliance, and change control.

Policies are evaluated by the engine at two points -- before a node runs
(pre-execution) and after it produces a result (post-execution). They are
separate from gates on purpose:

* A **gate** asks "is this work correct?" and is owned by the workflow author.
* A **policy** asks "is this work *allowed*?" and is owned by the organisation.

Policies therefore apply across every workflow, cannot be waived by a workflow
author, and their strongest verdict wins. A DENY from any policy stops the node
regardless of how the agent or the gates feel about it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .context import RunContext
from .types import AgentResult, AutonomyLevel, PolicyEffect, Severity, severity_at_least

# Ordered weakest to strongest; used to combine verdicts.
_EFFECT_RANK = {
    PolicyEffect.ALLOW: 0,
    PolicyEffect.WARN: 1,
    PolicyEffect.REQUIRE_APPROVAL: 2,
    PolicyEffect.DENY: 3,
}


@dataclass
class PolicyDecision:
    effect: PolicyEffect
    policy_id: str
    reason: str
    category: str = "general"
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "effect": self.effect.value,
            "category": self.category,
            "reason": self.reason,
            "details": self.details,
        }


@dataclass
class PolicyVerdict:
    """Combined result of every policy that fired."""

    effect: PolicyEffect
    decisions: List[PolicyDecision] = field(default_factory=list)

    @property
    def denied(self) -> bool:
        return self.effect is PolicyEffect.DENY

    @property
    def needs_approval(self) -> bool:
        return self.effect is PolicyEffect.REQUIRE_APPROVAL

    def blocking_reasons(self) -> List[str]:
        return [
            d.reason for d in self.decisions
            if d.effect in (PolicyEffect.DENY, PolicyEffect.REQUIRE_APPROVAL)
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {"effect": self.effect.value, "decisions": [d.to_dict() for d in self.decisions]}


class Policy:
    """Base policy. Implement ``pre`` and/or ``post``."""

    id = "policy"
    category = "general"
    description = ""

    def pre(self, node: Any, context: RunContext) -> Optional[PolicyDecision]:
        return None

    def post(self, node: Any, context: RunContext, result: AgentResult) -> Optional[PolicyDecision]:
        return None

    def _decide(self, effect: PolicyEffect, reason: str, **details) -> PolicyDecision:
        return PolicyDecision(effect, self.id, reason, self.category, details)


# -- security -----------------------------------------------------------------

# Patterns for credentials that must never be committed. Deliberately narrow:
# a noisy secret scanner gets switched off, which is worse than a narrow one.
_SECRET_PATTERNS = [
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("generic_assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|passwd|password|token)\b\s*[=:]\s*['\"][^'\"\s]{12,}['\"]"
    )),
]


class NoSecretsInArtifacts(Policy):
    """Refuse to let credential-shaped strings enter generated artifacts."""

    id = "SEC-001"
    category = "security"
    description = "Generated code and documents must not contain credentials."

    def post(self, node, context, result) -> Optional[PolicyDecision]:
        hits: List[str] = []
        for artifact in result.artifacts:
            content = artifact.content or ""
            for label, pattern in _SECRET_PATTERNS:
                if pattern.search(content):
                    hits.append("%s in %s" % (label, artifact.name))
        if hits:
            return self._decide(
                PolicyEffect.DENY,
                "credential-shaped content detected in generated artifacts",
                matches=hits,
            )
        return None


class NoCriticalSecurityFindings(Policy):
    """Critical security findings stop the run; high findings need a human."""

    id = "SEC-002"
    category = "security"
    description = "Critical security findings block; high findings require sign-off."

    def post(self, node, context, result) -> Optional[PolicyDecision]:
        security = [f for f in result.findings if f.category in ("security", "vulnerability")]
        critical = [f for f in security if f.severity is Severity.CRITICAL]
        if critical:
            return self._decide(
                PolicyEffect.DENY,
                "%d critical security finding(s)" % len(critical),
                findings=[f.message for f in critical],
            )
        high = [f for f in security if severity_at_least(f.severity, Severity.HIGH)]
        if high:
            return self._decide(
                PolicyEffect.REQUIRE_APPROVAL,
                "%d high-severity security finding(s) need sign-off" % len(high),
                findings=[f.message for f in high],
            )
        return None


class WorkspaceConfinement(Policy):
    """Agents may only write inside the run workspace.

    Path traversal in an agent-produced path is the difference between a bad
    patch and a compromised machine.
    """

    id = "SEC-003"
    category = "security"
    description = "Artifact paths must stay inside the declared workspace."

    def __init__(self, workspace_key: str = "workspace_root") -> None:
        self.workspace_key = workspace_key

    def post(self, node, context, result) -> Optional[PolicyDecision]:
        import os

        root = context.get(self.workspace_key)
        if not root:
            return None
        root_abs = os.path.realpath(root)
        escapes = []
        for artifact in result.artifacts:
            if not artifact.path:
                continue
            target = os.path.realpath(os.path.join(root_abs, artifact.path))
            if not (target == root_abs or target.startswith(root_abs + os.sep)):
                escapes.append(artifact.path)
        if escapes:
            return self._decide(
                PolicyEffect.DENY, "artifact paths escape the workspace", paths=escapes
            )
        return None


# -- change control -----------------------------------------------------------

class ChangeBudget(Policy):
    """Large diffs are not reviewable; force a human decision past a threshold."""

    id = "CHG-001"
    category = "change_control"
    description = "Changes beyond the review budget require explicit approval."

    def __init__(self, max_files: int = 12, max_lines: int = 400) -> None:
        self.max_files = max_files
        self.max_lines = max_lines

    def post(self, node, context, result) -> Optional[PolicyDecision]:
        files = result.metrics.get("files_changed")
        lines = result.metrics.get("lines_changed")
        if files is None and lines is None:
            return None
        if (files or 0) > self.max_files or (lines or 0) > self.max_lines:
            return self._decide(
                PolicyEffect.REQUIRE_APPROVAL,
                "change exceeds the review budget (%s files / %s lines)" % (files, lines),
                files_changed=files, lines_changed=lines,
                max_files=self.max_files, max_lines=self.max_lines,
            )
        return None


class NoNewDependencies(Policy):
    """New third-party dependencies are a supply-chain decision, not a code one."""

    id = "CHG-002"
    category = "change_control"
    description = "Adding a runtime dependency requires human sign-off."

    def post(self, node, context, result) -> Optional[PolicyDecision]:
        added = result.metrics.get("dependencies_added") or []
        if added:
            return self._decide(
                PolicyEffect.REQUIRE_APPROVAL,
                "new runtime dependencies introduced: %s" % ", ".join(added),
                dependencies=list(added),
            )
        return None


class DestructiveMigrationControl(Policy):
    """Schema changes that can lose data need a reviewed rollback path."""

    id = "CHG-003"
    category = "change_control"
    description = "Destructive migrations require an approved rollback script."

    _DESTRUCTIVE = re.compile(r"(?i)\b(drop\s+(table|column)|truncate|delete\s+from)\b")

    def post(self, node, context, result) -> Optional[PolicyDecision]:
        for artifact in result.artifacts:
            if artifact.kind != "schema":
                continue
            if self._DESTRUCTIVE.search(artifact.content or ""):
                if not result.outputs.get("rollback_script"):
                    return self._decide(
                        PolicyEffect.DENY,
                        "destructive migration in %s has no rollback script" % artifact.name,
                        artifact=artifact.name,
                    )
                return self._decide(
                    PolicyEffect.REQUIRE_APPROVAL,
                    "destructive migration in %s requires sign-off" % artifact.name,
                    artifact=artifact.name,
                )
        return None


# -- compliance ---------------------------------------------------------------

class AutonomyBoundary(Policy):
    """Enforce the node's declared autonomy level.

    A PROPOSE_ONLY node that produced mutations has exceeded its mandate; that
    is a control failure, not a code-quality issue, so it is denied.
    """

    id = "GOV-001"
    category = "compliance"
    description = "Agents may not act beyond their declared autonomy level."

    def pre(self, node, context) -> Optional[PolicyDecision]:
        if node.autonomy is AutonomyLevel.ACT_WITH_APPROVAL and not node.requires_approval:
            return self._decide(
                PolicyEffect.REQUIRE_APPROVAL,
                "node %r is ACT_WITH_APPROVAL but declares no approval checkpoint" % node.id,
            )
        return None

    def post(self, node, context, result) -> Optional[PolicyDecision]:
        if node.autonomy is AutonomyLevel.PROPOSE_ONLY:
            mutations = [a for a in result.artifacts if a.kind in ("code", "schema")]
            if mutations or result.metrics.get("files_changed"):
                return self._decide(
                    PolicyEffect.DENY,
                    "node %r is PROPOSE_ONLY but produced mutations" % node.id,
                    artifacts=[a.name for a in mutations],
                )
        return None


class ReleaseRequiresEvidence(Policy):
    """A release cannot be approved on assertion; it needs recorded evidence."""

    id = "GOV-002"
    category = "compliance"
    description = "Release stages require test, security, and documentation evidence."

    REQUIRED = ("test_report", "security_report", "documentation_report")

    def pre(self, node, context) -> Optional[PolicyDecision]:
        from .types import Stage

        if node.stage is not Stage.RELEASE:
            return None
        missing = [k for k in self.REQUIRED if not context.has(k)]
        if missing:
            return self._decide(
                PolicyEffect.DENY,
                "release evidence missing: %s" % ", ".join(missing),
                missing=missing,
            )
        return None


class TraceabilityRequired(Policy):
    """Every artifact must trace to a requirement.

    Untraceable output is how scope creep and unreviewed behaviour enter a
    codebase; it is cheap to require the link and expensive to reconstruct it.
    """

    id = "GOV-003"
    category = "compliance"
    description = "Implementation artifacts must reference a requirement id."

    def post(self, node, context, result) -> Optional[PolicyDecision]:
        from .types import Stage

        if node.stage is not Stage.IMPLEMENTATION:
            return None
        if not result.artifacts:
            return None
        if not result.outputs.get("requirement_ids"):
            return self._decide(
                PolicyEffect.WARN,
                "implementation artifacts are not linked to a requirement id",
            )
        return None


@dataclass
class CustomPolicy(Policy):
    """Escape hatch for workflow-specific rules without a subclass."""

    id: str
    category: str
    description: str
    pre_fn: Optional[Callable[[Any, RunContext], Optional[PolicyDecision]]] = None
    post_fn: Optional[Callable[[Any, RunContext, AgentResult], Optional[PolicyDecision]]] = None

    def pre(self, node, context):
        return self.pre_fn(node, context) if self.pre_fn else None

    def post(self, node, context, result):
        return self.post_fn(node, context, result) if self.post_fn else None


class PolicyEngine:
    def __init__(self, policies: Optional[List[Policy]] = None) -> None:
        self.policies: List[Policy] = list(policies) if policies is not None else default_policies()

    def add(self, policy: Policy) -> None:
        self.policies.append(policy)

    def evaluate_pre(self, node: Any, context: RunContext) -> PolicyVerdict:
        return self._combine([p.pre(node, context) for p in self.policies])

    def evaluate_post(self, node: Any, context: RunContext, result: AgentResult) -> PolicyVerdict:
        return self._combine([p.post(node, context, result) for p in self.policies])

    @staticmethod
    def _combine(raw: List[Optional[PolicyDecision]]) -> PolicyVerdict:
        decisions = [d for d in raw if d is not None]
        if not decisions:
            return PolicyVerdict(PolicyEffect.ALLOW, [])
        strongest = max(decisions, key=lambda d: _EFFECT_RANK[d.effect])
        return PolicyVerdict(strongest.effect, decisions)

    def describe(self) -> List[Dict[str, str]]:
        return [
            {"id": p.id, "category": p.category, "description": p.description}
            for p in self.policies
        ]


def default_policies() -> List[Policy]:
    """The baseline control set applied to every workflow."""
    return [
        NoSecretsInArtifacts(),
        NoCriticalSecurityFindings(),
        WorkspaceConfinement(),
        ChangeBudget(),
        NoNewDependencies(),
        DestructiveMigrationControl(),
        AutonomyBoundary(),
        ReleaseRequiresEvidence(),
        TraceabilityRequired(),
    ]
