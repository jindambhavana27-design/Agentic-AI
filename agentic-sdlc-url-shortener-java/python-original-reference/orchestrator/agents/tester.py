"""Test execution and coverage measurement.

This agent runs the **real** test suite in a subprocess and reports what
actually happened. It does not summarise, estimate, or infer -- if the suite
fails, the agent fails, and the exit gate on the node turns that into a stopped
run.

Coverage is measured with the stdlib :mod:`trace` module. Executed lines come
from the tracer; the denominator is the set of statement lines found by parsing
each module with :mod:`ast`. That ratio is a close approximation of statement
coverage, not branch coverage, and it is labelled as such wherever it is
reported. Using a subprocess keeps the tracer away from the orchestrator's own
threads, which would otherwise be traced too.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional

from ..types import AgentResult, Severity
from .base import Agent, AgentContext

_RUNNER = r'''
import ast, json, os, sys, trace, unittest

package = sys.argv[1]
test_dir = sys.argv[2]
out_path = sys.argv[3]
root = os.getcwd()

def statement_lines(path):
    """Line numbers of executable statements, used as the coverage denominator."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
    except (OSError, SyntaxError):
        return set()
    lines = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt):
            # Definitions and docstrings execute once at import and would
            # inflate the denominator without measuring anything useful.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                continue
            lines.add(node.lineno)
    return lines

targets = {}
for dirpath, dirnames, filenames in os.walk(os.path.join(root, package)):
    dirnames[:] = [d for d in dirnames if d != "__pycache__"]
    for name in filenames:
        if name.endswith(".py"):
            full = os.path.join(dirpath, name)
            targets[os.path.realpath(full)] = statement_lines(full)

loader = unittest.TestLoader()
suite = loader.discover(test_dir, top_level_dir=root)
runner = unittest.TextTestRunner(verbosity=2, stream=sys.stderr)

tracer = trace.Trace(count=1, trace=0, ignoredirs=[sys.prefix, sys.exec_prefix])
holder = {}
tracer.runfunc(lambda: holder.setdefault("result", runner.run(suite)))
result = holder["result"]

executed = {}
for (filename, lineno), hits in tracer.results().counts.items():
    real = os.path.realpath(filename)
    if real in targets:
        executed.setdefault(real, set()).add(lineno)

per_file = {}
total_stmts = total_hit = 0
for path, stmts in sorted(targets.items()):
    hit = len(stmts & executed.get(path, set()))
    total_stmts += len(stmts)
    total_hit += hit
    rel = os.path.relpath(path, root)
    per_file[rel] = {
        "statements": len(stmts),
        "covered": hit,
        "percent": round(100.0 * hit / len(stmts), 2) if stmts else 100.0,
        "missing": sorted(stmts - executed.get(path, set()))[:40],
    }

payload = {
    "tests_run": result.testsRun,
    "failures": [str(t) for t, _ in result.failures],
    "errors": [str(t) for t, _ in result.errors],
    "skipped": [str(t) for t, _ in result.skipped],
    "expected_failures": len(result.expectedFailures),
    "success": result.wasSuccessful(),
    "coverage_percent": round(100.0 * total_hit / total_stmts, 2) if total_stmts else 0.0,
    "statements_total": total_stmts,
    "statements_covered": total_hit,
    "per_file": per_file,
}
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh)
'''


class TestAgent(Agent):
    name = "test-runner"
    description = "Runs the real test suite and measures statement coverage."
    reads = ("changed_files", "requirements", "acceptance_criteria")
    writes = ("test_report", "coverage_percent", "tests_passed")

    def __init__(self, package: str = "service", test_dir: str = "tests",
                 timeout: float = 300.0, measure_coverage: bool = True) -> None:
        self.package = package
        self.test_dir = test_dir
        self.timeout = timeout
        self.measure_coverage = measure_coverage

    def execute(self, ctx: AgentContext) -> AgentResult:
        ctx.read("changed_files", [])
        ctx.read("acceptance_criteria", [])

        workspace = ctx.workspace
        if not os.path.isdir(os.path.join(workspace, self.test_dir)):
            return self.failed("test directory %r not found in workspace" % self.test_dir)

        payload, stderr, returncode = self._run(workspace)
        if payload is None:
            return self.failed(
                "test runner did not produce a report (exit %s)" % returncode,
                findings=[self.finding(Severity.HIGH, "test execution failed to complete",
                                       category="testing",
                                       remediation=(stderr or "").strip()[-800:] or "see runner output")],
                metrics={"returncode": returncode},
            )

        failures = payload["failures"]
        errors = payload["errors"]
        passed = payload["tests_run"] - len(failures) - len(errors)
        coverage = payload["coverage_percent"]

        findings = [
            self.finding(Severity.HIGH, "test failure: %s" % name, category="testing")
            for name in failures + errors
        ]
        weak = [
            (path, data) for path, data in payload["per_file"].items()
            if data["statements"] >= 20 and data["percent"] < 60.0
        ]
        for path, data in weak:
            findings.append(self.finding(
                Severity.MEDIUM,
                "%s has %.0f%% statement coverage" % (path, data["percent"]),
                category="test-coverage", location=path,
                remediation="add tests for the uncovered branches before this ships"))

        report = {
            "tests_run": payload["tests_run"],
            "passed": passed,
            "failed": len(failures),
            "errored": len(errors),
            "skipped": len(payload["skipped"]),
            "success": payload["success"],
            "coverage_percent": coverage,
            "statements_total": payload["statements_total"],
            "statements_covered": payload["statements_covered"],
            "failing_tests": failures + errors,
            "coverage_kind": "statement (stdlib trace); branch coverage is not measured",
        }
        document = _render_report(report, payload["per_file"])
        artifacts = [self.document("test-report", "docs/test-report.md", document)]

        metrics = {
            "tests_run": payload["tests_run"],
            "tests_passed": passed,
            "tests_failed": len(failures) + len(errors),
            "coverage_percent": coverage,
            "pass_rate": round(passed / payload["tests_run"], 4) if payload["tests_run"] else 0.0,
        }

        if not payload["success"]:
            # Failing tests are a hard stop. The exit gate would catch this too,
            # but reporting FAILED keeps the retry policy meaningful.
            return self.failed(
                "%d of %d test(s) failed" % (len(failures) + len(errors), payload["tests_run"]),
                findings=findings, metrics=metrics,
                outputs={"test_report": report, "coverage_percent": coverage,
                         "tests_passed": False},
            )

        return self.ok(
            outputs={"test_report": report, "coverage_percent": coverage, "tests_passed": True},
            artifacts=artifacts, findings=findings, metrics=metrics,
            rationale="%d test(s) passed, %.1f%% statement coverage"
                      % (payload["tests_run"], coverage),
        )

    def _run(self, workspace: str):
        handle, out_path = tempfile.mkstemp(suffix=".json", prefix="testrun-")
        os.close(handle)
        runner_path = os.path.join(tempfile.gettempdir(),
                                   "orch_test_runner_%d.py" % os.getpid())
        with open(runner_path, "w", encoding="utf-8") as fh:
            fh.write(_RUNNER)
        env = dict(os.environ)
        env["PYTHONPATH"] = workspace + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            proc = subprocess.run(
                [sys.executable, runner_path, self.package, self.test_dir, out_path],
                cwd=workspace, env=env, capture_output=True, text=True, timeout=self.timeout,
            )
            payload = None
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                with open(out_path, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            return payload, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired:
            return None, "test suite exceeded %ss" % self.timeout, -1
        finally:
            for path in (out_path, runner_path):
                try:
                    os.remove(path)
                except OSError:
                    pass


def _render_report(report: Dict[str, Any], per_file: Dict[str, Any]) -> str:
    lines = ["# Test Report", "",
             "- tests run: **%d**" % report["tests_run"],
             "- passed: **%d**, failed: **%d**, errored: **%d**, skipped: **%d**"
             % (report["passed"], report["failed"], report["errored"], report["skipped"]),
             "- statement coverage: **%.1f%%** (%d/%d statements)"
             % (report["coverage_percent"], report["statements_covered"],
                report["statements_total"]),
             "", "> %s" % report["coverage_kind"], "",
             "## Per-module coverage", "",
             "| Module | Statements | Covered | % |", "| --- | ---: | ---: | ---: |"]
    for path, data in sorted(per_file.items(), key=lambda kv: kv[1]["percent"]):
        lines.append("| `%s` | %d | %d | %.1f |" % (path, data["statements"],
                                                    data["covered"], data["percent"]))
    if report["failing_tests"]:
        lines += ["", "## Failing tests", ""] + ["- `%s`" % t for t in report["failing_tests"]]
    return "\n".join(lines) + "\n"
