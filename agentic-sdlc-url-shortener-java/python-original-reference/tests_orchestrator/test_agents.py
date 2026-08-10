"""The SDLC agents, exercised against a real workspace copy."""

import os
import shutil
import tempfile
import unittest

from orchestrator.agents import (
    ArchitectAgent,
    ChangePlan,
    DocumentationAgent,
    FileChange,
    ImpactAnalysisAgent,
    ImplementationAgent,
    PlannerAgent,
    ReleaseAgent,
    RequirementsAgent,
    SecurityAgent,
)
from orchestrator.agents.base import AgentContext
from orchestrator.agents.docs import _normalise, _read_schema_paths
from orchestrator.agents.impact import _extract_routes, _pattern_to_path, _reaches
from orchestrator.agents.requirements import _split_statements
from orchestrator.agents.taskspec import TaskSpecAgent
from orchestrator.context import RunContext
from orchestrator.types import AgentOutcome, Severity
from orchestrator.workflows.common import prepare_workspace


def context_for(workspace, values=None, node_id="n"):
    return AgentContext(run_id="r1", node_id=node_id, context=RunContext(values or {}),
                        workspace=workspace, artifacts_dir=tempfile.mkdtemp())


class RequirementsAgentTests(unittest.TestCase):
    def run_agent(self, requirement, clarifications=None, threshold=0.3):
        ctx = context_for(".", {"requirement": requirement,
                                "clarifications": clarifications or {}})
        return RequirementsAgent(ambiguity_threshold=threshold).execute(ctx)

    def test_bulleted_requirements_are_split_per_bullet(self):
        statements = _split_statements("- one thing\n- two thing\n- three thing")
        self.assertEqual(statements, ["one thing", "two thing", "three thing"])

    def test_prose_is_split_into_sentences(self):
        self.assertEqual(len(_split_statements("First thing. Second thing. Third thing.")), 3)

    def test_precise_requirement_produces_requirements(self):
        result = self.run_agent(
            "- The API must return 404 for an unknown code.\n"
            "- Redirects must complete within 50 ms at p99.\n"
            "- Management endpoints must require an API key.\n"
            "- Click counts must be retained for 24 months.\n"
            "- Errors must be logged with a request id and a metric.\n"
            "- The service must sustain 100 requests per second.")
        self.assertIs(result.outcome, AgentOutcome.OK)
        self.assertGreaterEqual(len(result.outputs["requirements"]), 6)

    def test_vague_requirement_escalates_instead_of_guessing(self):
        result = self.run_agent("Make it fast and scalable and handle load.")
        self.assertIs(result.outcome, AgentOutcome.NEEDS_INPUT)
        self.assertTrue(result.questions)

    def test_clarifications_resolve_the_blocking_questions(self):
        vague = "Make it fast and scalable."
        first = self.run_agent(vague)
        blocking = [q for q in first.outputs["open_questions"] if q["blocking"]]
        answers = {q["id"]: "p99 under 50ms" for q in blocking}
        second = self.run_agent(vague, clarifications=answers)
        self.assertIs(second.outcome, AgentOutcome.OK)
        self.assertEqual([q for q in second.outputs["open_questions"]
                          if q["blocking"] and not q["resolved"]], [])

    def test_question_ids_are_stable_across_runs(self):
        # Clarifications are stored against these ids, so they must not move.
        first = self.run_agent("Make it fast.")
        second = self.run_agent("Make it fast.")
        self.assertEqual([q["id"] for q in first.outputs["open_questions"]],
                         [q["id"] for q in second.outputs["open_questions"]])

    def test_ids_are_derived_from_the_term_not_its_position(self):
        result = self.run_agent("Make it fast.")
        self.assertIn("Q-TERM-FAST", [q["id"] for q in result.outputs["open_questions"]])

    def test_interrogative_many_is_not_flagged_as_vague(self):
        result = self.run_agent(
            "- Operators must see how many events are buffered.\n"
            "- The gauge must appear on the metrics endpoint.\n"
            "- Access must require an API key.\n"
            "- Buffered counts are not retained after restart.\n"
            "- The endpoint must sustain 100 requests per second.\n"
            "- Availability follows the existing service SLO.")
        blocking = [q for q in result.outputs["open_questions"] if q["blocking"]]
        self.assertEqual(blocking, [])

    def test_word_boundaries_prevent_substring_false_positives(self):
        result = self.run_agent("- Links from Germany must resolve.\n"
                                "- Somewhere in the docs this is explained.")
        terms = [q["id"] for q in result.outputs["open_questions"]]
        self.assertNotIn("Q-TERM-MANY", terms)
        self.assertNotIn("Q-TERM-SOME", terms)

    def test_missing_requirement_input_fails(self):
        ctx = context_for(".", {})
        self.assertIs(RequirementsAgent().execute(ctx).outcome, AgentOutcome.FAILED)

    def test_unstated_nfr_dimensions_are_raised_as_advisory(self):
        result = self.run_agent("- The API must return 404 for an unknown code.")
        advisory = [q for q in result.outputs["open_questions"] if not q["blocking"]]
        self.assertTrue(any(q["id"].startswith("Q-NFR-") for q in advisory))

    def test_a_requirements_document_is_produced(self):
        result = self.run_agent("- The API must return 404 for an unknown code.")
        names = [a.name for a in result.artifacts]
        self.assertIn("requirements-spec", names)


class ArchitectAgentTests(unittest.TestCase):
    def requirements(self):
        return [
            {"id": "FR-1", "text": "shorten a url and redirect", "kind": "functional",
             "acceptance": []},
            {"id": "FR-2", "text": "store links durably in a database", "kind": "functional",
             "acceptance": []},
            {"id": "FR-3", "text": "report click analytics", "kind": "functional",
             "acceptance": []},
        ]

    def test_produces_components_and_decisions(self):
        ctx = context_for(".", {"requirements": self.requirements()})
        result = ArchitectAgent().execute(ctx)
        self.assertIs(result.outcome, AgentOutcome.OK)
        self.assertTrue(result.outputs["components"])
        self.assertTrue(result.outputs["adrs"])

    def test_decisions_record_alternatives(self):
        ctx = context_for(".", {"requirements": self.requirements()})
        adrs = ArchitectAgent().execute(ctx).outputs["adrs"]
        self.assertTrue(all(a["alternatives"] for a in adrs))

    def test_uncovered_requirement_is_reported_as_a_gap(self):
        requirements = self.requirements() + [
            {"id": "FR-9", "text": "zzz unrelated capability", "kind": "functional",
             "acceptance": []}]
        ctx = context_for(".", {"requirements": requirements})
        result = ArchitectAgent().execute(ctx)
        self.assertLess(result.outputs["design_coverage"], 1.0)
        self.assertTrue(any("FR-9" in f.message for f in result.findings))

    def test_fails_without_requirements(self):
        self.assertIs(ArchitectAgent().execute(context_for(".", {})).outcome,
                      AgentOutcome.FAILED)


class PlannerAgentTests(unittest.TestCase):
    def setup_context(self):
        return context_for(".", {
            "requirements": [
                {"id": "FR-1", "text": "redirect", "kind": "functional", "acceptance": []},
                {"id": "FR-2", "text": "store", "kind": "functional", "acceptance": []},
            ],
            "components": [
                {"name": "domain", "satisfies": ["FR-1"], "depends_on": ["storage"]},
                {"name": "storage", "satisfies": ["FR-2"], "depends_on": []},
            ],
        })

    def test_produces_tasks_with_dependencies(self):
        result = PlannerAgent(inject=False).execute(self.setup_context())
        self.assertIs(result.outcome, AgentOutcome.OK)
        self.assertTrue(result.outputs["task_plan"])
        self.assertTrue(result.outputs["task_sequence"])

    def test_component_dependencies_become_task_dependencies(self):
        plan = PlannerAgent(inject=False).execute(self.setup_context()).outputs["task_plan"]
        domain = next(t for t in plan if "domain" in t["title"])
        storage = next(t for t in plan if "storage" in t["title"])
        self.assertIn(storage["id"], domain["depends_on"])

    def test_test_and_doc_tasks_are_appended(self):
        plan = PlannerAgent(inject=False).execute(self.setup_context()).outputs["task_plan"]
        self.assertIn("test", [t["kind"] for t in plan])
        self.assertIn("document", [t["kind"] for t in plan])

    def test_node_factory_injection_and_barrier(self):
        from orchestrator.graph import Node
        from orchestrator.types import Stage

        def factory(task):
            return Node(id="spec-%s" % task.id, stage=Stage.PLANNING,
                        agent=TaskSpecAgent(task))

        result = PlannerAgent(node_factory=factory, barrier="implement").execute(
            self.setup_context())
        self.assertTrue(result.proposed_nodes)
        self.assertIn("implement", result.rewire)
        self.assertEqual(len(result.rewire["implement"]), len(result.proposed_nodes))

    def test_fails_without_requirements(self):
        self.assertIs(PlannerAgent(inject=False).execute(context_for(".", {})).outcome,
                      AgentOutcome.FAILED)


class ImpactAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = prepare_workspace()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.workspace, ignore_errors=True)

    def test_scans_the_real_package(self):
        ctx = context_for(self.workspace, {"change_target": ["service/storage/base.py"]})
        result = ImpactAnalysisAgent().execute(ctx)
        self.assertIs(result.outcome, AgentOutcome.OK)
        self.assertGreater(result.metrics["modules_scanned"], 10)

    def test_blast_radius_includes_transitive_dependents(self):
        ctx = context_for(self.workspace, {"change_target": ["service/storage/base.py"]})
        impacted = ImpactAnalysisAgent().execute(ctx).outputs["impacted_modules"]
        self.assertIn("service.storage.base", impacted)
        self.assertIn("service.shortener", impacted)
        self.assertIn("service.app", impacted)

    def test_unrelated_module_is_outside_the_blast_radius(self):
        ctx = context_for(self.workspace, {"change_target": ["service/ratelimit.py"]})
        impacted = ImpactAnalysisAgent().execute(ctx).outputs["impacted_modules"]
        self.assertNotIn("service.validation", impacted)

    def test_route_table_is_extracted_from_source(self):
        ctx = context_for(self.workspace, {"change_target": ["service/app.py"]})
        routes = ImpactAnalysisAgent().execute(ctx).outputs["route_table"]
        pairs = {(r["method"], r["path"]) for r in routes}
        self.assertIn(("POST", "/api/v1/links"), pairs)
        self.assertIn(("GET", "/healthz"), pairs)

    def test_auth_flag_is_read_from_the_registration(self):
        ctx = context_for(self.workspace, {"change_target": ["service/app.py"]})
        routes = ImpactAnalysisAgent().execute(ctx).outputs["route_table"]
        create = next(r for r in routes if r["name"] == "create_link")
        redirect = next(r for r in routes if r["name"] == "redirect")
        self.assertTrue(create["auth_required"])
        self.assertFalse(redirect["auth_required"])

    def test_the_real_codebase_has_no_layering_violations(self):
        ctx = context_for(self.workspace, {"change_target": ["service/app.py"]})
        result = ImpactAnalysisAgent().execute(ctx)
        self.assertEqual(result.metrics["layering_violations"], 0)

    def test_missing_package_fails(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, True)
        self.assertIs(ImpactAnalysisAgent().execute(context_for(empty, {})).outcome,
                      AgentOutcome.FAILED)

    def test_pattern_to_path_converts_named_groups(self):
        self.assertEqual(_pattern_to_path(r"^/api/v1/links/(?P<code>[A-Za-z0-9]+)$"),
                         "/api/v1/links/{code}")

    def test_package_import_reaches_submodules(self):
        self.assertTrue(_reaches("service.storage.memory_store", {"service.storage"}))
        self.assertFalse(_reaches("service.validation", {"service.storage"}))


class ImplementationAgentTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="impl-test-")
        self.addCleanup(shutil.rmtree, self.workspace, True)

    def write(self, rel, content):
        path = os.path.join(self.workspace, rel)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as handle:
            handle.write(content)

    def read(self, rel):
        with open(os.path.join(self.workspace, rel)) as handle:
            return handle.read()

    def test_creates_a_file(self):
        plan = ChangePlan("add", [FileChange("a.py", "create", "x = 1\n")])
        result = ImplementationAgent(plan).execute(context_for(self.workspace))
        self.assertIs(result.outcome, AgentOutcome.OK)
        self.assertEqual(self.read("a.py"), "x = 1\n")

    def test_create_over_an_existing_file_fails(self):
        self.write("a.py", "existing\n")
        plan = ChangePlan("add", [FileChange("a.py", "create", "x = 1\n")])
        self.assertIs(ImplementationAgent(plan).execute(context_for(self.workspace)).outcome,
                      AgentOutcome.FAILED)

    def test_anchored_patch_applies(self):
        self.write("a.py", "before\nANCHOR\nafter\n")
        plan = ChangePlan("patch", [FileChange("a.py", "patch", "REPLACED", anchor="ANCHOR")])
        result = ImplementationAgent(plan).execute(context_for(self.workspace))
        self.assertIs(result.outcome, AgentOutcome.OK)
        self.assertIn("REPLACED", self.read("a.py"))

    def test_a_missing_anchor_fails_loudly(self):
        # The file moved on since the plan was written; guessing would corrupt it.
        self.write("a.py", "nothing here\n")
        plan = ChangePlan("patch", [FileChange("a.py", "patch", "X", anchor="ANCHOR")])
        result = ImplementationAgent(plan).execute(context_for(self.workspace))
        self.assertIs(result.outcome, AgentOutcome.FAILED)
        self.assertIn("anchor not found", result.rationale)

    def test_an_ambiguous_anchor_fails(self):
        self.write("a.py", "ANCHOR\nANCHOR\n")
        plan = ChangePlan("patch", [FileChange("a.py", "patch", "X", anchor="ANCHOR")])
        result = ImplementationAgent(plan).execute(context_for(self.workspace))
        self.assertIn("ambiguous", result.rationale)

    def test_path_traversal_is_refused(self):
        plan = ChangePlan("evil", [FileChange("../escape.py", "create", "x = 1\n")])
        result = ImplementationAgent(plan).execute(context_for(self.workspace))
        self.assertIs(result.outcome, AgentOutcome.FAILED)
        self.assertIn("escapes the workspace", result.rationale)

    def test_reports_files_and_lines_changed(self):
        plan = ChangePlan("add", [FileChange("a.py", "create", "one\ntwo\nthree\n")])
        result = ImplementationAgent(plan).execute(context_for(self.workspace))
        self.assertEqual(result.metrics["files_changed"], 1)
        self.assertEqual(result.metrics["lines_changed"], 3)

    def test_third_party_import_is_reported(self):
        plan = ChangePlan("add", [FileChange("a.py", "create", "import requests\n")])
        result = ImplementationAgent(plan).execute(context_for(self.workspace))
        self.assertIn("requests", result.metrics["dependencies_added"])

    def test_stdlib_imports_are_not_flagged(self):
        plan = ChangePlan("add", [FileChange(
            "a.py", "create", "from __future__ import annotations\nimport json, os\n")])
        result = ImplementationAgent(plan).execute(context_for(self.workspace))
        self.assertEqual(result.metrics["dependencies_added"], [])

    def test_dry_run_changes_nothing(self):
        ctx = context_for(self.workspace)
        ctx.dry_run = True
        plan = ChangePlan("add", [FileChange("a.py", "create", "x = 1\n")])
        result = ImplementationAgent(plan).execute(ctx)
        self.assertIs(result.outcome, AgentOutcome.OK)
        self.assertFalse(os.path.exists(os.path.join(self.workspace, "a.py")))


class SecurityAgentTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="sec-test-")
        self.addCleanup(shutil.rmtree, self.workspace, True)
        os.makedirs(os.path.join(self.workspace, "service"))

    def write(self, name, content):
        with open(os.path.join(self.workspace, "service", name), "w") as handle:
            handle.write(content)

    def run_agent(self):
        return SecurityAgent().execute(context_for(self.workspace))

    def test_eval_is_critical_and_fails_the_agent(self):
        self.write("bad.py", "def f(x):\n    return eval(x)\n")
        result = self.run_agent()
        self.assertIs(result.outcome, AgentOutcome.FAILED)
        self.assertTrue(any(f.severity is Severity.CRITICAL for f in result.findings))

    def test_hardcoded_credential_is_critical(self):
        self.write("cfg.py", "api_key = 'abcdefghijklmnop123'\n")
        self.assertGreaterEqual(self.run_agent().metrics["critical_findings"], 1)

    def test_shell_true_is_high(self):
        self.write("run.py", "import subprocess\nsubprocess.run('ls', shell=True)\n")
        result = self.run_agent()
        self.assertTrue(any("shell=True" in f.message for f in result.findings))

    def test_interpolated_sql_is_flagged(self):
        self.write("db.py", "def q(c, t):\n    c.execute('SELECT * FROM %s' % t)\n")
        result = self.run_agent()
        self.assertTrue(any("interpolation" in f.message for f in result.findings))

    def test_non_cryptographic_randomness_is_flagged(self):
        self.write("gen.py", "import random\ndef code():\n    return random.choice('abc')\n")
        result = self.run_agent()
        self.assertTrue(any("randomness" in f.message for f in result.findings))

    def test_missing_ssrf_controls_are_critical(self):
        self.write("clean.py", "x = 1\n")
        result = self.run_agent()
        self.assertTrue(any("private address space" in f.message or
                            "URL validation" in f.message for f in result.findings))

    def test_the_real_service_has_no_critical_findings(self):
        workspace = prepare_workspace()
        self.addCleanup(shutil.rmtree, workspace, True)
        result = SecurityAgent().execute(context_for(workspace))
        self.assertIs(result.outcome, AgentOutcome.OK)
        self.assertEqual(result.metrics["critical_findings"], 0)


class DocumentationAgentTests(unittest.TestCase):
    def setUp(self):
        self.workspace = prepare_workspace()
        self.addCleanup(shutil.rmtree, self.workspace, True)

    def test_verify_mode_detects_a_missing_schema(self):
        schema = os.path.join(self.workspace, "docs", "openapi.yaml")
        if os.path.exists(schema):
            os.remove(schema)
        result = DocumentationAgent(mode="verify").execute(context_for(self.workspace))
        self.assertIs(result.outcome, AgentOutcome.FAILED)
        self.assertGreater(result.metrics["drift_count"], 0)

    def test_sync_mode_reconciles_then_verify_passes(self):
        synced = DocumentationAgent(mode="sync").execute(context_for(self.workspace))
        self.assertIs(synced.outcome, AgentOutcome.OK)
        verified = DocumentationAgent(mode="verify").execute(context_for(self.workspace))
        self.assertIs(verified.outcome, AgentOutcome.OK)
        self.assertEqual(verified.metrics["drift_count"], 0)

    def test_sync_writes_the_schema_into_the_workspace(self):
        DocumentationAgent(mode="sync").execute(context_for(self.workspace))
        self.assertTrue(os.path.exists(os.path.join(self.workspace, "docs", "openapi.yaml")))

    def test_a_phantom_documented_endpoint_is_detected(self):
        DocumentationAgent(mode="sync").execute(context_for(self.workspace))
        schema = os.path.join(self.workspace, "docs", "openapi.yaml")
        with open(schema, "a") as handle:
            handle.write("  /api/v1/ghost:\n    get:\n      responses:\n"
                         "        '200':\n          description: nope\n")
        result = DocumentationAgent(mode="verify").execute(context_for(self.workspace))
        self.assertIs(result.outcome, AgentOutcome.FAILED)
        self.assertTrue(result.outputs["docs_drift"]["phantom"])

    def test_readme_gaps_are_reported(self):
        readme = os.path.join(self.workspace, "README.md")
        with open(readme, "w") as handle:
            handle.write("# Nothing useful here\n")
        result = DocumentationAgent(mode="sync").execute(context_for(self.workspace))
        self.assertGreater(result.metrics["readme_gaps"], 0)

    def test_path_normalisation_makes_parameters_comparable(self):
        self.assertEqual(_normalise("/api/v1/links/{code}"), _normalise("api/v1/links/{id}/"))


class ReleaseAgentTests(unittest.TestCase):
    def evidence(self, **overrides):
        values = {
            "test_report": {"success": True, "coverage_percent": 80.0, "tests_run": 10,
                            "passed": 10},
            "security_report": {"by_severity": {"critical": 0, "high": 0}},
            "documentation_report": {"in_sync": True, "drift": {}},
            "requirements": [{"id": "FR-1", "text": "do a thing", "kind": "functional"}],
            "open_questions": [],
            "design_coverage": 1.0,
            "impact_analysis": {},
        }
        values.update(overrides)
        return values

    def test_full_evidence_passes(self):
        result = ReleaseAgent(min_coverage=60.0).execute(
            context_for(".", self.evidence()))
        self.assertIs(result.outcome, AgentOutcome.OK)
        self.assertTrue(result.outputs["release_decision"]["ready"])

    def test_failing_tests_block_the_release(self):
        result = ReleaseAgent().execute(context_for(".", self.evidence(
            test_report={"success": False, "coverage_percent": 80.0, "tests_run": 10,
                         "passed": 8})))
        self.assertIs(result.outcome, AgentOutcome.FAILED)
        self.assertIn("REL-01", result.outputs["release_decision"]["blocking_failures"])

    def test_low_coverage_blocks_the_release(self):
        result = ReleaseAgent(min_coverage=90.0).execute(context_for(".", self.evidence()))
        self.assertIn("REL-02", result.outputs["release_decision"]["blocking_failures"])

    def test_missing_evidence_is_treated_as_failure_not_success(self):
        # Absence of a report must never read as a passing report.
        result = ReleaseAgent().execute(context_for(".", {}))
        self.assertIs(result.outcome, AgentOutcome.FAILED)
        self.assertIn("REL-01", result.outputs["release_decision"]["blocking_failures"])

    def test_critical_security_finding_blocks(self):
        result = ReleaseAgent().execute(context_for(".", self.evidence(
            security_report={"by_severity": {"critical": 1, "high": 0}})))
        self.assertIn("REL-03", result.outputs["release_decision"]["blocking_failures"])

    def test_documentation_drift_blocks(self):
        result = ReleaseAgent().execute(context_for(".", self.evidence(
            documentation_report={"in_sync": False, "drift": {"undocumented": ["GET /x"]}})))
        self.assertIn("REL-05", result.outputs["release_decision"]["blocking_failures"])

    def test_unresolved_blocking_question_blocks(self):
        result = ReleaseAgent().execute(context_for(".", self.evidence(
            open_questions=[{"blocking": True, "resolved": False, "question": "?"}])))
        self.assertIn("REL-06", result.outputs["release_decision"]["blocking_failures"])

    def test_advisory_check_does_not_block(self):
        result = ReleaseAgent().execute(context_for(".", self.evidence(design_coverage=0.5)))
        self.assertIs(result.outcome, AgentOutcome.OK)
        self.assertIn("REL-07", result.outputs["release_decision"]["advisory_failures"])

    def test_high_severity_can_be_configured_as_advisory(self):
        evidence = self.evidence(security_report={"by_severity": {"critical": 0, "high": 2}})
        blocking = ReleaseAgent().execute(context_for(".", evidence))
        advisory = ReleaseAgent(require_zero_high_security=False).execute(
            context_for(".", evidence))
        self.assertIs(blocking.outcome, AgentOutcome.FAILED)
        self.assertIs(advisory.outcome, AgentOutcome.OK)


class TaskSpecAgentTests(unittest.TestCase):
    def make_task(self, requirement_ids, kind="implement"):
        from orchestrator.agents.planner import Task

        return Task(id="T01", title="do a thing", kind=kind,
                    requirement_ids=requirement_ids)

    def test_traceable_task_produces_a_spec(self):
        ctx = context_for(".", {"requirements": [
            {"id": "FR-1", "text": "a thing", "acceptance": ["it works"]}]})
        result = TaskSpecAgent(self.make_task(["FR-1"])).execute(ctx)
        self.assertIs(result.outcome, AgentOutcome.OK)
        self.assertIn("task_spec_T01", result.outputs)

    def test_untraceable_implementation_task_fails(self):
        # Work with no requirement behind it is scope creep or a dropped
        # requirement; both are cheaper to catch here than in review.
        ctx = context_for(".", {"requirements": []})
        result = TaskSpecAgent(self.make_task([])).execute(ctx)
        self.assertIs(result.outcome, AgentOutcome.FAILED)

    def test_non_implementation_task_without_requirements_is_allowed(self):
        ctx = context_for(".", {"requirements": []})
        result = TaskSpecAgent(self.make_task([], kind="test")).execute(ctx)
        self.assertIs(result.outcome, AgentOutcome.OK)

    def test_unknown_requirement_reference_is_reported(self):
        ctx = context_for(".", {"requirements": [
            {"id": "FR-1", "text": "a thing", "acceptance": []}]})
        result = TaskSpecAgent(self.make_task(["FR-1", "FR-99"])).execute(ctx)
        self.assertTrue(any("FR-99" in f.message for f in result.findings))


if __name__ == "__main__":
    unittest.main()
