"""Scenario workflows: graph shape, workspace isolation, and a full ambiguous run.

The greenfield and brownfield graphs are validated structurally rather than
executed here -- their test nodes run the entire service suite under coverage,
which belongs in ``python3 -m orchestrator run``, not in a unit test. The
ambiguous scenario *is* executed end to end because it is fast and because its
re-planning behaviour is the part most worth regression-testing.
"""

import os
import shutil
import tempfile
import unittest

from orchestrator.approvals import AutoApprovalBroker, StaticApprovalBroker
from orchestrator.engine import EngineConfig, Orchestrator
from orchestrator.replan import Replanner
from orchestrator.types import NodeStatus, RunStatus, Severity, Stage
from orchestrator.workflows import REGISTRY, ambiguous, brownfield, greenfield
from orchestrator.workflows.common import WORKSPACE_CONTENTS, prepare_workspace


class WorkspaceIsolationTests(unittest.TestCase):
    def test_workspace_contains_the_declared_entries(self):
        workspace = prepare_workspace()
        self.addCleanup(shutil.rmtree, workspace, True)
        for entry in WORKSPACE_CONTENTS:
            self.assertTrue(os.path.exists(os.path.join(workspace, entry)), entry)

    def test_orchestrator_source_is_not_copied_into_the_workspace(self):
        # Agents must not be able to reach the engine that is running them.
        workspace = prepare_workspace()
        self.addCleanup(shutil.rmtree, workspace, True)
        self.assertFalse(os.path.exists(os.path.join(workspace, "orchestrator")))

    def test_each_workspace_is_independent(self):
        first = prepare_workspace()
        second = prepare_workspace()
        self.addCleanup(shutil.rmtree, first, True)
        self.addCleanup(shutil.rmtree, second, True)
        with open(os.path.join(first, "service", "marker.txt"), "w") as handle:
            handle.write("x")
        self.assertFalse(os.path.exists(os.path.join(second, "service", "marker.txt")))


class GraphShapeTests(unittest.TestCase):
    def test_every_registered_scenario_builds_a_valid_graph(self):
        for name, module in REGISTRY.items():
            inputs = (module.default_inputs(with_clarifications=True)
                      if name == "ambiguous" else module.default_inputs())
            graph = module.build(inputs)
            graph.validate()
            self.assertGreater(len(graph), 0, name)

    def test_greenfield_has_a_parallel_verification_tail(self):
        graph = greenfield.build(greenfield.default_inputs())
        levels = graph.topological_levels()
        self.assertTrue(any(len(level) > 1 for level in levels),
                        "verification stages should run in parallel")

    def test_greenfield_release_requires_approval(self):
        graph = greenfield.build(greenfield.default_inputs())
        self.assertTrue(graph.get("release").requires_approval)

    def test_greenfield_implementation_can_roll_back(self):
        graph = greenfield.build(greenfield.default_inputs())
        self.assertEqual(graph.get("implement").on_failure.value, "rollback")

    def test_brownfield_analyses_before_designing(self):
        graph = brownfield.build(brownfield.default_inputs())
        self.assertIn("impact", graph.ancestors("architecture"))
        self.assertIn("impact", graph.ancestors("implement"))

    def test_brownfield_change_plan_touches_multiple_modules(self):
        paths = {c.path for c in brownfield.change_plan().changes}
        self.assertIn("service/app.py", paths)
        self.assertIn("service/storage/base.py", paths)
        self.assertGreaterEqual(len(paths), 5)

    def test_brownfield_fault_injection_raises_the_attempt_budget(self):
        graph = brownfield.build({**brownfield.default_inputs(), "inject_fault": True})
        self.assertGreater(graph.get("impact").retry.max_attempts, 1)

    def test_ambiguous_nodes_are_all_propose_only(self):
        # The right output for an ambiguous requirement is a plan, not code.
        graph = ambiguous.build(ambiguous.default_inputs())
        for node in graph:
            self.assertEqual(node.autonomy.name, "PROPOSE_ONLY", node.id)

    def test_ambiguous_review_runs_beside_architecture(self):
        graph = ambiguous.build(ambiguous.default_inputs())
        self.assertNotIn("stakeholder-review", graph.ancestors("architecture"))
        self.assertNotIn("architecture", graph.ancestors("stakeholder-review"))

    def test_release_barrier_waits_for_all_verification(self):
        graph = greenfield.build(greenfield.default_inputs())
        ancestors = graph.ancestors("release")
        for required in ("test", "security", "docs-verify", "implement"):
            self.assertIn(required, ancestors)


class AmbiguousScenarioTests(unittest.TestCase):
    def run_scenario(self, with_clarifications=True, broker=None):
        graph = ambiguous.build({})
        config = EngineConfig(max_parallelism=4, artifacts_dir=tempfile.mkdtemp(),
                              max_replans_per_node=3)
        engine = Orchestrator(
            graph, config,
            approval_broker=broker or AutoApprovalBroker(max_auto_risk=Severity.HIGH),
            replanner=Replanner(3),
        )
        return engine.run(ambiguous.default_inputs(with_clarifications=with_clarifications))

    def test_without_clarifications_the_run_refuses_to_proceed(self):
        report = self.run_scenario(with_clarifications=False,
                                   broker=StaticApprovalBroker({}, default=False))
        self.assertIsNot(report.status, RunStatus.SUCCEEDED)
        self.assertIs(report.state.node("requirements").status, NodeStatus.FAILED)

    def test_without_clarifications_nothing_downstream_runs(self):
        report = self.run_scenario(with_clarifications=False,
                                   broker=StaticApprovalBroker({}, default=False))
        for node_id in ("architecture", "decompose", "scope-proposal"):
            self.assertIsNot(report.state.node(node_id).status, NodeStatus.SUCCEEDED)

    def test_with_clarifications_the_run_completes(self):
        report = self.run_scenario()
        self.assertIs(report.status, RunStatus.SUCCEEDED)

    def test_a_late_requirement_change_forces_a_replan(self):
        report = self.run_scenario()
        self.assertGreaterEqual(report.metrics.replan_count, 1)
        self.assertGreaterEqual(report.state.node("architecture").replan_count, 1)

    def test_replanning_converges_within_budget(self):
        report = self.run_scenario()
        for node_state in report.state.nodes.values():
            self.assertLessEqual(node_state.replan_count, 3, node_state.node_id)

    def test_the_derived_requirement_reaches_the_final_proposal(self):
        report = self.run_scenario()
        requirements = report.context.get("requirements")
        self.assertIn(ambiguous.DERIVED_REQUIREMENT["id"],
                      [r["id"] for r in requirements])

    def test_the_proposal_is_gated_on_a_human_decision(self):
        report = self.run_scenario()
        approvals = [a for a in report.state.approvals if a["node_id"] == "scope-proposal"]
        self.assertEqual(len(approvals), 1)
        self.assertTrue(approvals[0]["decision"])

    def test_a_rejected_proposal_fails_the_run(self):
        report = self.run_scenario(broker=StaticApprovalBroker({"scope-proposal": False}))
        self.assertIsNot(report.status, RunStatus.SUCCEEDED)
        self.assertIs(report.state.node("scope-proposal").status, NodeStatus.FAILED)

    def test_the_audit_chain_survives_replanning(self):
        report = self.run_scenario()
        self.assertTrue(report.audit.verify().valid)

    def test_lineage_records_who_amended_the_requirements(self):
        report = self.run_scenario()
        authors = {e.node_id for e in report.context.lineage("requirements")}
        self.assertIn("stakeholder-review", authors)


if __name__ == "__main__":
    unittest.main()
