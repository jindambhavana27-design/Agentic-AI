"""Agentic SDLC orchestration layer.

Public surface::

    WorkflowGraph / Node        the dependency graph a run executes
    Orchestrator / EngineConfig the readiness scheduler and its knobs
    RunContext                  versioned shared context with decision lineage
    PolicyEngine                security, compliance, and change-control guardrails
    ApprovalBroker              human checkpoints (auto / interactive / deferred)
    RunReport / RunMetrics      the outcome and its reliability numbers
"""

from .approvals import (
    ApprovalBroker,
    ApprovalDecision,
    ApprovalRequest,
    AutoApprovalBroker,
    DeferredApprovalBroker,
    InteractiveApprovalBroker,
    StaticApprovalBroker,
)
from .audit import AuditLog, verify_file
from .context import RunContext
from .engine import EngineConfig, Orchestrator, RunReport
from .gates import (
    AgentSucceeded,
    AllUpstreamSucceeded,
    ContextKeysPresent,
    MetricThreshold,
    NoFindingsAtOrAbove,
    NoOpenQuestions,
    PredicateGate,
    ProducedOutputs,
)
from .graph import Node, WorkflowGraph
from .metrics import RunMetrics, compute_metrics
from .policy import PolicyEngine, default_policies
from .replan import Replanner
from .state import RunState
from .types import (
    AgentOutcome,
    AgentResult,
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

__version__ = "1.0.0"

__all__ = [
    "Orchestrator", "EngineConfig", "RunReport", "WorkflowGraph", "Node", "RunContext",
    "PolicyEngine", "default_policies", "Replanner", "RunState", "RunMetrics",
    "compute_metrics", "AuditLog", "verify_file",
    "ApprovalBroker", "ApprovalRequest", "ApprovalDecision", "AutoApprovalBroker",
    "InteractiveApprovalBroker", "DeferredApprovalBroker", "StaticApprovalBroker",
    "AgentSucceeded", "AllUpstreamSucceeded", "ContextKeysPresent", "MetricThreshold",
    "NoFindingsAtOrAbove", "NoOpenQuestions", "PredicateGate", "ProducedOutputs",
    "Stage", "NodeStatus", "RunStatus", "Severity", "AutonomyLevel", "FailureAction",
    "RetryPolicy", "AgentResult", "AgentOutcome", "Finding", "Artifact",
]
