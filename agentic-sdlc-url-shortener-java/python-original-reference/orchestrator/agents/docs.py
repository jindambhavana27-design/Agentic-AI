"""Documentation generation and drift detection.

Documentation is treated as a testable artifact. The agent extracts the route
table from the source and compares it against the committed OpenAPI schema, so
an endpoint added without a schema entry -- or a schema entry for an endpoint
that no longer exists -- is a **failure**, not a stale paragraph nobody notices.

The OpenAPI document is emitted by hand rather than through a YAML library: the
service has no third-party dependencies, and adding one to the orchestrator to
write eight paths would be a poor trade.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from ..types import AgentResult, Severity
from .base import Agent, AgentContext

REQUIRED_README_SECTIONS = ("Quick start", "API", "Configuration", "Testing")


class DocumentationAgent(Agent):
    name = "doc-writer"
    description = "Generates API documentation and fails on contract drift."
    reads = ("route_table", "api_contract", "requirements", "test_report")
    writes = ("documentation_report", "openapi", "docs_drift")

    def __init__(self, package: str = "service", schema_path: str = "docs/openapi.yaml",
                 readme_path: str = "README.md", mode: str = "verify") -> None:
        """``mode='sync'`` regenerates the schema in the workspace to match the
        code and reports what it reconciled. ``mode='verify'`` changes nothing
        and fails on any drift -- that is the mode a release gate runs in."""
        if mode not in ("sync", "verify"):
            raise ValueError("mode must be 'sync' or 'verify'")
        self.package = package
        self.schema_path = schema_path
        self.readme_path = readme_path
        self.mode = mode
        self.name = "doc-writer" if mode == "sync" else "doc-verifier"

    def execute(self, ctx: AgentContext) -> AgentResult:
        from .impact import _extract_routes

        contract = ctx.read("api_contract", {}) or {}

        # Always re-extract from the workspace. A route table published by an
        # earlier analysis stage predates the implementation, so trusting it
        # would document the API as it was before the change -- which is exactly
        # the drift this agent exists to catch.
        app_path = os.path.join(ctx.workspace, self.package, "app.py")
        routes = [r.to_dict() for r in _extract_routes(app_path)]
        if not routes:
            return self.failed("no routes could be extracted; cannot verify the API contract")

        analysed = ctx.read("route_table", []) or []
        stale_analysis = (
            len(analysed) != len(routes) if analysed else False
        )

        implemented = {(r["method"].upper(), _normalise(r["path"])) for r in routes}
        schema_file = os.path.join(ctx.workspace, self.schema_path)
        documented = _read_schema_paths(schema_file)
        schema = _render_openapi(routes, contract)

        reconciled: List[str] = []
        if self.mode == "sync" and not ctx.dry_run:
            reconciled = sorted("%s %s" % p for p in
                                (implemented - documented) | (documented - implemented))
            os.makedirs(os.path.dirname(schema_file) or ".", exist_ok=True)
            with open(schema_file, "w", encoding="utf-8") as handle:
                handle.write(schema)
            # Re-read what was just written; the drift verdict must describe the
            # file on disk, not what the agent intended to write.
            documented = _read_schema_paths(schema_file)

        undocumented = sorted(implemented - documented)
        phantom = sorted(documented - implemented)

        findings = []
        for method, path in undocumented:
            findings.append(self.finding(
                Severity.HIGH, "endpoint %s %s is implemented but not documented" % (method, path),
                category="documentation", location=self.schema_path,
                remediation="add the path to the OpenAPI schema"))
        for method, path in phantom:
            findings.append(self.finding(
                Severity.MEDIUM, "endpoint %s %s is documented but not implemented" % (method, path),
                category="documentation", location=self.schema_path,
                remediation="remove the stale path or implement it"))

        if stale_analysis:
            findings.append(self.finding(
                Severity.LOW,
                "the API surface changed after impact analysis ran (%d routes analysed, "
                "%d implemented)" % (len(analysed), len(routes)),
                category="documentation",
                remediation="expected when a change adds routes; recorded so the "
                            "analysis-time blast radius is not mistaken for the final one"))

        missing_sections = self._readme_gaps(ctx.workspace)
        for section in missing_sections:
            findings.append(self.finding(
                Severity.LOW, "README is missing a %r section" % section,
                category="documentation", location=self.readme_path,
                remediation="document it so a new operator can run this unaided"))

        artifacts = [
            self.document("api-reference", "docs/api.md", _render_api_doc(routes, contract)),
            self.document("openapi", "docs/openapi.yaml", schema),
        ]

        drift = {"undocumented": ["%s %s" % p for p in undocumented],
                 "phantom": ["%s %s" % p for p in phantom],
                 "reconciled": reconciled}
        report = {
            "mode": self.mode,
            "routes_implemented": len(implemented),
            "routes_documented": len(documented),
            "drift": drift,
            "reconciled": reconciled,
            "readme_gaps": missing_sections,
            "in_sync": not undocumented and not phantom,
        }
        metrics = {
            "routes_implemented": len(implemented),
            "routes_documented": len(documented),
            "drift_count": len(undocumented) + len(phantom),
            "drift_reconciled": len(reconciled),
            "readme_gaps": len(missing_sections),
        }

        if undocumented or phantom:
            return self.failed(
                "API documentation drift: %d undocumented, %d phantom endpoint(s)"
                % (len(undocumented), len(phantom)),
                findings=findings, metrics=metrics,
                outputs={"documentation_report": report, "openapi": schema, "docs_drift": drift},
            )

        return self.ok(
            outputs={"documentation_report": report, "openapi": schema, "docs_drift": drift},
            artifacts=artifacts, findings=findings, metrics=metrics,
            rationale="%d endpoint(s) in sync with the implementation%s"
                      % (len(implemented),
                         "; reconciled %d drift item(s)" % len(reconciled) if reconciled else ""),
        )

    def _readme_gaps(self, workspace: str) -> List[str]:
        path = os.path.join(workspace, self.readme_path)
        if not os.path.exists(path):
            return list(REQUIRED_README_SECTIONS)
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read().lower()
        return [s for s in REQUIRED_README_SECTIONS if s.lower() not in content]


def _normalise(path: str) -> str:
    """Normalise a path so `{code}` and `{id}` compare equal across sources."""
    path = re.sub(r"\{[^}]*\}", "{param}", path)
    return "/" + path.strip("/")


def _read_schema_paths(path: str) -> Set[Tuple[str, str]]:
    """Extract (METHOD, path) pairs from an OpenAPI YAML document.

    A deliberately small parser for the two-level ``paths:`` block, so the
    project keeps its zero-dependency property. It reads what this agent writes.
    """
    if not os.path.exists(path):
        return set()
    pairs: Set[Tuple[str, str]] = set()
    current: Optional[str] = None
    in_paths = False
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            indent = len(line) - len(line.lstrip())
            stripped = line.strip()
            if indent == 0:
                in_paths = stripped.startswith("paths:")
                current = None
                continue
            if not in_paths:
                continue
            if indent == 2 and stripped.endswith(":"):
                current = stripped[:-1].strip().strip("'\"")
                continue
            if indent == 4 and current and stripped.endswith(":"):
                method = stripped[:-1].strip().lower()
                if method in ("get", "post", "put", "patch", "delete", "head", "options"):
                    pairs.add((method.upper(), _normalise(current)))
    return pairs


def _render_openapi(routes: List[Dict[str, Any]], contract: Dict[str, Any]) -> str:
    by_path: Dict[str, List[Dict[str, Any]]] = {}
    for route in routes:
        by_path.setdefault(_normalise(route["path"]), []).append(route)

    lines = [
        "openapi: 3.0.3",
        "info:",
        "  title: URL Shortener API",
        "  version: '%s'" % (contract.get("version") or "1.0.0"),
        "  description: >-",
        "    Short-link creation, resolution, and click analytics.",
        "    Management endpoints require an X-API-Key header; redirects are public.",
        "servers:",
        "  - url: http://127.0.0.1:8080",
        "components:",
        "  securitySchemes:",
        "    ApiKeyAuth:",
        "      type: apiKey",
        "      in: header",
        "      name: X-API-Key",
        "  schemas:",
        "    Error:",
        "      type: object",
        "      properties:",
        "        error:",
        "          type: object",
        "          properties:",
        "            code: { type: string }",
        "            message: { type: string }",
        "            request_id: { type: string }",
        "    Link:",
        "      type: object",
        "      properties:",
        "        code: { type: string }",
        "        short_url: { type: string }",
        "        url: { type: string, format: uri }",
        "        created_at: { type: string, format: date-time }",
        "        expires_at: { type: string, format: date-time, nullable: true }",
        "        custom_alias: { type: boolean }",
        "paths:",
    ]
    for path in sorted(by_path):
        lines.append("  %s:" % path)
        for route in sorted(by_path[path], key=lambda r: r["method"]):
            method = route["method"].lower()
            lines.append("    %s:" % method)
            lines.append("      operationId: %s" % (route.get("name") or "%s%s" % (method, path)))
            lines.append("      summary: %s" % _summary_for(route))
            if route.get("auth_required"):
                lines.append("      security:")
                lines.append("        - ApiKeyAuth: []")
            if "{param}" in path or "{code}" in path:
                lines += [
                    "      parameters:",
                    "        - name: code",
                    "          in: path",
                    "          required: true",
                    "          schema: { type: string }",
                ]
            lines.append("      responses:")
            for status, description in _responses_for(route, contract).items():
                lines.append("        '%s':" % status)
                lines.append("          description: %s" % description)
    return "\n".join(lines) + "\n"


def _summary_for(route: Dict[str, Any]) -> str:
    return {
        "create_link": "Create a short link",
        "list_links": "List short links",
        "get_link": "Fetch link metadata",
        "delete_link": "Soft-delete a link",
        "link_stats": "Fetch aggregated click analytics",
        "redirect": "Resolve a short code and redirect",
        "healthz": "Liveness probe",
        "readyz": "Readiness probe",
        "metrics": "Prometheus metrics",
    }.get(route.get("name", ""), "%s %s" % (route["method"], route["path"]))


def _responses_for(route: Dict[str, Any], contract: Dict[str, Any]) -> Dict[str, str]:
    for endpoint in contract.get("endpoints", []):
        if endpoint["method"].upper() == route["method"].upper() \
                and _normalise(endpoint["path"]) == _normalise(route["path"]):
            return {str(k): str(v) for k, v in endpoint["responses"].items()}
    defaults = {
        "create_link": {"201": "created", "400": "validation error", "409": "alias taken"},
        "get_link": {"200": "link metadata", "404": "unknown code"},
        "list_links": {"200": "page of links"},
        "delete_link": {"204": "deleted", "404": "unknown code"},
        "link_stats": {"200": "aggregated analytics", "404": "unknown code"},
        "redirect": {"302": "redirect", "404": "unknown code", "410": "expired or deleted"},
        "healthz": {"200": "alive"},
        "readyz": {"200": "ready", "503": "degraded"},
        "metrics": {"200": "Prometheus exposition"},
    }
    return defaults.get(route.get("name", ""), {"200": "success"})


def _render_api_doc(routes: List[Dict[str, Any]], contract: Dict[str, Any]) -> str:
    lines = ["# API Reference", "",
             "All management endpoints require `X-API-Key`. Redirects are public.", "",
             "| Method | Path | Auth | Description |", "| --- | --- | --- | --- |"]
    for route in sorted(routes, key=lambda r: (_normalise(r["path"]), r["method"])):
        lines.append("| `%s` | `%s` | %s | %s |" % (
            route["method"], _normalise(route["path"]),
            "required" if route.get("auth_required") else "public", _summary_for(route)))
    lines += ["", "## Error shape", "",
              "```json",
              '{"error": {"code": "validation_error", "message": "...", "request_id": "..."}}',
              "```", "",
              "Every response carries `X-Request-Id`; the same value appears in the "
              "structured logs and in the error body, so a report can be traced to a request."]
    return "\n".join(lines) + "\n"
