"""Engine behaviour: scheduling, gates, retries, failure paths, approvals, re-planning."""

import os
import shutil
import tempfile
import time
import unittest

from orchestrator.approvals import (
    AutoApprovalBroker,
    DeferredApprovalBroker,
    StaticApprovalBroker,
)
from orchestrator.engine import EngineConfig, Orchestrator
from orchestrator.gates import (
    AgentSucceeded,
    AllUpstreamSucceeded,
    ContextKeysPresent,
    MetricThreshold,
    NoFindingsAtOrAbove,
    PredicateGate,
    ProducedOutputs,
)
from orchestrator.policy import PolicyEngine
from orchestrator.replan import Replanner
from orchestrator.types import (
    Artifact,
    AutonomyLevel,
    FailureAction,
    Finding,
    NodeStatus,
    RetryPolicy,
    RunStatus,
    Severity,
    Stage,
)

from .support import (
    FailingAgent,
    NeedsInputAgent,
    RaisingAgent,
    ReadingAgent,
    RecordingAgent,
    WritingAgent,
    graph,
    node,
)


def run(g, **kwargs):
    inputs = kwargs.pop("inputs", None)
    config = kwargs.pop("config", None) or EngineConfig(max_parallelism=4)
    engine = Orchestrator(g, config, **kwargs)
    return engine.run(inputs or {}), engine


class SchedulingTests(unittest.TestCase):
    def test_linear_chain_runs_in_order(self):
        log = []
        g = graph(
            node("a", RecordingAgent("a", log=log)),
            node("b", RecordingAgent("b", log=log), depends_on=["a"]),
            node("c", RecordingAgent("c", log=log), depends_on=["b"]),
        )
        report, _ = run(g)
        self.assertIs(report.status, RunStatus.SUCCEEDED)
        self.assertEqual(log, ["a:start", "a:end", "b:start", "b:end", "c:start", "c:end"])

    def test_independent_branches_overlap_in_time(self):
        left = RecordingAgent("left", delay=0.25)
        right = RecordingAgent("right", delay=0.25)
        g = graph(
            node("root", RecordingAgent("root")),
            node("left", left, depends_on=["root"]),
            node("right", right, depends_on=["root"]),
            node("join", RecordingAgent("join"), depends_on=["left", "right"]),
        )
        report, _ = run(g)
        self.assertIs(report.status, RunStatus.SUCCEEDED)
        # Genuine overlap, not just "both ran".
        (l_start, l_end), (r_start, r_end) = left.windows[0], right.windows[0]
        self.assertLess(max(l_start, r_start), min(l_end, r_end))
        self.assertLess(report.metrics.e2e_latency_seconds, 0.45)

    def test_join_waits_for_every_parent(self):
        log = []
        g = graph(
            node("a", RecordingAgent("a", log=log, delay=0.15)),
            node("b", RecordingAgent("b", log=log)),
            node("join", RecordingAgent("join", log=log), depends_on=["a", "b"]),
        )
        run(g)
        self.assertLess(log.index("a:end"), log.index("join:start"))
        self.assertLess(log.index("b:end"), log.index("join:start"))

    def test_parallelism_limit_is_respected(self):
        agents = [RecordingAgent("n%d" % i, delay=0.1) for i in range(4)]
        g = graph(*[node("n%d" % i, agents[i]) for i in range(4)])
        report, _ = run(g, config=EngineConfig(max_parallelism=1))
        self.assertIs(report.status, RunStatus.SUCCEEDED)
        # Serialised: total wall clock must exceed the sum of the delays.
        self.assertGreaterEqual(report.metrics.e2e_latency_seconds, 0.35)

    def test_outputs_flow_into_downstream_context(self):
        reader = ReadingAgent("reader", "shared")
        g = graph(
            node("writer", WritingAgent("writer", "shared", ["value-1"])),
            node("reader", reader, depends_on=["writer"]),
        )
        run(g)
        self.assertEqual(reader.observed, ["value-1"])


class GateTests(unittest.TestCase):
    def test_failing_exit_gate_fails_the_node(self):
        g = graph(node("a", RecordingAgent("a"),
                       exit_gates=[ProducedOutputs("never_produced")]))
        report, _ = run(g)
        self.assertIs(report.status, RunStatus.FAILED)
        self.assertIs(report.state.node("a").status, NodeStatus.FAILED)

    def test_metric_threshold_floor(self):
        g = graph(node("a", RecordingAgent("a", metrics={"coverage": 40.0}),
                       exit_gates=[MetricThreshold("coverage", minimum=60.0)]))
        report, _ = run(g)
        self.assertIs(report.status, RunStatus.FAILED)

    def test_metric_threshold_cap(self):
        g = graph(node("a", RecordingAgent("a", metrics={"drift": 3}),
                       exit_gates=[MetricThreshold("drift", maximum=0)]))
        self.assertIs(run(g)[0].status, RunStatus.FAILED)

    def test_severity_ceiling_gate(self):
        agent = RecordingAgent("a", findings=[Finding(Severity.HIGH, "bad thing")])
        g = graph(node("a", agent, exit_gates=[NoFindingsAtOrAbove(Severity.HIGH)]))
        self.assertIs(run(g)[0].status, RunStatus.FAILED)

    def test_non_blocking_entry_gate_skips_the_node(self):
        g = graph(
            node("a", RecordingAgent("a")),
            node("b", RecordingAgent("b"), depends_on=["a"],
                 entry_gates=[PredicateGate("never", lambda _c: False)]),
        )
        report, _ = run(g)
        self.assertIs(report.state.node("b").status, NodeStatus.SKIPPED)
        self.assertIs(report.status, RunStatus.SUCCEEDED)

    def test_blocking_entry_gate_fails_the_node(self):
        g = graph(node("a", RecordingAgent("a"),
                       entry_gates=[ContextKeysPresent("absent_key")]))
        report, _ = run(g)
        self.assertIs(report.state.node("a").status, NodeStatus.FAILED)
        self.assertIs(report.status, RunStatus.FAILED)

    def test_all_upstream_succeeded_rejects_a_skipped_parent(self):
        # A skipped scan is not a passed scan.
        g = graph(
            node("a", RecordingAgent("a")),
            node("optional", RecordingAgent("optional"), depends_on=["a"],
                 entry_gates=[PredicateGate("never", lambda _c: False)]),
            node("release", RecordingAgent("release"), depends_on=["optional"],
                 stage=Stage.RELEASE, entry_gates=[AllUpstreamSucceeded()]),
        )
        report, _ = run(g, policy_engine=PolicyEngine([]))
        self.assertIs(report.state.node("optional").status, NodeStatus.SKIPPED)
        self.assertIs(report.state.node("release").status, NodeStatus.FAILED)

    def test_gate_results_are_recorded_for_audit(self):
        g = graph(node("a", RecordingAgent("a"), exit_gates=[AgentSucceeded()]))
        report, _ = run(g)
        self.assertEqual(len(report.state.node("a").exit_gates), 1)
        self.assertTrue(report.state.node("a").exit_gates[0]["passed"])


class FailureAndRetryTests(unittest.TestCase):
    def test_retry_then_succeed(self):
        agent = FailingAgent("flaky", fail_times=2)
        g = graph(node("a", agent, retry=RetryPolicy(max_attempts=3, backoff_seconds=0.01)))
        report, _ = run(g)
        self.assertIs(report.status, RunStatus.SUCCEEDED)
        self.assertEqual(agent.calls, 3)
        self.assertEqual(report.state.node("a").retry_count, 2)

    def test_retries_are_bounded(self):
        agent = FailingAgent("always", fail_times=None)
        g = graph(node("a", agent, retry=RetryPolicy(max_attempts=2, backoff_seconds=0.01)))
        report, _ = run(g)
        self.assertIs(report.status, RunStatus.FAILED)
        self.assertEqual(agent.calls, 2)

    def test_backoff_delays_the_next_attempt(self):
        agent = FailingAgent("flaky", fail_times=1)
        g = graph(node("a", agent, retry=RetryPolicy(max_attempts=2, backoff_seconds=0.3)))
        report, _ = run(g)
        self.assertIs(report.status, RunStatus.SUCCEEDED)
        self.assertGreaterEqual(report.metrics.e2e_latency_seconds, 0.3)

    def test_mttr_is_measured_for_a_recovered_node(self):
        g = graph(node("a", FailingAgent("flaky", fail_times=1),
                       retry=RetryPolicy(max_attempts=2, backoff_seconds=0.05)))
        report, _ = run(g)
        self.assertIsNotNone(report.metrics.mttr_seconds)
        self.assertGreater(report.metrics.mttr_seconds, 0.0)

    def test_an_exception_inside_an_agent_is_a_failure_not_a_crash(self):
        g = graph(node("a", RaisingAgent()))
        report, _ = run(g)
        self.assertIs(report.status, RunStatus.FAILED)
        self.assertIn("agent exploded", report.state.node("a").error)

    def test_continue_lets_dependents_proceed(self):
        g = graph(
            node("optional", FailingAgent("optional"), on_failure=FailureAction.CONTINUE),
            node("after", RecordingAgent("after"), depends_on=["optional"]),
        )
        report, _ = run(g)
        self.assertIs(report.state.node("optional").status, NodeStatus.FAILED)
        self.assertIs(report.state.node("after").status, NodeStatus.SUCCEEDED)

    def test_fail_run_stops_downstream_work(self):
        downstream = RecordingAgent("downstream")
        g = graph(
            node("a", FailingAgent("a"), on_failure=FailureAction.FAIL_RUN),
            node("b", downstream, depends_on=["a"]),
        )
        report, _ = run(g)
        self.assertIs(report.status, RunStatus.FAILED)
        self.assertEqual(downstream.calls, 0)

    def test_safe_stop_freezes_pending_nodes(self):
        g = graph(
            node("a", FailingAgent("a"), on_failure=FailureAction.SAFE_STOP),
            node("b", RecordingAgent("b"), depends_on=["a"]),
        )
        report, _ = run(g)
        self.assertIs(report.status, RunStatus.HALTED)
        self.assertIs(report.state.node("b").status, NodeStatus.HALTED)

    def test_consecutive_failures_trip_the_safe_stop(self):
        g = graph(
            node("a", FailingAgent("a"), on_failure=FailureAction.CONTINUE),
            node("b", FailingAgent("b"), on_failure=FailureAction.CONTINUE),
            node("c", FailingAgent("c"), on_failure=FailureAction.FAIL_RUN),
        )
        report, _ = run(g, config=EngineConfig(max_parallelism=1,
                                               safe_stop_after_consecutive_failures=3))
        self.assertIs(report.status, RunStatus.HALTED)
        self.assertIn("consecutive", report.state.failure_reason)

    def test_fallback_agent_is_engaged(self):
        g = graph(node("a", FailingAgent("primary"),
                       fallback_agent=RecordingAgent("backup"),
                       on_failure=FailureAction.FALLBACK))
        report, _ = run(g)
        self.assertIs(report.status, RunStatus.SUCCEEDED)

    def test_timeout_fails_the_node(self):
        g = graph(node("slow", RecordingAgent("slow", delay=2.0), timeout_seconds=0.2))
        report, _ = run(g)
        self.assertIs(report.status, RunStatus.FAILED)
        self.assertIn("timed out", report.state.node("slow").error)


class RollbackTests(unittest.TestCase):
    def test_compensations_run_in_reverse_order(self):
        undone = []
        g = graph(
            node("first", RecordingAgent("first"), on_failure=FailureAction.ROLLBACK,
                 compensation=lambda _c: undone.append("first")),
            node("second", RecordingAgent("second"), depends_on=["first"],
                 on_failure=FailureAction.ROLLBACK,
                 compensation=lambda _c: undone.append("second")),
            node("boom", FailingAgent("boom"), depends_on=["second"],
                 on_failure=FailureAction.ROLLBACK),
        )
        report, _ = run(g)
        self.assertIs(report.status, RunStatus.ROLLED_BACK)
        self.assertEqual(undone, ["second", "first"])

    def test_a_failing_compensation_does_not_stop_the_others(self):
        undone = []

        def explode(_ctx):
            raise RuntimeError("compensation failed")

        g = graph(
            node("first", RecordingAgent("first"), on_failure=FailureAction.ROLLBACK,
                 compensation=lambda _c: undone.append("first")),
            node("second", RecordingAgent("second"), depends_on=["first"],
                 on_failure=FailureAction.ROLLBACK, compensation=explode),
            node("boom", FailingAgent("boom"), depends_on=["second"],
                 on_failure=FailureAction.ROLLBACK),
        )
        report, _ = run(g)
        self.assertEqual(undone, ["first"])
        self.assertFalse(report.rollbacks[0].clean)

    def test_workspace_snapshot_is_restored(self):
        workspace = tempfile.mkdtemp(prefix="orch-test-ws-")
        self.addCleanup(shutil.rmtree, workspace, True)
        with open(os.path.join(workspace, "keep.txt"), "w") as handle:
            handle.write("original")

        def mutate(ctx):
            with open(os.path.join(workspace, "keep.txt"), "w") as handle:
                handle.write("modified")
            with open(os.path.join(workspace, "added.txt"), "w") as handle:
                handle.write("new")
            return RecordingAgent("mutator").execute(ctx)

        from .support import CallableAgentAdapter

        g = graph(
            node("mutate", CallableAgentAdapter("mutate", mutate),
                 on_failure=FailureAction.ROLLBACK),
            node("boom", FailingAgent("boom"), depends_on=["mutate"],
                 on_failure=FailureAction.ROLLBACK),
        )
        config = EngineConfig(workspace=workspace, snapshot_workspace=True,
                              artifacts_dir=tempfile.mkdtemp())
        report, _ = run(g, config=config)
        self.assertIs(report.status, RunStatus.ROLLED_BACK)
        with open(os.path.join(workspace, "keep.txt")) as handle:
            self.assertEqual(handle.read(), "original")
        self.assertFalse(os.path.exists(os.path.join(workspace, "added.txt")))


class ApprovalTests(unittest.TestCase):
    def test_approved_node_proceeds(self):
        g = graph(node("a", RecordingAgent("a"), requires_approval=True))
        report, _ = run(g, approval_broker=StaticApprovalBroker({"a": True}))
        self.assertIs(report.status, RunStatus.SUCCEEDED)

    def test_rejected_node_fails_and_blocks_downstream(self):
        downstream = RecordingAgent("b")
        g = graph(
            node("a", RecordingAgent("a"), requires_approval=True),
            node("b", downstream, depends_on=["a"]),
        )
        report, _ = run(g, approval_broker=StaticApprovalBroker({"a": False}))
        self.assertIs(report.status, RunStatus.FAILED)
        self.assertEqual(downstream.calls, 0)

    def test_pending_decision_pauses_the_run(self):
        g = graph(node("a", RecordingAgent("a"), requires_approval=True))
        report, _ = run(g, approval_broker=StaticApprovalBroker({}, default=None))
        self.assertIs(report.status, RunStatus.AWAITING_APPROVAL)
        self.assertTrue(report.resumable)
        self.assertEqual(len(report.pending_approvals), 1)

    def test_auto_broker_refuses_above_its_ceiling(self):
        # A release must not be approved by automation at the default ceiling.
        g = graph(node("release", RecordingAgent("release"), stage=Stage.RELEASE,
                       requires_approval=True))
        report, _ = run(g, approval_broker=AutoApprovalBroker(max_auto_risk=Severity.MEDIUM),
                        policy_engine=PolicyEngine([]))
        self.assertIs(report.status, RunStatus.FAILED)

    def test_auto_broker_approves_within_its_ceiling(self):
        g = graph(node("release", RecordingAgent("release"), stage=Stage.RELEASE,
                       requires_approval=True))
        report, _ = run(g, approval_broker=AutoApprovalBroker(max_auto_risk=Severity.HIGH),
                        policy_engine=PolicyEngine([]))
        self.assertIs(report.status, RunStatus.SUCCEEDED)

    def test_needs_input_escalates_rather_than_failing_silently(self):
        agent = NeedsInputAgent(["what does 'fast' mean?"])
        g = graph(node("a", agent))
        report, _ = run(g, approval_broker=StaticApprovalBroker({"a": False}))
        self.assertIs(report.status, RunStatus.FAILED)
        self.assertIn("clarification denied", report.state.node("a").error)

    def test_needs_input_accepted_records_the_assumptions(self):
        g = graph(node("a", NeedsInputAgent(["assume p99 < 50ms"])))
        report, _ = run(g, approval_broker=StaticApprovalBroker({"a": True}))
        self.assertIs(report.status, RunStatus.SUCCEEDED)
        self.assertIn("assume p99 < 50ms", report.context.get("resolved_assumptions"))

    def test_deferred_broker_round_trip(self):
        directory = tempfile.mkdtemp(prefix="orch-approvals-")
        self.addCleanup(shutil.rmtree, directory, True)
        store = os.path.join(directory, "approvals.json")
        journal = os.path.join(directory, "journal.jsonl")

        def build():
            return graph(
                node("a", RecordingAgent("a")),
                node("gate", RecordingAgent("gate"), depends_on=["a"], requires_approval=True),
            )

        config = EngineConfig(journal_path=journal, artifacts_dir=directory)
        first = Orchestrator(build(), config, approval_broker=DeferredApprovalBroker(store))
        report = first.run({})
        self.assertIs(report.status, RunStatus.AWAITING_APPROVAL)

        broker = DeferredApprovalBroker(store)
        self.assertEqual(len(broker.pending()), 1)
        broker.decide(report.run_id, "gate", True, decided_by="operator")

        second = Orchestrator(build(), config, approval_broker=DeferredApprovalBroker(store))
        resumed = second.run({}, run_id=report.run_id, resume=True)
        self.assertIs(resumed.status, RunStatus.SUCCEEDED)
        self.assertEqual(resumed.metrics.approvals_pending, 0)
        self.assertEqual(resumed.metrics.approvals_granted, 1)


class ReplanTests(unittest.TestCase):
    def test_downstream_node_reruns_when_its_input_changes(self):
        reader = ReadingAgent("reader", "spec")
        late = WritingAgent("late", "spec", ["v2"])
        g = graph(
            node("seed", WritingAgent("seed", "spec", ["v1"])),
            node("reader", reader, depends_on=["seed"]),
            node("late", late, depends_on=["seed"]),
        )
        # Serial execution guarantees `reader` completes before `late` rewrites.
        report, _ = run(g, config=EngineConfig(max_parallelism=1),
                        replanner=Replanner(max_replans_per_node=3))
        self.assertIs(report.status, RunStatus.SUCCEEDED)
        self.assertEqual(reader.observed, ["v1", "v2"])
        self.assertGreaterEqual(report.metrics.replan_count, 1)

    def test_identical_rewrite_triggers_no_replan(self):
        reader = ReadingAgent("reader", "spec")
        g = graph(
            node("seed", WritingAgent("seed", "spec", ["same"])),
            node("reader", reader, depends_on=["seed"]),
            node("rewriter", WritingAgent("rewriter", "spec", ["same"]), depends_on=["seed"]),
        )
        report, _ = run(g, config=EngineConfig(max_parallelism=1))
        self.assertEqual(reader.observed, ["same"])
        self.assertEqual(report.metrics.replan_count, 0)

    def test_replan_budget_halts_rather_than_looping(self):
        # A writer that changes the value every time can never converge.
        reader = ReadingAgent("reader", "spec")
        g = graph(
            node("seed", WritingAgent("seed", "spec", ["v1"])),
            node("reader", reader, depends_on=["seed"]),
            node("churn", WritingAgent("churn", "spec", ["v2", "v3", "v4", "v5"]),
                 depends_on=["seed"]),
        )
        report, _ = run(g, config=EngineConfig(max_parallelism=1),
                        replanner=Replanner(max_replans_per_node=1))
        self.assertIn(report.status, (RunStatus.HALTED, RunStatus.SUCCEEDED))
        self.assertLessEqual(report.state.node("reader").replan_count, 1)

    def test_replan_insensitive_node_is_left_alone(self):
        reader = ReadingAgent("reader", "spec")
        g = graph(
            node("seed", WritingAgent("seed", "spec", ["v1"])),
            node("reader", reader, depends_on=["seed"], replan_sensitive=False),
            node("late", WritingAgent("late", "spec", ["v2"]), depends_on=["seed"]),
        )
        report, _ = run(g, config=EngineConfig(max_parallelism=1))
        self.assertEqual(reader.observed, ["v1"])
        self.assertEqual(report.metrics.replan_count, 0)


class GraphExpansionAtRuntimeTests(unittest.TestCase):
    def test_planner_injects_nodes_and_a_barrier(self):
        from orchestrator.agents.base import Agent
        from orchestrator.types import AgentResult

        executed = []

        class Injector(Agent):
            name = "injector"

            def execute(self, ctx):
                result = self.ok(outputs={"planned": True})
                result.proposed_nodes = [
                    node("task-1", RecordingAgent("task-1", log=executed), depends_on=["plan"]),
                    node("task-2", RecordingAgent("task-2", log=executed), depends_on=["plan"]),
                ]
                result.rewire = {"build": ["task-1", "task-2"]}
                return result

        build_log = []
        g = graph(
            node("plan", Injector()),
            node("build", RecordingAgent("build", log=build_log), depends_on=["plan"]),
        )
        report, _ = run(g)
        self.assertIs(report.status, RunStatus.SUCCEEDED)
        self.assertIn("task-1", report.graph)
        self.assertIn("task-1", report.graph.get("build").depends_on)
        self.assertIs(report.state.node("task-2").status, NodeStatus.SUCCEEDED)


if __name__ == "__main__":
    unittest.main()
