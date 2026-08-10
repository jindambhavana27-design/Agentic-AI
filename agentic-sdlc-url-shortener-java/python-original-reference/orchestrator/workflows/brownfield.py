"""Scenario 2 -- Brownfield: change an existing system safely.

Requirement: operators cannot see analytics back-pressure, and expired links
accumulate with no way to reclaim them. Add a queue-depth gauge and an
authenticated maintenance endpoint that purges expired links.

What this scenario is meant to demonstrate:

* **codebase reasoning** -- the impact agent parses the real source, derives the
  import graph and the route table, and computes the actual blast radius and
  the modules that no test reaches;
* **change control** -- a workspace snapshot is taken before any edit, the
  implementation node declares a compensation, and failure downstream rolls the
  workspace back rather than leaving it half-modified;
* **contract drift as a hard signal** -- the change adds a route, the schema is
  reconciled by one node and independently re-verified by another;
* **fault injection** (optional) -- ``inject_fault`` makes a node fail its first
  attempts so retry, backoff, and MTTR can be observed rather than asserted.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..agents import (
    ArchitectAgent,
    ChangePlan,
    FileChange,
    ImpactAnalysisAgent,
    ImplementationAgent,
    PlannerAgent,
)
from ..agents.base import FlakyAgentWrapper
from ..agents.requirements import RequirementsAgent
from ..gates import (
    AgentSucceeded,
    ContextKeysPresent,
    MetricThreshold,
    NoOpenQuestions,
    ProducedOutputs,
)
from ..graph import Node, WorkflowGraph
from ..types import AutonomyLevel, FailureAction, RetryPolicy, Stage
from .common import verification_tail

REQUIREMENT = """
- Operators must be able to see how many click events are buffered but not yet written, exposed as a Prometheus gauge on the existing metrics endpoint.
- The service must provide an authenticated maintenance endpoint that permanently removes links which expired more than a caller-specified age ago.
- Purging a link must also remove its click analytics so no orphaned aggregates remain.
- The purge endpoint must reject a negative or non-integer age with a 400 response.
- The purge endpoint must require the same API key authentication as the other management endpoints, and must cost more rate-limit budget than a normal write.
- The change must not alter the behaviour of any existing endpoint, and the API documentation must stay in sync.
"""

CONSTRAINTS = [
    "no third-party runtime dependencies",
    "no breaking change to the LinkStore interface for existing callers",
    "purge must be an explicit operator action, never a background job",
]

CHANGE_TARGET = ["service/app.py", "service/analytics.py", "service/storage/base.py"]


# --- change plan --------------------------------------------------------------

_QUEUE_DEPTH = '''    def queue_depth(self) -> int:
        """Click events buffered but not yet written.

        Exposed as a gauge so back-pressure is visible *before* the queue fills
        and events start being dropped. Returns 0 in synchronous mode, where no
        buffering happens at all.
        """
        if self._synchronous or not self._enabled:
            return 0
        return self._queue.qsize()

    def close(self) -> None:'''

_STORE_PORT = '''    @abc.abstractmethod
    def purge_expired(self, before: float) -> int:
        """Permanently remove links whose expiry is earlier than ``before``.

        Returns the number of links removed. Unlike :meth:`soft_delete` this
        frees the code for reissue, so it is an explicit operator action rather
        than a background job -- see docs/RISKS_AND_TRADEOFFS.md.
        """

    def close(self) -> None:  # pragma: no cover - default no-op'''

_SQLITE_PURGE = '''    # Static statements; the only caller-supplied value is a bound timestamp.
    _PURGE_LINKS = "DELETE FROM links WHERE expires_at IS NOT NULL AND expires_at < ?"
    _PURGE_DAILY = "DELETE FROM click_daily WHERE code NOT IN (SELECT code FROM links)"
    _PURGE_REFERRERS = "DELETE FROM click_referrer WHERE code NOT IN (SELECT code FROM links)"

    def purge_expired(self, before: float) -> int:
        with self.conn:
            cursor = self.conn.execute(self._PURGE_LINKS, (before,))
            removed = cursor.rowcount
            # Aggregates are removed in the same transaction, so a crash cannot
            # leave click counts for links that no longer exist.
            self.conn.execute(self._PURGE_DAILY)
            self.conn.execute(self._PURGE_REFERRERS)
        return removed

    def health(self) -> bool:
        try:'''

_MEMORY_PURGE = '''    def purge_expired(self, before: float) -> int:
        with self._lock:
            doomed = [
                code for code, link in self._links.items()
                if link.expires_at is not None and link.expires_at < before
            ]
            for code in doomed:
                link = self._links.pop(code)
                if link.idempotency_key is not None:
                    self._idem.pop((link.created_by, link.idempotency_key), None)
                self._referrers.pop(code, None)
            for key in [k for k in self._daily if k[0] in set(doomed)]:
                del self._daily[key]
            return len(doomed)

    def health(self) -> bool:
        return True'''

_SERVICE_PURGE = '''    def purge_expired(self, older_than_seconds: int = 0) -> int:
        """Permanently remove links that expired at least ``older_than_seconds`` ago.

        This is the one operation that frees a short code for reissue, so it is
        deliberately explicit, authenticated, and never scheduled.
        """
        if isinstance(older_than_seconds, bool) or not isinstance(older_than_seconds, int):
            raise ValidationError("older_than_seconds must be an integer",
                                  {"older_than_seconds": older_than_seconds})
        if older_than_seconds < 0:
            raise ValidationError("older_than_seconds must not be negative",
                                  {"older_than_seconds": older_than_seconds})
        removed = self.store.purge_expired(utc_now() - older_than_seconds)
        METRICS.increment("links_purged_total", value=removed)
        _log.info("purged expired links", extra={"removed": removed})
        return removed

    def close(self) -> None:
        self.recorder.close()'''

_APP_ROUTE = '''        r("POST", r"^/api/v1/maintenance/purge$", "purge_expired",
          auth_required=True, rate_limit_cost=5.0)(self.handle_purge)
        r("GET", r"^/healthz$", "healthz")(self.handle_healthz)'''

_APP_HANDLER = '''    def handle_purge(self, request: Request) -> Response:
        payload = request.json(self.config.max_body_bytes) if request.body else {}
        removed = self.service.purge_expired(payload.get("older_than_seconds", 0))
        return Response.json(200, {"purged": removed,
                                   "older_than_seconds": payload.get("older_than_seconds", 0)})

    def handle_healthz(self, request: Request) -> Response:'''

_APP_METRICS = '''    def handle_metrics(self, request: Request) -> Response:
        # Sampled at scrape time rather than on every click, so observing the
        # queue costs nothing on the hot path.
        METRICS.set_gauge("analytics_queue_depth", self.service.recorder.queue_depth())
        return Response.text(200, METRICS.render_prometheus(), "text/plain; version=0.0.4")'''

_TESTS = '''"""Tests for analytics back-pressure visibility and expired-link purging."""

import json
import time
import unittest

from service.analytics import AnalyticsRecorder
from service.app import Application, Request, parse_target
from service.config import Config
from service.errors import ValidationError
from service.models import ClickEvent, Link
from service.observability import METRICS
from service.shortener import ShortenerService
from service.storage import MemoryLinkStore
from service.storage.sqlite_store import SQLiteLinkStore


def build_app(tmp_db=None):
    config = Config(db_path=":memory:", api_keys=frozenset({"k"}),
                    base_url="http://s.test", rate_limit_enabled=False)
    store = MemoryLinkStore() if tmp_db is None else SQLiteLinkStore(tmp_db)
    service = ShortenerService(store, config, AnalyticsRecorder(store, queue_size=0))
    return Application(config, service), service, store


def call(app, method, target, body=None, key="k"):
    path, query = parse_target(target)
    headers = {"x-api-key": key} if key else {}
    request = Request(method, path, query, headers,
                      json.dumps(body).encode() if body is not None else b"")
    response = app.handle(request)
    parsed = json.loads(response.body) if response.body else None
    return response.status, parsed


class QueueDepthTests(unittest.TestCase):
    def test_synchronous_recorder_reports_zero(self):
        store = MemoryLinkStore()
        self.assertEqual(AnalyticsRecorder(store, queue_size=0).queue_depth(), 0)

    def test_disabled_recorder_reports_zero(self):
        store = MemoryLinkStore()
        recorder = AnalyticsRecorder(store, queue_size=8, enabled=False)
        self.assertEqual(recorder.queue_depth(), 0)

    def test_async_recorder_drains_to_zero(self):
        store = MemoryLinkStore()
        recorder = AnalyticsRecorder(store, queue_size=64)
        try:
            for _ in range(10):
                recorder.record(ClickEvent(code="abc", timestamp=time.time()))
            self.assertTrue(recorder.flush(timeout=5.0))
            self.assertEqual(recorder.queue_depth(), 0)
        finally:
            recorder.close()

    def test_metrics_endpoint_exposes_the_gauge(self):
        app, _, _ = build_app()
        request = Request("GET", "/metrics", {}, {}, b"")
        body = app.handle(request).body.decode()
        self.assertIn("analytics_queue_depth", body)


class PurgeStoreTests(unittest.TestCase):
    def _seed(self, store):
        now = time.time()
        store.create(Link(code="live", target_url="https://e.com/1", created_at=now,
                          expires_at=now + 3600))
        store.create(Link(code="dead", target_url="https://e.com/2", created_at=now - 100,
                          expires_at=now - 50, idempotency_key="k1", created_by="u"))
        store.create(Link(code="forever", target_url="https://e.com/3", created_at=now))
        store.record_click(ClickEvent(code="dead", timestamp=now - 60))
        return now

    def test_memory_store_purges_only_expired(self):
        store = MemoryLinkStore()
        now = self._seed(store)
        self.assertEqual(store.purge_expired(now), 1)
        self.assertIsNone(store.get("dead"))
        self.assertIsNotNone(store.get("live"))
        self.assertIsNotNone(store.get("forever"))

    def test_memory_store_removes_orphaned_analytics(self):
        store = MemoryLinkStore()
        now = self._seed(store)
        store.purge_expired(now)
        self.assertEqual(store.stats("dead", 7).total_clicks, 0)

    def test_memory_store_frees_the_idempotency_key(self):
        store = MemoryLinkStore()
        now = self._seed(store)
        store.purge_expired(now)
        self.assertIsNone(store.find_by_idempotency_key("k1", "u"))

    def test_memory_store_purge_is_idempotent(self):
        store = MemoryLinkStore()
        now = self._seed(store)
        self.assertEqual(store.purge_expired(now), 1)
        self.assertEqual(store.purge_expired(now), 0)

    def test_sqlite_store_purges_and_clears_aggregates(self):
        import os
        import tempfile

        path = os.path.join(tempfile.mkdtemp(), "purge.db")
        store = SQLiteLinkStore(path)
        try:
            now = self._seed(store)
            self.assertEqual(store.purge_expired(now), 1)
            self.assertIsNone(store.get("dead"))
            self.assertIsNotNone(store.get("live"))
            self.assertEqual(store.stats("dead", 7).total_clicks, 0)
        finally:
            store.close()


class PurgeServiceTests(unittest.TestCase):
    def test_rejects_negative_age(self):
        _, service, _ = build_app()
        with self.assertRaises(ValidationError):
            service.purge_expired(-1)

    def test_rejects_non_integer_age(self):
        _, service, _ = build_app()
        with self.assertRaises(ValidationError):
            service.purge_expired("10")

    def test_rejects_boolean_age(self):
        _, service, _ = build_app()
        with self.assertRaises(ValidationError):
            service.purge_expired(True)

    def test_age_window_protects_recently_expired_links(self):
        _, service, store = build_app()
        now = time.time()
        store.create(Link(code="recent", target_url="https://e.com/r",
                          created_at=now - 20, expires_at=now - 10))
        self.assertEqual(service.purge_expired(3600), 0)
        self.assertEqual(service.purge_expired(0), 1)


class PurgeEndpointTests(unittest.TestCase):
    def test_requires_authentication(self):
        app, _, _ = build_app()
        status, body = call(app, "POST", "/api/v1/maintenance/purge", {}, key=None)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthenticated")

    def test_purges_and_reports_the_count(self):
        app, _, store = build_app()
        now = time.time()
        store.create(Link(code="old1", target_url="https://e.com/a",
                          created_at=now - 100, expires_at=now - 90))
        status, body = call(app, "POST", "/api/v1/maintenance/purge",
                            {"older_than_seconds": 0})
        self.assertEqual(status, 200)
        self.assertEqual(body["purged"], 1)

    def test_rejects_bad_age_with_400(self):
        app, _, _ = build_app()
        status, body = call(app, "POST", "/api/v1/maintenance/purge",
                            {"older_than_seconds": -5})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_empty_body_defaults_to_zero_age(self):
        app, _, _ = build_app()
        path, query = parse_target("/api/v1/maintenance/purge")
        response = app.handle(Request("POST", path, query, {"x-api-key": "k"}, b""))
        self.assertEqual(response.status, 200)

    def test_existing_endpoints_are_unaffected(self):
        app, _, _ = build_app()
        status, body = call(app, "POST", "/api/v1/links", {"url": "https://example.com/x"})
        self.assertEqual(status, 201)
        code = body["code"]
        path, query = parse_target("/" + code)
        self.assertEqual(app.handle(Request("GET", path, query, {}, b"")).status, 302)


if __name__ == "__main__":
    unittest.main()
'''


def change_plan() -> ChangePlan:
    return ChangePlan(
        summary=("Expose analytics queue depth as a gauge and add an authenticated "
                 "maintenance endpoint that purges expired links and their aggregates."),
        requirement_ids=["FR-1", "FR-2", "FR-3", "NFR-1"],
        rollback_note=(
            "Every edit is additive: one new method per storage adapter, one new "
            "service method, one new route and handler. Restoring the workspace "
            "snapshot reverts all of them; no data migration is involved."
        ),
        changes=[
            FileChange(path="service/analytics.py", action="patch", anchor="    def close(self) -> None:",
                       content=_QUEUE_DEPTH,
                       description="expose buffered click count for the metrics gauge"),
            FileChange(path="service/storage/base.py", action="patch",
                       anchor="    def close(self) -> None:  # pragma: no cover - default no-op",
                       content=_STORE_PORT,
                       description="add purge_expired to the storage port"),
            FileChange(path="service/storage/sqlite_store.py", action="patch",
                       anchor="    def health(self) -> bool:\n        try:",
                       content=_SQLITE_PURGE,
                       description="transactional purge of links and orphaned aggregates"),
            FileChange(path="service/storage/memory_store.py", action="patch",
                       anchor="    def health(self) -> bool:\n        return True",
                       content=_MEMORY_PURGE,
                       description="matching purge semantics for the in-memory adapter"),
            FileChange(path="service/shortener.py", action="patch",
                       anchor="    def close(self) -> None:\n        self.recorder.close()",
                       content=_SERVICE_PURGE,
                       description="validated purge entry point on the domain service"),
            FileChange(path="service/app.py", action="patch",
                       anchor='        r("GET", r"^/healthz$", "healthz")(self.handle_healthz)',
                       content=_APP_ROUTE,
                       description="register the authenticated purge route"),
            FileChange(path="service/app.py", action="patch",
                       anchor="    def handle_healthz(self, request: Request) -> Response:",
                       content=_APP_HANDLER,
                       description="purge request handler"),
            FileChange(path="service/app.py", action="patch",
                       anchor=("    def handle_metrics(self, request: Request) -> Response:\n"
                               "        return Response.text(200, METRICS.render_prometheus(), "
                               '"text/plain; version=0.0.4")'),
                       content=_APP_METRICS,
                       description="sample the analytics queue depth at scrape time"),
            FileChange(path="tests/test_maintenance.py", action="create", content=_TESTS,
                       description="tests for queue-depth visibility and purge semantics"),
        ],
    )


def build(inputs: Optional[Dict[str, Any]] = None) -> WorkflowGraph:
    inputs = inputs or {}
    fault = bool(inputs.get("inject_fault"))

    impact_agent = ImpactAnalysisAgent(package="service", test_dir="tests")
    if fault:
        # Fault injection is opt-in and clearly labelled. It exercises the
        # retry/backoff path against the real engine instead of asserting it.
        impact_agent = FlakyAgentWrapper(impact_agent, fail_times=2,
                                         reason="injected transient analysis failure")

    nodes: List[Node] = [
        Node(
            id="requirements", stage=Stage.REQUIREMENTS,
            agent=RequirementsAgent(ambiguity_threshold=0.35),
            description="Normalise the change request and surface ambiguity.",
            entry_gates=[ContextKeysPresent("requirement")],
            exit_gates=[AgentSucceeded(), ProducedOutputs("requirements")],
            autonomy=AutonomyLevel.ACT_AND_REPORT,
        ),
        Node(
            id="impact", stage=Stage.ANALYSIS, agent=impact_agent,
            description="Parse the codebase and compute the blast radius of the change.",
            depends_on=["requirements"],
            entry_gates=[ContextKeysPresent("change_target")],
            exit_gates=[AgentSucceeded(), ProducedOutputs("impact_analysis", "route_table")],
            retry=RetryPolicy(max_attempts=3, backoff_seconds=0.25, backoff_multiplier=2.0),
            autonomy=AutonomyLevel.PROPOSE_ONLY,
        ),
        Node(
            id="architecture", stage=Stage.ARCHITECTURE, agent=ArchitectAgent(),
            description="Fit the change into the existing component model.",
            depends_on=["requirements", "impact"],
            entry_gates=[NoOpenQuestions()],
            exit_gates=[AgentSucceeded(), ProducedOutputs("design", "adrs")],
            autonomy=AutonomyLevel.ACT_AND_REPORT,
        ),
        Node(
            id="decompose", stage=Stage.PLANNING, agent=PlannerAgent(inject=False),
            description="Sequence the change into dependency-ordered tasks.",
            depends_on=["architecture", "impact"],
            exit_gates=[AgentSucceeded(), ProducedOutputs("task_plan")],
            autonomy=AutonomyLevel.ACT_AND_REPORT,
        ),
        Node(
            id="implement", stage=Stage.IMPLEMENTATION,
            agent=ImplementationAgent(change_plan()),
            description="Apply the anchored change plan across the impacted modules.",
            depends_on=["decompose"],
            entry_gates=[ContextKeysPresent("task_plan", "impact_analysis")],
            exit_gates=[
                AgentSucceeded(),
                ProducedOutputs("changed_files"),
                # A change far larger than planned means the plan no longer
                # describes what is happening; stop rather than review a surprise.
                MetricThreshold("files_changed", maximum=12),
            ],
            retry=RetryPolicy(max_attempts=1),
            on_failure=FailureAction.ROLLBACK,
            autonomy=AutonomyLevel.ACT_AND_REPORT,
            policy_tags=["change_control"],
        ),
    ]
    nodes.extend(verification_tail(depends_on=["implement"], min_coverage=55.0, version="1.2.0"))

    return WorkflowGraph(
        name="brownfield",
        description="Add analytics back-pressure visibility and an expired-link purge endpoint.",
        nodes=nodes,
    )


def default_inputs() -> Dict[str, Any]:
    return {
        "requirement": REQUIREMENT.strip(),
        "constraints": list(CONSTRAINTS),
        "change_target": list(CHANGE_TARGET),
        "clarifications": {
            "Q-NFR-AVAILABILITY": "no change to the availability target; purge is operator-initiated",
            "Q-NFR-SCALE": "purge handles up to 100k expired rows in one call",
            "Q-NFR-OBSERVABILITY": "queue depth is a gauge on the existing /metrics endpoint",
        },
    }
