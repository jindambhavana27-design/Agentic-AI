"""Static security review of the workspace.

Real AST and lexical analysis over the source tree. Each check exists because
the corresponding mistake is both common and consequential in a service that
takes a URL from an untrusted caller and later follows it.

The scanner is intentionally conservative about severity. A scanner that cries
CRITICAL at every string concatenation gets muted, and a muted scanner is worse
than none. Findings that cannot be judged statically are reported at MEDIUM with
the reason, not escalated to force attention.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..types import AgentResult, Finding, Severity
from .base import Agent, AgentContext

_SECRET_PATTERNS: List[Tuple[str, "re.Pattern"]] = [
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b")),
    ("hardcoded credential", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|passwd|password|auth[_-]?token)\b\s*[=:]\s*['\"][^'\"\s]{12,}['\"]")),
]

# name -> (severity, why it matters)
_DANGEROUS_CALLS: Dict[str, Tuple[Severity, str]] = {
    "eval": (Severity.CRITICAL, "evaluates arbitrary expressions"),
    "exec": (Severity.CRITICAL, "executes arbitrary code"),
    "system": (Severity.HIGH, "spawns a shell"),
    "popen": (Severity.HIGH, "spawns a shell"),
    "loads": (Severity.HIGH, "pickle.loads deserialises arbitrary objects"),
}

_SQL_METHODS = {"execute", "executemany", "executescript"}


@dataclass
class _Hit:
    severity: Severity
    message: str
    category: str
    location: str
    remediation: str


class SecurityAgent(Agent):
    name = "security-reviewer"
    description = "Static security review of source, configuration, and HTTP surface."
    reads = ("changed_files", "route_table", "impact_analysis")
    writes = ("security_report", "security_findings")

    def __init__(self, package: str = "service", scan_paths: Optional[List[str]] = None,
                 fail_on: Severity = Severity.CRITICAL) -> None:
        self.package = package
        self.scan_paths = scan_paths
        self.fail_on = fail_on

    def execute(self, ctx: AgentContext) -> AgentResult:
        ctx.read("changed_files", [])
        roots = self.scan_paths or [self.package]
        files = list(_python_files(ctx.workspace, roots))
        if not files:
            return self.failed("no Python sources found to scan under %s" % ", ".join(roots))

        hits: List[_Hit] = []
        for path in files:
            rel = os.path.relpath(path, ctx.workspace)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    source = handle.read()
            except OSError:
                continue
            hits.extend(_scan_text(source, rel))
            try:
                tree = ast.parse(source, filename=path)
            except SyntaxError as exc:
                hits.append(_Hit(Severity.HIGH, "file does not parse: %s" % exc,
                                 "correctness", rel, "fix the syntax error"))
                continue
            hits.extend(_scan_ast(tree, rel))

        hits.extend(self._surface_checks(ctx))

        findings = [
            self.finding(h.severity, h.message, category=h.category,
                         location=h.location, remediation=h.remediation)
            for h in hits
        ]
        by_severity = {s.value: sum(1 for h in hits if h.severity is s) for s in Severity}
        report = {
            "files_scanned": len(files),
            "findings_total": len(hits),
            "by_severity": by_severity,
            "findings": [f.to_dict() for f in findings],
            "checks": [
                "dangerous call sites (eval/exec/shell/pickle)",
                "hardcoded credentials",
                "SQL built by string interpolation",
                "non-cryptographic randomness for security-relevant values",
                "assert used for runtime validation",
                "SSRF controls on user-supplied URLs",
                "security response headers and unauthenticated write endpoints",
            ],
            "limits": [
                "static analysis only; no dependency CVE scan and no runtime testing",
                "cannot see behaviour behind dynamic dispatch or reflection",
                "SQL interpolation is detected at the call site only, so hoisting the "
                "query into a local variable defeats it -- this is a lint, not a proof",
            ],
        }
        document = _render_report(report)

        criticals = [h for h in hits if h.severity is Severity.CRITICAL]
        metrics = {
            "files_scanned": len(files),
            "findings_total": len(hits),
            "critical_findings": len(criticals),
            "high_findings": by_severity.get("high", 0),
        }

        if criticals:
            return self.failed(
                "%d critical security finding(s)" % len(criticals),
                findings=findings, metrics=metrics,
                outputs={"security_report": report, "security_findings": report["findings"]},
            )

        return self.ok(
            outputs={"security_report": report, "security_findings": report["findings"]},
            artifacts=[self.document("security-review", "docs/security-review.md", document)],
            findings=findings, metrics=metrics,
            rationale="scanned %d file(s); %d finding(s), %d high, 0 critical"
                      % (len(files), len(hits), by_severity.get("high", 0)),
        )

    def _surface_checks(self, ctx: AgentContext) -> List[_Hit]:
        """Checks over the HTTP surface rather than individual files."""
        from .impact import _extract_routes

        hits: List[_Hit] = []
        # Extracted from the workspace, not from context: this agent runs after
        # implementation, and a route added by the change must be reviewed.
        routes = [r.to_dict() for r in
                  _extract_routes(os.path.join(ctx.workspace, self.package, "app.py"))]
        if not routes:
            routes = ctx.read("route_table", []) or []
        for route in routes:
            if route.get("method") in ("POST", "PUT", "PATCH", "DELETE") \
                    and not route.get("auth_required"):
                hits.append(_Hit(
                    Severity.HIGH,
                    "%s %s mutates state without authentication"
                    % (route["method"], route.get("path", route.get("pattern"))),
                    "security", "route:%s" % route.get("name", "?"),
                    "require an API key on every state-changing endpoint"))

        app_path = os.path.join(ctx.workspace, self.package, "app.py")
        if os.path.exists(app_path):
            with open(app_path, "r", encoding="utf-8") as handle:
                source = handle.read()
            for header in ("X-Content-Type-Options", "X-Frame-Options"):
                if header not in source:
                    hits.append(_Hit(
                        Severity.LOW, "response header %s is never set" % header,
                        "security", "%s/app.py" % self.package,
                        "add it to the default security header set"))

        validation_path = os.path.join(ctx.workspace, self.package, "validation.py")
        if os.path.exists(validation_path):
            with open(validation_path, "r", encoding="utf-8") as handle:
                source = handle.read()
            if "is_private" not in source and "ip_address" not in source:
                hits.append(_Hit(
                    Severity.CRITICAL,
                    "user-supplied URLs are not checked against private address space",
                    "security", "%s/validation.py" % self.package,
                    "reject loopback, link-local, and RFC1918 targets before storing them"))
        else:
            hits.append(_Hit(
                Severity.HIGH, "no URL validation module found",
                "security", self.package,
                "validate and normalise every user-supplied URL before persisting it"))
        return hits


# -- scanners -----------------------------------------------------------------


def _python_files(workspace: str, roots: Iterable[str]) -> Iterable[str]:
    for root in roots:
        base = os.path.join(workspace, root)
        if os.path.isfile(base) and base.endswith(".py"):
            yield base
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git", ".venv")]
            for filename in sorted(filenames):
                if filename.endswith(".py"):
                    yield os.path.join(dirpath, filename)


def _scan_text(source: str, rel: str) -> List[_Hit]:
    hits: List[_Hit] = []
    for line_no, line in enumerate(source.splitlines(), start=1):
        for label, pattern in _SECRET_PATTERNS:
            if pattern.search(line):
                hits.append(_Hit(
                    Severity.CRITICAL, "%s appears in source" % label,
                    "security", "%s:%d" % (rel, line_no),
                    "move it to configuration and rotate the exposed credential"))
    return hits


def _scan_ast(tree: ast.AST, rel: str) -> List[_Hit]:
    hits: List[_Hit] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            hits.extend(_check_call(node, rel))
        elif isinstance(node, ast.Assert):
            # Asserts vanish under `python -O`, so validation written as an
            # assert silently disappears in an optimised deployment.
            hits.append(_Hit(
                Severity.MEDIUM, "assert used outside tests",
                "security", "%s:%d" % (rel, node.lineno),
                "raise an explicit exception; asserts are stripped under -O"))
    return hits


def _check_call(node: ast.Call, rel: str) -> List[_Hit]:
    hits: List[_Hit] = []
    name = _call_name(node.func)
    location = "%s:%d" % (rel, node.lineno)

    if name in _DANGEROUS_CALLS:
        severity, why = _DANGEROUS_CALLS[name]
        qualified = _dotted(node.func)
        # json.loads and similar share the bare name; only flag the real ones.
        if name != "loads" or qualified.startswith("pickle"):
            hits.append(_Hit(severity, "call to %s -- %s" % (qualified or name, why),
                             "security", location,
                             "remove it, or constrain the input to a validated allowlist"))

    if any(kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True
           for kw in node.keywords):
        hits.append(_Hit(Severity.HIGH, "subprocess invoked with shell=True",
                         "security", location,
                         "pass an argument list instead of a shell string"))

    if isinstance(node.func, ast.Attribute) and node.func.attr in _SQL_METHODS and node.args:
        query = node.args[0]
        interpolated = (
            isinstance(query, ast.JoinedStr)
            or (isinstance(query, ast.BinOp) and isinstance(query.op, (ast.Add, ast.Mod)))
        )
        if interpolated:
            hits.append(_Hit(
                Severity.HIGH, "SQL statement built by string interpolation",
                "security", location,
                "use parameter placeholders so values are never parsed as SQL"))

    dotted = _dotted(node.func)
    if dotted.startswith("random.") and dotted.split(".")[-1] in ("choice", "randint", "random",
                                                                  "sample", "randrange"):
        hits.append(_Hit(
            Severity.MEDIUM, "non-cryptographic randomness (%s)" % dotted,
            "security", location,
            "use the secrets module for anything an attacker should not predict"))
    return hits


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _dotted(node: ast.AST) -> str:
    parts: List[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _render_report(report: Dict[str, Any]) -> str:
    lines = ["# Security Review", "",
             "- files scanned: **%d**" % report["files_scanned"],
             "- findings: **%d**" % report["findings_total"], ""]
    counts = report["by_severity"]
    lines.append("| Severity | Count |")
    lines.append("| --- | ---: |")
    for severity in ("critical", "high", "medium", "low", "info"):
        lines.append("| %s | %d |" % (severity, counts.get(severity, 0)))
    lines += ["", "## Findings", ""]
    if report["findings"]:
        lines += ["| Severity | Location | Finding | Remediation |",
                  "| --- | --- | --- | --- |"]
        for finding in report["findings"]:
            lines.append("| %s | `%s` | %s | %s |" % (
                finding["severity"], finding.get("location") or "-",
                finding["message"], finding.get("remediation") or "-"))
    else:
        lines.append("None.")
    lines += ["", "## Checks performed", ""] + ["- %s" % c for c in report["checks"]]
    lines += ["", "## Limits of this review", ""] + ["- %s" % l for l in report["limits"]]
    return "\n".join(lines) + "\n"
