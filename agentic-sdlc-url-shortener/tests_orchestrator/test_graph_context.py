"""Graph structure, runtime expansion, and the versioned context."""

import unittest

from orchestrator.context import RunContext, fingerprint
from orchestrator.graph import Node, WorkflowGraph
from orchestrator.types import FailureAction, GraphValidationError, Stage

from .support import RecordingAgent, graph, node


class GraphStructureTests(unittest.TestCase):
    def build(self):
        return graph(
            node("a", RecordingAgent("a")),
            node("b", RecordingAgent("b"), depends_on=["a"]),
            node("c", RecordingAgent("c"), depends_on=["a"]),
            node("d", RecordingAgent("d"), depends_on=["b", "c"]),
        )

    def test_levels_group_parallel_nodes(self):
        self.assertEqual(self.build().topological_levels(),
                         [["a"], ["b", "c"], ["d"]])

    def test_roots_and_leaves(self):
        g = self.build()
        self.assertEqual(g.roots(), ["a"])
        self.assertEqual(g.leaves(), ["d"])

    def test_dependents_and_descendants(self):
        g = self.build()
        self.assertEqual(sorted(g.dependents("a")), ["b", "c"])
        self.assertEqual(g.descendants("a"), {"b", "c", "d"})

    def test_ancestors(self):
        self.assertEqual(self.build().ancestors("d"), {"a", "b", "c"})

    def test_critical_path_length(self):
        self.assertEqual(self.build().critical_path_length(), 3)

    def test_unknown_dependency_rejected(self):
        with self.assertRaises(GraphValidationError):
            graph(node("a", RecordingAgent("a"), depends_on=["ghost"]))

    def test_self_dependency_rejected(self):
        with self.assertRaises(GraphValidationError):
            graph(node("a", RecordingAgent("a"), depends_on=["a"]))

    def test_cycle_rejected(self):
        with self.assertRaises(GraphValidationError):
            graph(
                node("a", RecordingAgent("a"), depends_on=["c"]),
                node("b", RecordingAgent("b"), depends_on=["a"]),
                node("c", RecordingAgent("c"), depends_on=["b"]),
            )

    def test_duplicate_id_rejected(self):
        g = self.build()
        with self.assertRaises(GraphValidationError):
            g.add(node("a", RecordingAgent("a2")))

    def test_compensation_on_a_non_rollback_node_rejected(self):
        with self.assertRaises(GraphValidationError):
            Node(id="x", stage=Stage.IMPLEMENTATION, agent=RecordingAgent("x"),
                 compensation=lambda _ctx: None, on_failure=FailureAction.CONTINUE)

    def test_render_ascii_shows_levels_and_flags(self):
        g = graph(
            node("a", RecordingAgent("a")),
            node("b", RecordingAgent("b"), depends_on=["a"], requires_approval=True),
        )
        rendered = g.render_ascii()
        self.assertIn("level 0", rendered)
        self.assertIn("approval", rendered)


class GraphExpansionTests(unittest.TestCase):
    def setUp(self):
        self.graph = graph(
            node("plan", RecordingAgent("plan")),
            node("build", RecordingAgent("build"), depends_on=["plan"]),
        )

    def test_injects_nodes(self):
        added = self.graph.expand([node("task-1", RecordingAgent("t1"), depends_on=["plan"])],
                                  injected_by="plan")
        self.assertEqual(added, ["task-1"])
        self.assertEqual(self.graph.get("task-1").injected_by, "plan")

    def test_rewires_a_downstream_barrier(self):
        self.graph.expand([node("task-1", RecordingAgent("t1"), depends_on=["plan"])],
                          injected_by="plan", attach={"build": ["task-1"]})
        self.assertIn("task-1", self.graph.get("build").depends_on)

    def test_a_cycle_creating_expansion_is_rejected_atomically(self):
        with self.assertRaises(GraphValidationError):
            self.graph.expand(
                [node("task-1", RecordingAgent("t1"), depends_on=["build"])],
                injected_by="plan", attach={"build": ["task-1"]},
            )
        # Nothing partially applied: no new node, no new edge.
        self.assertNotIn("task-1", self.graph)
        self.assertEqual(self.graph.get("build").depends_on, ["plan"])

    def test_attaching_to_an_unknown_node_is_rejected_atomically(self):
        with self.assertRaises(GraphValidationError):
            self.graph.expand([node("task-1", RecordingAgent("t1"))],
                              injected_by="plan", attach={"ghost": ["task-1"]})
        self.assertNotIn("task-1", self.graph)

    def test_duplicate_injection_rejected(self):
        self.graph.expand([node("task-1", RecordingAgent("t1"))], injected_by="plan")
        with self.assertRaises(GraphValidationError):
            self.graph.expand([node("task-1", RecordingAgent("t1"))], injected_by="plan")


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.context = RunContext({"seed": 1})

    def test_seed_values_are_versioned(self):
        self.assertEqual(self.context.get("seed"), 1)
        self.assertEqual(self.context.version("seed"), 1)

    def test_write_bumps_the_version(self):
        self.context.put("k", "v1", "n1")
        self.context.put("k", "v2", "n1")
        self.assertEqual(self.context.version("k"), 2)

    def test_identical_rewrite_does_not_bump_the_version(self):
        # Otherwise an idempotent re-run would cascade a pointless re-plan.
        self.context.put("k", {"a": 1}, "n1")
        self.context.put("k", {"a": 1}, "n1")
        self.assertEqual(self.context.version("k"), 1)

    def test_read_records_the_observed_version(self):
        self.context.put("k", "v1", "writer")
        self.context.read("k", "reader")
        self.assertEqual(self.context.observed_versions("reader"), {"k": 1})

    def test_get_does_not_record_a_dependency(self):
        self.context.put("k", "v1", "writer")
        self.context.get("k")
        self.assertEqual(self.context.observed_versions("reader"), {})

    def test_stale_keys_detect_drift(self):
        self.context.put("k", "v1", "writer")
        self.context.read("k", "reader")
        self.context.put("k", "v2", "writer")
        self.assertEqual(self.context.stale_keys("reader"),
                         {"k": {"observed": 1, "current": 2}})

    def test_no_drift_when_unchanged(self):
        self.context.put("k", "v1", "writer")
        self.context.read("k", "reader")
        self.assertEqual(self.context.stale_keys("reader"), {})

    def test_reading_a_missing_key_records_version_zero(self):
        self.context.read("absent", "reader")
        self.assertEqual(self.context.observed_versions("reader"), {"absent": 0})
        self.context.put("absent", "now here", "writer")
        self.assertIn("absent", self.context.stale_keys("reader"))

    def test_clear_reads_resets_the_dependency_set(self):
        self.context.put("k", "v1", "writer")
        self.context.read("k", "reader")
        self.context.clear_reads("reader")
        self.assertEqual(self.context.observed_versions("reader"), {})

    def test_lineage_records_author_and_rationale(self):
        self.context.put("k", "v1", "writer", "because")
        entry = self.context.lineage("k")[-1]
        self.assertEqual(entry.node_id, "writer")
        self.assertEqual(entry.rationale, "because")

    def test_lineage_is_append_only_across_versions(self):
        self.context.put("k", "v1", "a")
        self.context.put("k", "v2", "b")
        self.assertEqual([e.version for e in self.context.lineage("k")], [1, 2])

    def test_fingerprint_is_order_independent(self):
        self.assertEqual(fingerprint({"a": 1, "b": 2}), fingerprint({"b": 2, "a": 1}))

    def test_fingerprint_distinguishes_values(self):
        self.assertNotEqual(fingerprint({"a": 1}), fingerprint({"a": 2}))


if __name__ == "__main__":
    unittest.main()
