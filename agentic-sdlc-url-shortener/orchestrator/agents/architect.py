"""Architecture and design.

Produces a component model, the API contract, and architecture decision records.
The design is *validated* against the requirements it claims to satisfy: a
requirement with no component mapped to it is reported as a coverage gap rather
than quietly dropped, which is the failure mode that makes design documents
worthless six weeks later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..types import AgentResult, Severity
from .base import Agent, AgentContext


@dataclass
class Component:
    name: str
    responsibility: str
    depends_on: List[str] = field(default_factory=list)
    satisfies: List[str] = field(default_factory=list)
    interfaces: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "responsibility": self.responsibility,
            "depends_on": self.depends_on, "satisfies": self.satisfies,
            "interfaces": self.interfaces,
        }


@dataclass
class DecisionRecord:
    id: str
    title: str
    context: str
    decision: str
    consequences: str
    alternatives: List[str] = field(default_factory=list)
    satisfies: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "context": self.context,
            "decision": self.decision, "consequences": self.consequences,
            "alternatives": self.alternatives, "satisfies": self.satisfies,
        }


class ArchitectAgent(Agent):
    name = "architect"
    description = "Derives a component model, API contract, and ADRs from requirements."
    reads = ("requirements", "constraints", "existing_components")
    writes = ("design", "components", "api_contract", "adrs", "design_coverage")

    def __init__(self, blueprint: Optional[Dict[str, Any]] = None) -> None:
        # A blueprint lets a workflow supply the target architecture; without
        # one the agent derives a baseline from the requirement keywords.
        self.blueprint = blueprint

    def execute(self, ctx: AgentContext) -> AgentResult:
        requirements: List[Dict[str, Any]] = ctx.read("requirements", []) or []
        if not requirements:
            return self.failed("no normalised requirements available to design against")
        constraints: List[str] = ctx.read("constraints", []) or []

        components = self._components(requirements)
        adrs = self._decisions(requirements, constraints)
        contract = self._api_contract(requirements)

        # -- validate the design against the requirements it claims to cover --
        covered = {rid for component in components for rid in component.satisfies}
        functional = [r for r in requirements if r.get("kind") == "functional"]
        gaps = [r["id"] for r in functional if r["id"] not in covered]

        findings = [
            self.finding(Severity.HIGH,
                         "requirement %s has no component assigned" % rid,
                         category="design",
                         remediation="assign an owning component or move the requirement out of scope")
            for rid in gaps
        ]

        coverage = 1.0 - (len(gaps) / len(functional)) if functional else 1.0
        document = _render_design(components, adrs, contract, constraints, gaps, coverage)

        return self.ok(
            outputs={
                "design": {"components": [c.to_dict() for c in components],
                           "constraints": constraints},
                "components": [c.to_dict() for c in components],
                "api_contract": contract,
                "adrs": [a.to_dict() for a in adrs],
                "design_coverage": round(coverage, 3),
            },
            artifacts=[
                self.document("architecture", "docs/design.md", document),
                self.document("adrs", "docs/decisions.md", _render_adrs(adrs)),
            ],
            findings=findings,
            metrics={
                "components": len(components),
                "decisions": len(adrs),
                "endpoints": len(contract.get("endpoints", [])),
                "design_coverage": round(coverage, 3),
                "uncovered_requirements": len(gaps),
            },
            rationale="designed %d component(s) covering %.0f%% of functional requirements"
                      % (len(components), coverage * 100),
        )

    # -- derivation -----------------------------------------------------------

    def _components(self, requirements: List[Dict[str, Any]]) -> List[Component]:
        if self.blueprint and "components" in self.blueprint:
            return [Component(**c) for c in self.blueprint["components"]]

        text = " ".join(r["text"].lower() for r in requirements)
        ids = {r["id"]: r["text"].lower() for r in requirements}

        def matching(*keywords: str) -> List[str]:
            return [rid for rid, body in ids.items() if any(k in body for k in keywords)]

        components = [
            Component("http-api", "Routing, authentication, rate limiting, error mapping.",
                      depends_on=["domain"],
                      satisfies=matching("api", "endpoint", "http", "request", "auth", "rate"),
                      interfaces=["POST /api/v1/links", "GET /{code}"]),
            Component("domain", "Short-code allocation, resolution, expiry, idempotency.",
                      depends_on=["storage"],
                      satisfies=matching("short", "redirect", "alias", "expire", "ttl", "idempot"),
                      interfaces=["ShortenerService.create_link", "ShortenerService.resolve"]),
            Component("storage", "Persistence port with SQLite and in-memory adapters.",
                      satisfies=matching("store", "persist", "database", "durable"),
                      interfaces=["LinkStore"]),
            Component("analytics", "Click aggregation off the redirect hot path.",
                      depends_on=["storage"],
                      satisfies=matching("analytic", "click", "stat", "count", "report"),
                      interfaces=["AnalyticsRecorder.record", "LinkStore.stats"]),
            Component("observability", "Structured logs, request correlation, metrics.",
                      satisfies=matching("log", "metric", "monitor", "trace", "observ"),
                      interfaces=["GET /metrics", "GET /healthz"]),
        ]
        if any(k in text for k in ("rate", "abuse", "throttle")):
            components.append(Component(
                "rate-limiter", "Per-identity token buckets protecting write endpoints.",
                satisfies=matching("rate", "abuse", "throttle", "limit"),
                interfaces=["TokenBucketRateLimiter.allow"]))
        return [c for c in components if c.satisfies or c.name in ("http-api", "domain", "storage")]

    def _decisions(self, requirements: List[Dict[str, Any]],
                   constraints: List[str]) -> List[DecisionRecord]:
        text = " ".join(r["text"].lower() for r in requirements)
        decisions = [
            DecisionRecord(
                "ADR-001", "Random base62 short codes rather than a counter encoding",
                "Codes must be unguessable but stay short.",
                "Generate 7 random base62 characters with the CSPRNG and retry on collision.",
                "62^7 keyspace makes enumeration impractical and keeps codes short. "
                "Costs one existence check per creation and a collision retry loop.",
                ["monotonic counter in base62 -- shorter, but the whole corpus is enumerable",
                 "hash of the target URL -- deduplicates, but leaks that a URL was shortened"],
                satisfies=[r["id"] for r in requirements if "short" in r["text"].lower()],
            ),
            DecisionRecord(
                "ADR-002", "Soft delete, never reissue a retired code",
                "Deleted links must not silently start pointing somewhere else.",
                "Mark links deleted with a timestamp; the code stays permanently allocated.",
                "Deleted codes return 410 rather than 404 and can never be reused for a "
                "different target. Costs unbounded growth in the links table.",
                ["hard delete -- reclaims the keyspace but allows dangerous code reuse"],
            ),
            DecisionRecord(
                "ADR-003", "Pre-aggregated click analytics",
                "Analytics queries are aggregate-only; raw event rows grow without bound.",
                "Increment per-day and per-referrer counters on resolution; store no raw events.",
                "Constant storage per link per day and O(days) stat queries. "
                "Loses per-click detail, so retrospective analyses are impossible.",
                ["raw event rows -- full fidelity, unbounded growth and slow aggregates",
                 "external analytics pipeline -- correct at scale, disproportionate here"],
            ),
        ]
        if "expire" in text or "ttl" in text:
            decisions.append(DecisionRecord(
                "ADR-004", "Lazy expiry evaluated on read",
                "Expired links must stop resolving without a sweeper process.",
                "Compare expires_at at resolution time and return 410 when past.",
                "No background job and no clock skew between sweeper and reader. "
                "Expired rows occupy storage until a separate compaction removes them.",
                ["scheduled purge job -- reclaims storage, adds an operational component"],
            ))
        if any("rate" in c.lower() for c in constraints) or "rate" in text:
            decisions.append(DecisionRecord(
                "ADR-005", "In-process token-bucket rate limiting",
                "Write endpoints need abuse protection without new infrastructure.",
                "Per-identity token buckets held in process memory, refilled lazily.",
                "Zero dependencies and no network hop. The limit is per replica, so N "
                "replicas permit N times the configured rate -- a shared counter is "
                "required before horizontal scaling.",
                ["Redis-backed counters -- correct across replicas, adds a hard dependency"],
            ))
        return decisions

    def _api_contract(self, requirements: List[Dict[str, Any]]) -> Dict[str, Any]:
        endpoints = [
            {"method": "POST", "path": "/api/v1/links", "auth": True,
             "request": {"url": "string (required)", "alias": "string (optional)",
                         "ttl_seconds": "integer (optional)", "metadata": "object (optional)"},
             "responses": {201: "created", 200: "idempotent replay", 400: "validation_error",
                           401: "unauthenticated", 409: "alias taken", 429: "rate_limited"}},
            {"method": "GET", "path": "/api/v1/links/{code}", "auth": True,
             "responses": {200: "link metadata", 404: "unknown code", 410: "deleted"}},
            {"method": "GET", "path": "/api/v1/links/{code}/stats", "auth": True,
             "responses": {200: "aggregated click analytics", 404: "unknown code"}},
            {"method": "DELETE", "path": "/api/v1/links/{code}", "auth": True,
             "responses": {204: "deleted", 404: "unknown code", 410: "already deleted"}},
            {"method": "GET", "path": "/{code}", "auth": False,
             "responses": {302: "redirect to target", 404: "unknown code", 410: "expired or deleted"}},
            {"method": "GET", "path": "/healthz", "auth": False, "responses": {200: "alive"}},
            {"method": "GET", "path": "/readyz", "auth": False,
             "responses": {200: "ready", 503: "storage degraded"}},
            {"method": "GET", "path": "/metrics", "auth": False,
             "responses": {200: "Prometheus exposition"}},
        ]
        return {"version": "v1", "base_path": "/api/v1", "endpoints": endpoints,
                "error_shape": {"error": {"code": "string", "message": "string",
                                          "request_id": "string", "details": "object"}}}


def _render_design(components: List[Component], adrs: List[DecisionRecord],
                   contract: Dict[str, Any], constraints: List[str],
                   gaps: List[str], coverage: float) -> str:
    lines = ["# Architecture", "",
             "## Components", "",
             "| Component | Responsibility | Depends on | Satisfies |",
             "| --- | --- | --- | --- |"]
    for component in components:
        lines.append("| `%s` | %s | %s | %s |" % (
            component.name, component.responsibility,
            ", ".join("`%s`" % d for d in component.depends_on) or "-",
            ", ".join(component.satisfies) or "-",
        ))
    lines += ["", "## API contract", "",
              "| Method | Path | Auth | Responses |", "| --- | --- | --- | --- |"]
    for endpoint in contract["endpoints"]:
        lines.append("| %s | `%s` | %s | %s |" % (
            endpoint["method"], endpoint["path"],
            "yes" if endpoint["auth"] else "no",
            ", ".join("%s %s" % (k, v) for k, v in endpoint["responses"].items()),
        ))
    lines += ["", "## Constraints", ""] + (["- %s" % c for c in constraints] or ["None stated."])
    lines += ["", "## Requirement coverage", "",
              "Functional coverage: **%.0f%%**" % (coverage * 100)]
    if gaps:
        lines.append("")
        lines.append("Uncovered requirements (must be assigned or descoped): %s"
                     % ", ".join("`%s`" % g for g in gaps))
    lines += ["", "## Decisions", ""] + ["- **%s** %s" % (a.id, a.title) for a in adrs]
    return "\n".join(lines) + "\n"


def _render_adrs(adrs: List[DecisionRecord]) -> str:
    lines = ["# Architecture Decision Records", ""]
    for adr in adrs:
        lines += [
            "## %s -- %s" % (adr.id, adr.title), "",
            "**Context.** %s" % adr.context, "",
            "**Decision.** %s" % adr.decision, "",
            "**Consequences.** %s" % adr.consequences, "",
            "**Alternatives considered.**", "",
        ]
        lines += ["- %s" % alt for alt in adr.alternatives] or ["- None recorded."]
        lines.append("")
    return "\n".join(lines) + "\n"
