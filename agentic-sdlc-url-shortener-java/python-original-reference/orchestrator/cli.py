"""Operator interface to the orchestration layer.

    python3 -m orchestrator plan      <scenario>      show the DAG without running it
    python3 -m orchestrator run       <scenario>      execute a scenario
    python3 -m orchestrator approvals                 list decisions waiting on a human
    python3 -m orchestrator approve   <run> <node>    record an approval
    python3 -m orchestrator reject    <run> <node>    record a rejection
    python3 -m orchestrator resume    <scenario> <run> continue a paused run
    python3 -m orchestrator audit     <run>           verify the audit chain
    python3 -m orchestrator report    <run>           print the stored run report
    python3 -m orchestrator policies                  list the active guardrails

Approval mode is the important flag. ``--approval auto`` is bounded by a risk
ceiling and is what CI uses; ``--approval interactive`` prompts; ``--approval
deferred`` stops the run and waits for an out-of-band decision, which is how a
real change-control process behaves.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from .approvals import (
    AutoApprovalBroker,
    DeferredApprovalBroker,
    InteractiveApprovalBroker,
)
from .audit import verify_file
from .engine import EngineConfig, Orchestrator
from .policy import PolicyEngine
from .replan import Replanner
from .types import RunStatus, Severity
from .workflows import REGISTRY, prepare_workspace, project_root

DEFAULT_ARTIFACT_ROOT = "artifacts"


def _run_dir(artifact_root: str, run_id: str) -> str:
    return os.path.join(artifact_root, run_id)


def _broker(mode: str, artifact_root: str, ceiling: str):
    if mode == "interactive":
        return InteractiveApprovalBroker()
    if mode == "deferred":
        return DeferredApprovalBroker(os.path.join(artifact_root, "approvals.json"))
    return AutoApprovalBroker(max_auto_risk=Severity(ceiling))


# -- commands -----------------------------------------------------------------


def cmd_plan(args: argparse.Namespace) -> int:
    module = REGISTRY[args.scenario]
    graph = module.build(module.default_inputs())
    print(graph.render_ascii())
    print()
    print("nodes: %d, critical path: %d level(s), max parallel width: %d"
          % (len(graph), graph.critical_path_length(),
             max(len(l) for l in graph.topological_levels())))
    if args.json:
        print(json.dumps(graph.to_dict(), indent=2, default=str))
    return 0


def cmd_policies(args: argparse.Namespace) -> int:
    for policy in PolicyEngine().describe():
        print("%-8s [%-15s] %s" % (policy["id"], policy["category"], policy["description"]))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    module = REGISTRY[args.scenario]

    inputs: Dict[str, Any] = (
        module.default_inputs(with_clarifications=not args.no_clarifications)
        if args.scenario == "ambiguous"
        else module.default_inputs()
    )
    if args.inject_fault:
        inputs["inject_fault"] = True
    if args.input:
        inputs.update(json.loads(args.input))

    workspace = args.workspace or prepare_workspace()
    graph = module.build(inputs)

    artifact_root = args.artifacts
    os.makedirs(artifact_root, exist_ok=True)

    config = EngineConfig(
        max_parallelism=args.parallelism,
        workspace=workspace,
        artifacts_dir=artifact_root,
        journal_path=os.path.join(artifact_root, "journal-%s.jsonl" % args.scenario),
        audit_path=os.path.join(artifact_root, "audit-%s.jsonl" % args.scenario),
        snapshot_workspace=args.snapshot,
        dry_run=args.dry_run,
        max_replans_per_node=args.max_replans,
        safe_stop_after_consecutive_failures=args.safe_stop_after,
    )
    engine = Orchestrator(
        graph, config,
        approval_broker=_broker(args.approval, artifact_root, args.risk_ceiling),
        replanner=Replanner(args.max_replans),
    )

    print("workspace : %s" % workspace)
    print("scenario  : %s -- %s" % (graph.name, graph.description))
    print("approvals : %s (auto ceiling: %s)" % (args.approval, args.risk_ceiling))
    print()
    print(graph.render_ascii())
    print("\n" + "-" * 78 + "\n")

    report = engine.run(inputs)

    _print_report(report, verbose=args.verbose)

    run_dir = _run_dir(artifact_root, report.run_id)
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "report.json"), "w", encoding="utf-8") as handle:
        json.dump(report.to_dict(), handle, indent=2, default=str)
    print("\nfull report : %s" % os.path.join(run_dir, "report.json"))
    print("artifacts   : %s" % run_dir)

    if report.status is RunStatus.SUCCEEDED:
        return 0
    if report.status is RunStatus.AWAITING_APPROVAL:
        print("\nrun is paused. record decisions with:")
        for pending in report.pending_approvals:
            print("  python3 -m orchestrator approve %s %s" % (report.run_id, pending["node_id"]))
        print("then: python3 -m orchestrator resume %s %s --workspace %s"
              % (args.scenario, report.run_id, workspace))
        return 2
    return 1


def cmd_resume(args: argparse.Namespace) -> int:
    module = REGISTRY[args.scenario]
    inputs = (module.default_inputs(with_clarifications=True)
              if args.scenario == "ambiguous" else module.default_inputs())
    if args.input:
        inputs.update(json.loads(args.input))

    workspace = args.workspace or prepare_workspace()
    graph = module.build(inputs)
    config = EngineConfig(
        max_parallelism=args.parallelism,
        workspace=workspace,
        artifacts_dir=args.artifacts,
        journal_path=os.path.join(args.artifacts, "journal-%s.jsonl" % args.scenario),
        audit_path=os.path.join(args.artifacts, "audit-%s.jsonl" % args.scenario),
    )
    engine = Orchestrator(graph, config,
                          approval_broker=_broker(args.approval, args.artifacts,
                                                  args.risk_ceiling))
    report = engine.run(inputs, run_id=args.run_id, resume=True)
    _print_report(report, verbose=args.verbose)
    return 0 if report.status is RunStatus.SUCCEEDED else 1


def cmd_approvals(args: argparse.Namespace) -> int:
    broker = DeferredApprovalBroker(os.path.join(args.artifacts, "approvals.json"))
    pending = broker.pending()
    if not pending:
        print("no approvals pending")
        return 0
    for record in pending:
        request = record["request"]
        print("run=%s node=%s risk=%s" % (request["run_id"], request["node_id"], request["risk"]))
        print("  %s" % request["summary"])
        for reason in request.get("reasons", []):
            print("    - %s" % reason)
    return 0


def cmd_decide(args: argparse.Namespace, approved: bool) -> int:
    broker = DeferredApprovalBroker(os.path.join(args.artifacts, "approvals.json"))
    try:
        broker.decide(args.run_id, args.node_id, approved,
                      decided_by=args.by, reason=args.reason or "")
    except KeyError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print("recorded: %s %s -> %s" % (args.run_id, args.node_id,
                                     "approved" if approved else "rejected"))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    path = args.path or os.path.join(args.artifacts, "audit-%s.jsonl" % args.scenario)
    if not os.path.exists(path):
        print("no audit log at %s" % path, file=sys.stderr)
        return 1
    outcome = verify_file(path)
    print("audit log   : %s" % path)
    print("events      : %d" % outcome.checked)
    print("chain valid : %s" % outcome.valid)
    if not outcome.valid:
        print("broken at   : sequence %s (%s)" % (outcome.broken_at, outcome.reason))
        return 1
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    path = os.path.join(_run_dir(args.artifacts, args.run_id), "report.json")
    if not os.path.exists(path):
        print("no report at %s" % path, file=sys.stderr)
        return 1
    with open(path, "r", encoding="utf-8") as handle:
        report = json.load(handle)
    print(json.dumps(report["metrics"], indent=2))
    return 0


# -- output -------------------------------------------------------------------


def _print_report(report, verbose: bool = False) -> None:
    print("=" * 78)
    print("RUN %s -- %s" % (report.run_id, report.status.value.upper()))
    print("=" * 78)
    print()
    print("Node outcomes")
    print("-" * 78)
    for node_id in report.graph.node_ids():
        state = report.state.nodes.get(node_id)
        if state is None:
            continue
        node = report.graph.get(node_id)
        flags = []
        if state.retry_count:
            flags.append("%d retry" % state.retry_count)
        if state.replan_count:
            flags.append("%d re-plan" % state.replan_count)
        if state.approval_wait_seconds:
            flags.append("approval %.1fs" % state.approval_wait_seconds)
        if node.injected_by:
            flags.append("injected by %s" % node.injected_by)
        print("  %-22s %-18s %6.2fs  %s"
              % (node_id, state.status.value, state.execution_seconds,
                 ("[%s]" % ", ".join(flags)) if flags else ""))
        if state.error:
            print("      error: %s" % state.error)

    if report.state.replan_events:
        print()
        print("Re-planning")
        print("-" * 78)
        for event in report.state.replan_events:
            print("  %s invalidated by %s (drift: %s)"
                  % (event["node_id"], event.get("trigger"),
                     ", ".join(sorted((event.get("drift") or {}).keys())) or "cascade"))

    if report.state.approvals:
        print()
        print("Human checkpoints")
        print("-" * 78)
        for approval in report.state.approvals:
            decision = approval.get("decision")
            verdict = "PENDING" if decision is None else ("approved" if decision else "REJECTED")
            print("  %-22s %-9s risk=%-8s %s"
                  % (approval["node_id"], verdict, approval.get("risk", "?"),
                     approval.get("summary", "")))
            if approval.get("decided_by"):
                print("      by %s: %s" % (approval["decided_by"], approval.get("reason", "")))

    findings = sorted(report.findings,
                      key=lambda f: ["critical", "high", "medium", "low", "info"]
                      .index(f.severity.value))
    if findings:
        print()
        print("Findings (%d)" % len(findings))
        print("-" * 78)
        for finding in findings[: (None if verbose else 12)]:
            print("  [%-8s] %-16s %s" % (finding.severity.value, finding.category,
                                         finding.message))
        if not verbose and len(findings) > 12:
            print("  ... %d more (use --verbose)" % (len(findings) - 12))

    if report.rollbacks:
        print()
        print("Rollbacks")
        print("-" * 78)
        for rollback in report.rollbacks:
            print("  triggered by %s: %s" % (rollback.triggered_by, rollback.reason))
            for outcome in rollback.compensations:
                print("    compensate %-20s %s" % (outcome.node_id,
                                                   "ok" if outcome.succeeded else outcome.error))

    print()
    print("Reliability metrics")
    print("-" * 78)
    print(report.metrics.render())

    verification = report.audit.verify()
    print()
    print("Audit: %d event(s), chain %s, head %s"
          % (verification.checked, "valid" if verification.valid else "BROKEN",
             report.audit.head_hash()[:16]))


# -- argument parsing ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m orchestrator",
        description="Agentic SDLC orchestration for the URL shortener.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--artifacts", default=DEFAULT_ARTIFACT_ROOT,
                        help="directory for journals, audit logs, and artifacts")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="print the workflow graph without executing it")
    plan.add_argument("scenario", choices=sorted(REGISTRY))
    plan.add_argument("--json", action="store_true")
    plan.set_defaults(func=cmd_plan)

    run = sub.add_parser("run", help="execute a scenario")
    run.add_argument("scenario", choices=sorted(REGISTRY))
    run.add_argument("--workspace", help="use this directory instead of a fresh copy")
    run.add_argument("--parallelism", type=int, default=4)
    run.add_argument("--approval", choices=("auto", "interactive", "deferred"), default="auto")
    run.add_argument("--risk-ceiling", default="medium",
                     choices=[s.value for s in Severity],
                     help="highest risk the auto broker may approve unattended")
    run.add_argument("--snapshot", action="store_true", default=True,
                     help="snapshot the workspace before implementation (default on)")
    run.add_argument("--no-snapshot", dest="snapshot", action="store_false")
    run.add_argument("--dry-run", action="store_true",
                     help="plan and validate without mutating the workspace")
    run.add_argument("--inject-fault", action="store_true",
                     help="make a node fail its first attempts, to exercise retry and MTTR")
    run.add_argument("--no-clarifications", action="store_true",
                     help="ambiguous scenario only: withhold the human answers")
    run.add_argument("--max-replans", type=int, default=3)
    run.add_argument("--safe-stop-after", type=int, default=3)
    run.add_argument("--input", help="JSON object merged over the scenario defaults")
    run.add_argument("--verbose", action="store_true")
    run.set_defaults(func=cmd_run)

    resume = sub.add_parser("resume", help="continue a run that paused for approval")
    resume.add_argument("scenario", choices=sorted(REGISTRY))
    resume.add_argument("run_id")
    resume.add_argument("--workspace")
    resume.add_argument("--parallelism", type=int, default=4)
    resume.add_argument("--approval", choices=("auto", "interactive", "deferred"),
                        default="deferred")
    resume.add_argument("--risk-ceiling", default="medium", choices=[s.value for s in Severity])
    resume.add_argument("--input")
    resume.add_argument("--verbose", action="store_true")
    resume.set_defaults(func=cmd_resume)

    approvals = sub.add_parser("approvals", help="list pending human decisions")
    approvals.set_defaults(func=cmd_approvals)

    approve = sub.add_parser("approve", help="record an approval")
    approve.add_argument("run_id")
    approve.add_argument("node_id")
    approve.add_argument("--by", default="operator")
    approve.add_argument("--reason")
    approve.set_defaults(func=lambda a: cmd_decide(a, True))

    reject = sub.add_parser("reject", help="record a rejection")
    reject.add_argument("run_id")
    reject.add_argument("node_id")
    reject.add_argument("--by", default="operator")
    reject.add_argument("--reason")
    reject.set_defaults(func=lambda a: cmd_decide(a, False))

    audit = sub.add_parser("audit", help="verify the tamper-evident audit chain")
    audit.add_argument("scenario", nargs="?", default="greenfield", choices=sorted(REGISTRY))
    audit.add_argument("--path")
    audit.set_defaults(func=cmd_audit)

    report = sub.add_parser("report", help="print the metrics from a stored run report")
    report.add_argument("run_id")
    report.set_defaults(func=cmd_report)

    policies = sub.add_parser("policies", help="list the active policy guardrails")
    policies.set_defaults(func=cmd_policies)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
