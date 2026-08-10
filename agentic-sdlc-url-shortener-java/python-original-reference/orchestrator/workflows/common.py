"""Shared scaffolding for the three scenario workflows.

Two things live here. First, workspace isolation: every run executes against a
disposable copy of the project, never the checkout. An orchestration system that
edits your working tree while you are reading its output is not a system anyone
should run twice.

Second, the standard verification tail -- test, security, documentation, release.
All three scenarios share it because the bar for shipping should not depend on
which scenario produced the change. Only the front half (how the work is
understood and decomposed) differs.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional

from ..agents import (
    DocumentationAgent,
    ReleaseAgent,
    SecurityAgent,
    TestAgent,
)
from ..gates import (
    AgentSucceeded,
    AllUpstreamSucceeded,
    ContextKeysPresent,
    MetricThreshold,
    NoFindingsAtOrAbove,
    ProducedOutputs,
)
from ..graph import Node
from ..types import AutonomyLevel, FailureAction, RetryPolicy, Severity, Stage

# Copied into every run workspace. Anything not listed is invisible to the run,
# which keeps the blast radius of a misbehaving agent small by construction.
WORKSPACE_CONTENTS = ("service", "tests", "docs", "README.md")


def project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def prepare_workspace(destination: Optional[str] = None,
                      source: Optional[str] = None) -> str:
    """Materialise an isolated copy of the project for a run."""
    source = source or project_root()
    destination = destination or tempfile.mkdtemp(prefix="orch-ws-")
    os.makedirs(destination, exist_ok=True)
    for entry in WORKSPACE_CONTENTS:
        src = os.path.join(source, entry)
        dst = os.path.join(destination, entry)
        if os.path.isdir(src):
            shutil.copytree(
                src, dst, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.db",
                                              "*.db-wal", "*.db-shm"),
            )
        elif os.path.isfile(src):
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            shutil.copy2(src, dst)
    return destination


def verification_tail(depends_on: List[str], min_coverage: float = 55.0,
                      version: str = "1.1.0",
                      require_zero_high_security: bool = True) -> List[Node]:
    """Test, security, documentation, and release, as a partly parallel tail.

    ``test`` and ``security`` have no dependency on each other and run
    concurrently. ``docs-sync`` reconciles the schema with the code; ``release``
    is the synchronisation barrier that waits for all of them and additionally
    requires that each genuinely *succeeded* rather than merely finished.
    """
    return [
        Node(
            id="test", stage=Stage.TESTING, agent=TestAgent(),
            description="Run the real test suite and measure statement coverage.",
            depends_on=list(depends_on),
            entry_gates=[ContextKeysPresent("requirements")],
            exit_gates=[
                AgentSucceeded(),
                ProducedOutputs("test_report"),
                MetricThreshold("pass_rate", minimum=1.0),
                MetricThreshold("coverage_percent", minimum=min_coverage),
            ],
            # A failing suite is usually deterministic, but an exhausted port or
            # a slow filesystem is not; one retry separates the two cheaply.
            retry=RetryPolicy(max_attempts=2, backoff_seconds=0.5),
            on_failure=FailureAction.ROLLBACK,
            autonomy=AutonomyLevel.AUTONOMOUS,
            timeout_seconds=420.0,
        ),
        Node(
            id="security", stage=Stage.SECURITY, agent=SecurityAgent(),
            description="Static security review of the service source and HTTP surface.",
            depends_on=list(depends_on),
            exit_gates=[AgentSucceeded(), NoFindingsAtOrAbove(Severity.CRITICAL)],
            on_failure=FailureAction.ROLLBACK,
            autonomy=AutonomyLevel.AUTONOMOUS,
            policy_tags=["security"],
        ),
        Node(
            id="docs-sync", stage=Stage.DOCUMENTATION,
            agent=DocumentationAgent(mode="sync"),
            description="Regenerate the OpenAPI schema from the implemented routes.",
            depends_on=list(depends_on),
            exit_gates=[AgentSucceeded(), ProducedOutputs("documentation_report")],
            autonomy=AutonomyLevel.ACT_AND_REPORT,
        ),
        Node(
            id="docs-verify", stage=Stage.DOCUMENTATION,
            agent=DocumentationAgent(mode="verify"),
            description="Independently confirm the schema matches the code. Changes nothing.",
            depends_on=["docs-sync"],
            exit_gates=[
                AgentSucceeded(),
                MetricThreshold("drift_count", maximum=0),
            ],
            autonomy=AutonomyLevel.PROPOSE_ONLY,
        ),
        Node(
            id="release", stage=Stage.RELEASE,
            agent=ReleaseAgent(min_coverage=min_coverage, version=version,
                               require_zero_high_security=require_zero_high_security),
            description="Evaluate release readiness from recorded evidence.",
            depends_on=["test", "security", "docs-verify"],
            # Dependency satisfaction alone would accept a skipped security
            # scan; the release barrier demands actual success.
            entry_gates=[AllUpstreamSucceeded()],
            exit_gates=[AgentSucceeded(), ProducedOutputs("release_decision")],
            requires_approval=True,
            approval_prompt="release v%s to production" % version,
            autonomy=AutonomyLevel.ACT_WITH_APPROVAL,
            on_failure=FailureAction.SAFE_STOP,
            policy_tags=["release", "change_control"],
        ),
    ]


def restore_workspace_compensation(snapshot_dir: str, workspace: str):
    """Build a compensation that restores the workspace from ``snapshot_dir``."""

    def _compensate(_context: Any) -> None:
        if not os.path.isdir(snapshot_dir):
            raise FileNotFoundError("snapshot %s is gone" % snapshot_dir)
        for entry in WORKSPACE_CONTENTS:
            src = os.path.join(snapshot_dir, entry)
            dst = os.path.join(workspace, entry)
            if os.path.isdir(src):
                shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(src, dst)
            elif os.path.isfile(src):
                shutil.copy2(src, dst)

    return _compensate
