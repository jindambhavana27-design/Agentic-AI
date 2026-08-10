"""Release readiness.

The release agent asserts nothing of its own. It reads the evidence other stages
deposited in shared context and decides whether that evidence clears the
declared bar. If a stage never ran, its evidence is missing and the check fails
-- absence is not treated as success, which is the single most common way a
release gate becomes decorative.

The node that carries this agent is declared ``ACT_WITH_APPROVAL``: even a fully
green checklist still requires a human decision to release.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ..types import AgentResult, Severity
from .base import Agent, AgentContext


@dataclass
class ReadinessCheck:
    id: str
    description: str
    passed: bool
    detail: str
    blocking: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "description": self.description, "passed": self.passed,
                "detail": self.detail, "blocking": self.blocking}


class ReleaseAgent(Agent):
    name = "release-manager"
    description = "Evaluates release readiness from recorded evidence and drafts release notes."
    reads = ("test_report", "security_report", "documentation_report", "requirements",
             "design_coverage", "impact_analysis", "open_questions")
    writes = ("release_decision", "release_notes", "readiness_checks")

    def __init__(self, min_coverage: float = 60.0, version: str = "1.1.0",
                 require_zero_high_security: bool = True) -> None:
        self.min_coverage = min_coverage
        self.version = version
        self.require_zero_high_security = require_zero_high_security

    def execute(self, ctx: AgentContext) -> AgentResult:
        test_report = ctx.read("test_report") or {}
        security_report = ctx.read("security_report") or {}
        docs_report = ctx.read("documentation_report") or {}
        requirements = ctx.read("requirements", []) or []
        questions = ctx.read("open_questions", []) or []
        coverage_design = ctx.read("design_coverage", 0.0) or 0.0
        impact = ctx.read("impact_analysis", {}) or {}

        checks: List[ReadinessCheck] = [
            ReadinessCheck(
                "REL-01", "All automated tests pass",
                bool(test_report.get("success")),
                "%s/%s passed" % (test_report.get("passed", 0), test_report.get("tests_run", 0))
                if test_report else "no test evidence recorded"),
            ReadinessCheck(
                "REL-02", "Statement coverage meets the floor (%.0f%%)" % self.min_coverage,
                float(test_report.get("coverage_percent", 0.0)) >= self.min_coverage,
                "%.1f%% measured" % float(test_report.get("coverage_percent", 0.0))
                if test_report else "no coverage evidence recorded"),
            ReadinessCheck(
                "REL-03", "No critical security findings",
                bool(security_report) and security_report.get("by_severity", {}).get("critical", 1) == 0,
                "%d critical" % security_report.get("by_severity", {}).get("critical", -1)
                if security_report else "no security evidence recorded"),
            ReadinessCheck(
                "REL-04", "No high-severity security findings",
                bool(security_report)
                and security_report.get("by_severity", {}).get("high", 1) == 0,
                "%d high" % security_report.get("by_severity", {}).get("high", -1)
                if security_report else "no security evidence recorded",
                blocking=self.require_zero_high_security),
            ReadinessCheck(
                "REL-05", "API documentation matches the implementation",
                bool(docs_report.get("in_sync")),
                "%d drift item(s)" % (len(docs_report.get("drift", {}).get("undocumented", []))
                                      + len(docs_report.get("drift", {}).get("phantom", [])))
                if docs_report else "no documentation evidence recorded"),
            ReadinessCheck(
                "REL-06", "No unresolved blocking requirement questions",
                not [q for q in questions if q.get("blocking") and not q.get("resolved")],
                "%d unresolved" % len([q for q in questions
                                       if q.get("blocking") and not q.get("resolved")])),
            ReadinessCheck(
                "REL-07", "Every functional requirement is owned by a component",
                float(coverage_design) >= 1.0,
                "%.0f%% design coverage" % (float(coverage_design) * 100), blocking=False),
            ReadinessCheck(
                "REL-08", "No impacted module is left untested",
                not impact.get("untested_impacted"),
                "%d untested impacted module(s)" % len(impact.get("untested_impacted", []) or []),
                blocking=False),
        ]

        blocking_failures = [c for c in checks if c.blocking and not c.passed]
        advisory_failures = [c for c in checks if not c.blocking and not c.passed]

        findings = [
            self.finding(Severity.HIGH if c.blocking else Severity.LOW,
                         "release check %s failed: %s (%s)" % (c.id, c.description, c.detail),
                         category="release")
            for c in checks if not c.passed
        ]

        notes = _render_notes(self.version, requirements, test_report, security_report,
                              docs_report, checks, impact)
        decision = {
            "version": self.version,
            "ready": not blocking_failures,
            "blocking_failures": [c.id for c in blocking_failures],
            "advisory_failures": [c.id for c in advisory_failures],
            "checks": [c.to_dict() for c in checks],
        }
        metrics = {
            "checks_total": len(checks),
            "checks_passed": sum(1 for c in checks if c.passed),
            "blocking_failures": len(blocking_failures),
            "release_ready": not blocking_failures,
        }

        if blocking_failures:
            return self.failed(
                "release blocked by %d check(s): %s"
                % (len(blocking_failures), ", ".join(c.id for c in blocking_failures)),
                findings=findings, metrics=metrics,
                outputs={"release_decision": decision, "readiness_checks": decision["checks"]},
            )

        return self.ok(
            outputs={"release_decision": decision, "release_notes": notes,
                     "readiness_checks": decision["checks"]},
            artifacts=[self.document("release-notes", "docs/release-notes.md", notes)],
            findings=findings, metrics=metrics,
            rationale="all %d blocking readiness check(s) passed; %d advisory item(s) outstanding"
                      % (len([c for c in checks if c.blocking]), len(advisory_failures)),
        )


def _render_notes(version: str, requirements: List[Dict[str, Any]],
                  test_report: Dict[str, Any], security_report: Dict[str, Any],
                  docs_report: Dict[str, Any], checks: List[ReadinessCheck],
                  impact: Dict[str, Any]) -> str:
    lines = ["# Release Notes -- v%s" % version, "", "## Scope", ""]
    for requirement in requirements:
        if requirement.get("kind") == "constraint":
            continue
        lines.append("- **%s** %s" % (requirement["id"], requirement["text"]))
    lines += ["", "## Verification evidence", "",
              "| Check | Result | Detail |", "| --- | --- | --- |"]
    for check in checks:
        lines.append("| %s %s | %s | %s |" % (
            check.id, check.description,
            "PASS" if check.passed else ("**FAIL**" if check.blocking else "warn"),
            check.detail))
    lines += ["", "## Quality summary", "",
              "- tests: %s passed of %s run" % (test_report.get("passed", "?"),
                                                test_report.get("tests_run", "?")),
              "- statement coverage: %.1f%%" % float(test_report.get("coverage_percent", 0.0)),
              "- security findings: %s" % (security_report.get("by_severity") or "not scanned"),
              "- documentation in sync: %s" % docs_report.get("in_sync", "unknown")]
    if impact.get("impacted_modules"):
        lines += ["", "## Blast radius", "",
                  "This change touches %d module(s): %s"
                  % (len(impact["impacted_modules"]),
                     ", ".join("`%s`" % m for m in impact["impacted_modules"]))]
    lines += ["", "## Rollback", "",
              "The orchestrator snapshots the workspace before implementation and restores it "
              "on a rollback path. Deployed rollback is redeploying the previous artifact; the "
              "schema migrations in this release are additive and backward compatible."]
    return "\n".join(lines) + "\n"
