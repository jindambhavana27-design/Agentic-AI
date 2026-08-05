"""Policy guardrails, approval brokers, audit-chain integrity, and metric derivation."""

import json
import os
import shutil
import tempfile
import time
import unittest

from orchestrator.approvals import (
    ApprovalRequest,
    AutoApprovalBroker,
    DeferredApprovalBroker,
    InteractiveApprovalBroker,
    risk_for,
)
from orchestrator.audit import AuditLog, verify_chain, verify_file
from orchestrator.engine import EngineConfig, Orchestrator, _redact
from orchestrator.metrics import compute_metrics
from orchestrator.policy import (
    AutonomyBoundary,
    ChangeBudget,
    DestructiveMigrationControl,
    NoCriticalSecurityFindings,
    NoNewDependencies,
    NoSecretsInArtifacts,
    PolicyEngine,
    ReleaseRequiresEvidence,
    WorkspaceConfinement,
    default_policies,
)
from orchestrator.state import RunState
from orchestrator.types import (
    AgentResult,
    Artifact,
    AutonomyLevel,
    Finding,
    NodeStatus,
    PolicyEffect,
    RunStatus,
    Severity,
    Stage,
)

from .support import FailingAgent, RecordingAgent, graph, node


class _Node:
    """Minimal node stand-in; policies only read a few attributes."""

    def __init__(self, node_id="n", stage=Stage.IMPLEMENTATION,
                 autonomy=AutonomyLevel.ACT_AND_REPORT, requires_approval=False):
        self.id = node_id
        self.stage = stage
        self.autonomy = autonomy
        self.requires_approval = requires_approval
        self.depends_on = []


def result(**kwargs) -> AgentResult:
    return AgentResult(**kwargs)


class SecurityPolicyTests(unittest.TestCase):
    def setUp(self):
        from orchestrator.context import RunContext

        self.context = RunContext()

    def test_aws_key_in_an_artifact_is_denied(self):
        artifact = Artifact("cfg", "code", "a.py", content="KEY = 'AKIAIOSFODNN7EXAMPLE'")
        decision = NoSecretsInArtifacts().post(_Node(), self.context,
                                               result(artifacts=[artifact]))
        self.assertIs(decision.effect, PolicyEffect.DENY)

    def test_private_key_block_is_denied(self):
        artifact = Artifact("key", "code", "k.pem",
                            content="-----BEGIN RSA PRIVATE KEY-----\nabc\n")
        self.assertIs(NoSecretsInArtifacts().post(_Node(), self.context,
                                                  result(artifacts=[artifact])).effect,
                      PolicyEffect.DENY)

    def test_clean_artifact_passes(self):
        artifact = Artifact("ok", "code", "a.py", content="x = 1\n")
        self.assertIsNone(NoSecretsInArtifacts().post(_Node(), self.context,
                                                      result(artifacts=[artifact])))

    def test_critical_security_finding_denies(self):
        finding = Finding(Severity.CRITICAL, "rce", category="security")
        self.assertIs(NoCriticalSecurityFindings().post(_Node(), self.context,
                                                        result(findings=[finding])).effect,
                      PolicyEffect.DENY)

    def test_high_security_finding_escalates_to_a_human(self):
        finding = Finding(Severity.HIGH, "weak crypto", category="security")
        self.assertIs(NoCriticalSecurityFindings().post(_Node(), self.context,
                                                        result(findings=[finding])).effect,
                      PolicyEffect.REQUIRE_APPROVAL)

    def test_non_security_findings_are_ignored_by_the_security_policy(self):
        finding = Finding(Severity.CRITICAL, "ugly code", category="style")
        self.assertIsNone(NoCriticalSecurityFindings().post(_Node(), self.context,
                                                            result(findings=[finding])))

    def test_workspace_escape_is_denied(self):
        workspace = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, workspace, True)
        self.context.put("workspace_root", workspace, "engine")
        artifact = Artifact("evil", "code", "../../etc/passwd", content="x")
        self.assertIs(WorkspaceConfinement().post(_Node(), self.context,
                                                  result(artifacts=[artifact])).effect,
                      PolicyEffect.DENY)

    def test_in_workspace_path_is_allowed(self):
        workspace = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, workspace, True)
        self.context.put("workspace_root", workspace, "engine")
        artifact = Artifact("ok", "code", "service/a.py", content="x")
        self.assertIsNone(WorkspaceConfinement().post(_Node(), self.context,
                                                      result(artifacts=[artifact])))


class ChangeControlPolicyTests(unittest.TestCase):
    def setUp(self):
        from orchestrator.context import RunContext

        self.context = RunContext()

    def test_oversized_change_requires_approval(self):
        decision = ChangeBudget(max_files=2, max_lines=10).post(
            _Node(), self.context, result(metrics={"files_changed": 5, "lines_changed": 200}))
        self.assertIs(decision.effect, PolicyEffect.REQUIRE_APPROVAL)

    def test_change_within_budget_passes(self):
        self.assertIsNone(ChangeBudget(max_files=10, max_lines=500).post(
            _Node(), self.context, result(metrics={"files_changed": 2, "lines_changed": 30})))

    def test_new_dependency_requires_approval(self):
        decision = NoNewDependencies().post(
            _Node(), self.context, result(metrics={"dependencies_added": ["requests"]}))
        self.assertIs(decision.effect, PolicyEffect.REQUIRE_APPROVAL)

    def test_destructive_migration_without_rollback_is_denied(self):
        artifact = Artifact("m1", "schema", "m.sql", content="DROP TABLE links;")
        self.assertIs(DestructiveMigrationControl().post(_Node(), self.context,
                                                         result(artifacts=[artifact])).effect,
                      PolicyEffect.DENY)

    def test_destructive_migration_with_rollback_requires_approval(self):
        artifact = Artifact("m1", "schema", "m.sql", content="DROP TABLE links;")
        decision = DestructiveMigrationControl().post(
            _Node(), self.context,
            result(artifacts=[artifact], outputs={"rollback_script": "CREATE TABLE links..."}))
        self.assertIs(decision.effect, PolicyEffect.REQUIRE_APPROVAL)

    def test_additive_migration_passes(self):
        artifact = Artifact("m1", "schema", "m.sql", content="ALTER TABLE links ADD COLUMN x;")
        self.assertIsNone(DestructiveMigrationControl().post(_Node(), self.context,
                                                             result(artifacts=[artifact])))


class CompliancePolicyTests(unittest.TestCase):
    def setUp(self):
        from orchestrator.context import RunContext

        self.context = RunContext()

    def test_propose_only_node_producing_code_is_denied(self):
        node_obj = _Node(autonomy=AutonomyLevel.PROPOSE_ONLY)
        artifact = Artifact("patch", "code", "a.py", content="x = 1")
        self.assertIs(AutonomyBoundary().post(node_obj, self.context,
                                              result(artifacts=[artifact])).effect,
                      PolicyEffect.DENY)

    def test_propose_only_node_producing_a_document_is_fine(self):
        node_obj = _Node(autonomy=AutonomyLevel.PROPOSE_ONLY)
        artifact = Artifact("plan", "document", "p.md", content="# plan")
        self.assertIsNone(AutonomyBoundary().post(node_obj, self.context,
                                                  result(artifacts=[artifact])))

    def test_act_with_approval_without_a_checkpoint_escalates(self):
        node_obj = _Node(autonomy=AutonomyLevel.ACT_WITH_APPROVAL, requires_approval=False)
        self.assertIs(AutonomyBoundary().pre(node_obj, self.context).effect,
                      PolicyEffect.REQUIRE_APPROVAL)

    def test_release_without_evidence_is_denied(self):
        node_obj = _Node(stage=Stage.RELEASE)
        self.assertIs(ReleaseRequiresEvidence().pre(node_obj, self.context).effect,
                      PolicyEffect.DENY)

    def test_release_with_full_evidence_passes(self):
        node_obj = _Node(stage=Stage.RELEASE)
        for key in ("test_report", "security_report", "documentation_report"):
            self.context.put(key, {"ok": True}, "n")
        self.assertIsNone(ReleaseRequiresEvidence().pre(node_obj, self.context))

    def test_non_release_stage_is_not_checked_for_evidence(self):
        self.assertIsNone(ReleaseRequiresEvidence().pre(_Node(), self.context))


class PolicyEngineTests(unittest.TestCase):
    def setUp(self):
        from orchestrator.context import RunContext

        self.context = RunContext()

    def test_strongest_effect_wins(self):
        engine = PolicyEngine(default_policies())
        artifact = Artifact("bad", "code", "a.py", content="AKIAIOSFODNN7EXAMPLE")
        verdict = engine.evaluate_post(
            _Node(), self.context,
            result(artifacts=[artifact], metrics={"dependencies_added": ["requests"]}))
        self.assertIs(verdict.effect, PolicyEffect.DENY)
        self.assertGreaterEqual(len(verdict.decisions), 2)

    def test_no_decisions_means_allow(self):
        self.assertIs(PolicyEngine([]).evaluate_post(_Node(), self.context, result()).effect,
                      PolicyEffect.ALLOW)

    def test_describe_lists_every_policy(self):
        described = PolicyEngine().describe()
        self.assertTrue(any(d["id"] == "SEC-001" for d in described))
        self.assertTrue(all({"id", "category", "description"} <= set(d) for d in described))

    def test_a_denying_policy_stops_the_node_in_a_real_run(self):
        secret = Artifact("leak", "code", "a.py", content="AKIAIOSFODNN7EXAMPLE")
        g = graph(node("a", RecordingAgent("a", artifacts=[secret])))
        report = Orchestrator(g, EngineConfig(artifacts_dir=tempfile.mkdtemp())).run({})
        self.assertIs(report.status, RunStatus.FAILED)
        self.assertIn("denied by policy", report.state.node("a").error)
        self.assertGreaterEqual(report.metrics.policy_denials, 1)


class ApprovalBrokerTests(unittest.TestCase):
    def request(self, risk=Severity.MEDIUM, node_id="n"):
        return ApprovalRequest(run_id="r1", node_id=node_id, stage="implementation",
                               summary="do the thing", risk=risk)

    def test_auto_broker_approves_at_the_ceiling(self):
        broker = AutoApprovalBroker(max_auto_risk=Severity.MEDIUM)
        self.assertTrue(broker.request(self.request(Severity.MEDIUM)).approved)

    def test_auto_broker_approves_below_the_ceiling(self):
        broker = AutoApprovalBroker(max_auto_risk=Severity.HIGH)
        self.assertTrue(broker.request(self.request(Severity.LOW)).approved)

    def test_auto_broker_refuses_above_the_ceiling(self):
        broker = AutoApprovalBroker(max_auto_risk=Severity.MEDIUM)
        self.assertFalse(broker.request(self.request(Severity.CRITICAL)).approved)

    def test_auto_broker_logs_its_decisions(self):
        broker = AutoApprovalBroker()
        broker.request(self.request())
        self.assertEqual(len(broker.log), 1)

    def test_interactive_broker_denies_without_a_tty(self):
        # An unattended pipeline must never be able to approve its own changes.
        import contextlib
        import io

        broker = InteractiveApprovalBroker(stream=io.StringIO("y\n"))
        with contextlib.redirect_stderr(io.StringIO()):
            decision = broker.request(self.request())
        self.assertFalse(decision.approved)

    def test_deferred_broker_persists_and_resolves(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        store = os.path.join(directory, "approvals.json")
        broker = DeferredApprovalBroker(store)

        first = broker.request(self.request())
        self.assertTrue(first.pending)
        self.assertEqual(len(broker.pending()), 1)

        broker.decide("r1", "n", True, decided_by="alice", reason="looks fine")
        second = broker.request(self.request())
        self.assertTrue(second.approved)
        self.assertEqual(second.decided_by, "alice")
        self.assertEqual(broker.pending(), [])

    def test_deciding_an_unknown_request_raises(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        broker = DeferredApprovalBroker(os.path.join(directory, "a.json"))
        with self.assertRaises(KeyError):
            broker.decide("nope", "nope", True)

    def test_risk_for_escalates_a_release_stage(self):
        class N:
            stage = Stage.RELEASE

        self.assertIs(risk_for(N(), []), Severity.HIGH)

    def test_risk_for_escalates_credential_language(self):
        class N:
            stage = Stage.IMPLEMENTATION

        self.assertIs(risk_for(N(), ["credential-shaped content found"]), Severity.CRITICAL)


class AuditTests(unittest.TestCase):
    def test_chain_is_valid_for_an_untouched_log(self):
        log = AuditLog(run_id="r1")
        for index in range(5):
            log.record("event", node_id="n%d" % index)
        self.assertTrue(log.verify().valid)

    def test_each_event_links_to_its_predecessor(self):
        log = AuditLog(run_id="r1")
        first = log.record("a")
        second = log.record("b")
        self.assertEqual(second.prev_hash, first.hash)

    def test_tampering_with_a_payload_breaks_the_chain(self):
        log = AuditLog(run_id="r1")
        log.record("a", detail="original")
        log.record("b")
        log.record("c")
        events = log.events()
        events[0].payload["detail"] = "rewritten"
        outcome = verify_chain(events)
        self.assertFalse(outcome.valid)
        self.assertEqual(outcome.broken_at, 1)

    def test_deleting_an_event_breaks_the_chain(self):
        log = AuditLog(run_id="r1")
        for index in range(4):
            log.record("e%d" % index)
        events = log.events()
        del events[1]
        self.assertFalse(verify_chain(events).valid)

    def test_file_backed_log_verifies_from_disk(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "audit.jsonl")
        log = AuditLog(path, run_id="r1")
        for index in range(3):
            log.record("e%d" % index, value=index)
        outcome = verify_file(path)
        self.assertTrue(outcome.valid)
        self.assertEqual(outcome.checked, 3)

    def test_file_verification_detects_an_edit(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "audit.jsonl")
        log = AuditLog(path, run_id="r1")
        log.record("a", value=1)
        log.record("b", value=2)
        with open(path) as handle:
            lines = handle.read().splitlines()
        first = json.loads(lines[0])
        first["payload"]["value"] = 999
        with open(path, "w") as handle:
            handle.write(json.dumps(first) + "\n" + lines[1] + "\n")
        self.assertFalse(verify_file(path).valid)

    def test_interleaved_runs_are_verified_independently(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "audit.jsonl")
        one, two = AuditLog(path, run_id="r1"), AuditLog(path, run_id="r2")
        one.record("a")
        two.record("a")
        one.record("b")
        two.record("b")
        self.assertTrue(verify_file(path).valid)

    def test_a_real_run_produces_a_valid_chain(self):
        g = graph(node("a", RecordingAgent("a")), node("b", RecordingAgent("b"),
                                                       depends_on=["a"]))
        report = Orchestrator(g, EngineConfig(artifacts_dir=tempfile.mkdtemp())).run({})
        self.assertTrue(report.audit.verify().valid)
        self.assertGreater(len(report.audit.events()), 5)


class RedactionTests(unittest.TestCase):
    def test_credential_shaped_keys_are_redacted(self):
        payload = {"api_key": "secret-value", "url": "https://example.com",
                   "nested": {"password": "hunter2", "port": 8080}}
        clean = _redact(payload)
        self.assertEqual(clean["api_key"], "<redacted>")
        self.assertEqual(clean["nested"]["password"], "<redacted>")
        self.assertEqual(clean["url"], "https://example.com")
        self.assertEqual(clean["nested"]["port"], 8080)


class MetricsTests(unittest.TestCase):
    def test_gate_blocked_node_is_counted_as_attempted(self):
        # Otherwise a run that failed at a gate reports a 100% success rate.
        from orchestrator.gates import ContextKeysPresent

        g = graph(
            node("ok", RecordingAgent("ok")),
            node("blocked", RecordingAgent("blocked"), depends_on=["ok"],
                 entry_gates=[ContextKeysPresent("never_set")]),
        )
        report = Orchestrator(g, EngineConfig(artifacts_dir=tempfile.mkdtemp())).run({})
        self.assertEqual(report.metrics.nodes_attempted, 2)
        self.assertEqual(report.metrics.success_rate, 0.5)

    def test_replan_reruns_are_not_counted_as_retries(self):
        from orchestrator.replan import Replanner

        from .support import ReadingAgent, WritingAgent

        g = graph(
            node("seed", WritingAgent("seed", "spec", ["v1"])),
            node("reader", ReadingAgent("reader", "spec"), depends_on=["seed"]),
            node("late", WritingAgent("late", "spec", ["v2"]), depends_on=["seed"]),
        )
        report = Orchestrator(g, EngineConfig(max_parallelism=1,
                                              artifacts_dir=tempfile.mkdtemp()),
                              replanner=Replanner(3)).run({})
        self.assertGreaterEqual(report.metrics.replan_count, 1)
        self.assertEqual(report.metrics.total_retries, 0)

    def test_approval_wait_is_excluded_from_execution_time(self):
        state = RunState("r1", "w")
        state.emit("run_started")
        state.emit("attempt_started", node_id="a", attempt=1)
        time.sleep(0.05)
        state.emit("approval_resolved", node_id="a", decision=True,
                   decided_by="op", waited_seconds=10.0)
        state.emit("node_succeeded", node_id="a", result={})
        state.emit("run_finished", status="succeeded")
        metrics = compute_metrics(state)
        self.assertEqual(metrics.approval_wait_seconds, 10.0)
        self.assertLess(metrics.execution_seconds, 1.0)

    def test_unrecovered_failures_are_reported_separately_from_mttr(self):
        g = graph(node("a", FailingAgent("a")))
        report = Orchestrator(g, EngineConfig(artifacts_dir=tempfile.mkdtemp())).run({})
        self.assertIsNone(report.metrics.mttr_seconds)
        self.assertEqual(report.metrics.unrecovered_failures, 1)

    def test_metrics_render_without_error(self):
        g = graph(node("a", RecordingAgent("a")))
        report = Orchestrator(g, EngineConfig(artifacts_dir=tempfile.mkdtemp())).run({})
        rendered = report.metrics.render()
        self.assertIn("success rate", rendered)
        self.assertIn("MTTR", rendered)

    def test_state_is_reconstructible_from_its_journal(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        journal = os.path.join(directory, "journal.jsonl")
        g = graph(node("a", RecordingAgent("a")), node("b", RecordingAgent("b"),
                                                       depends_on=["a"]))
        report = Orchestrator(g, EngineConfig(journal_path=journal,
                                              artifacts_dir=directory)).run({})

        rebuilt = RunState.from_journal(journal, report.run_id, "test")
        self.assertIs(rebuilt.status, RunStatus.SUCCEEDED)
        self.assertIs(rebuilt.nodes["a"].status, NodeStatus.SUCCEEDED)
        self.assertEqual(rebuilt.status_map(), report.state.status_map())


if __name__ == "__main__":
    unittest.main()
