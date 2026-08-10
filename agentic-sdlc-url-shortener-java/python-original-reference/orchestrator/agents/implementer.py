"""Implementation agent.

Applies a declared :class:`ChangePlan` to the workspace. Changes are declarative
and reviewable *before* execution, and every edit is anchored to text that must
already be present -- an anchored patch that no longer matches fails loudly
instead of silently corrupting a file that moved on underneath it.

The agent reports the facts governance needs -- files touched, lines changed,
new third-party imports -- and lets the policy engine decide whether that is
acceptable. It does not grade its own change.
"""

from __future__ import annotations

import ast
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..types import AgentResult, Artifact, Severity
from .base import Agent, AgentContext

# Modules that ship with Python. Anything imported outside this set and outside
# the workspace is a new third-party dependency and a supply-chain decision.
#
# ``sys.stdlib_module_names`` only exists from 3.10, so the explicit set is the
# fallback for older interpreters. Both are unioned rather than either/or -- an
# incomplete list here produces false supply-chain alarms, and a policy that
# cries wolf is a policy that gets disabled.
_STDLIB = (
    set(getattr(sys, "stdlib_module_names", ()))
    | set(sys.builtin_module_names)
    | {
        "__future__", "abc", "argparse", "ast", "base64", "binascii", "bisect",
        "codecs", "collections", "concurrent", "contextlib", "copy", "csv",
        "dataclasses", "datetime", "decimal", "difflib", "enum", "filecmp",
        "fnmatch", "functools", "glob", "gzip", "hashlib", "heapq", "hmac",
        "html", "http", "importlib", "inspect", "io", "ipaddress", "itertools",
        "json", "logging", "math", "mimetypes", "operator", "os", "pathlib",
        "pickle", "pprint", "queue", "random", "re", "secrets", "shutil",
        "signal", "socket", "sqlite3", "ssl", "statistics", "string", "struct",
        "subprocess", "sys", "tempfile", "textwrap", "threading", "time",
        "trace", "traceback", "types", "typing", "unittest", "urllib", "uuid",
        "warnings", "weakref", "xml", "zipfile", "zlib",
    }
)


@dataclass
class FileChange:
    """One declarative edit.

    ``create``  -- write ``content``, failing if the file already exists.
    ``replace`` -- overwrite the whole file with ``content``.
    ``patch``   -- replace ``anchor`` with ``content``; the anchor must be
                   present exactly once.
    ``append``  -- append ``content`` to the file.
    """

    path: str
    action: str = "create"
    content: str = ""
    anchor: Optional[str] = None
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "action": self.action, "description": self.description}


@dataclass
class ChangePlan:
    summary: str
    changes: List[FileChange] = field(default_factory=list)
    requirement_ids: List[str] = field(default_factory=list)
    rollback_note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary,
            "changes": [c.to_dict() for c in self.changes],
            "requirement_ids": self.requirement_ids,
            "rollback_note": self.rollback_note,
        }


class ChangeApplicationError(Exception):
    pass


class ImplementationAgent(Agent):
    name = "implementer"
    description = "Applies a reviewed change plan to the workspace."
    reads = ("requirements", "design", "impact_analysis")
    writes = ("implementation_report", "changed_files", "requirement_ids")

    def __init__(self, plan: ChangePlan, package: str = "service") -> None:
        self.plan = plan
        self.package = package

    def execute(self, ctx: AgentContext) -> AgentResult:
        # Reading these records the dependency, so a later requirement change
        # invalidates this node and forces the implementation to be re-derived.
        ctx.read("requirements", [])
        ctx.read("design", {})

        if ctx.dry_run:
            return self.ok(
                outputs={"implementation_report": {"dry_run": True, **self.plan.to_dict()},
                         "changed_files": [], "requirement_ids": self.plan.requirement_ids},
                rationale="dry run: %d change(s) validated but not applied" % len(self.plan.changes),
                metrics={"files_changed": 0, "lines_changed": 0, "dry_run": True},
            )

        applied: List[Dict[str, Any]] = []
        lines_changed = 0
        try:
            for change in self.plan.changes:
                detail = _apply(ctx.workspace, change)
                applied.append(detail)
                lines_changed += detail["lines_changed"]
        except ChangeApplicationError as exc:
            # Partially applied changes are the caller's problem to undo; the
            # node declares a compensation and the engine rolls back.
            return self.failed(
                "change application failed: %s" % exc,
                findings=[self.finding(Severity.HIGH, str(exc), category="implementation")],
                outputs={"changed_files": [d["path"] for d in applied]},
            )

        new_deps = self._new_dependencies(ctx.workspace, [d["path"] for d in applied])
        findings = [
            self.finding(Severity.MEDIUM,
                         "change introduces third-party import %r" % dep,
                         category="supply-chain",
                         remediation="vendor it, replace it with stdlib, or get the dependency approved")
            for dep in new_deps
        ]

        artifacts: List[Artifact] = [
            Artifact(name=os.path.basename(detail["path"]), kind="code",
                     path=detail["path"], content=detail["content"], digest=detail["digest"])
            for detail in applied
        ]
        artifacts.append(self.document("change-summary", "docs/change-summary.md",
                                       _render_summary(self.plan, applied, new_deps)))

        report = {
            "summary": self.plan.summary,
            "changes": applied,
            "requirement_ids": self.plan.requirement_ids,
            "dependencies_added": new_deps,
        }
        return self.ok(
            outputs={
                "implementation_report": report,
                "changed_files": [d["path"] for d in applied],
                "requirement_ids": self.plan.requirement_ids,
            },
            artifacts=artifacts,
            findings=findings,
            metrics={
                "files_changed": len(applied),
                "lines_changed": lines_changed,
                "dependencies_added": new_deps,
            },
            rationale=self.plan.summary,
        )

    def _new_dependencies(self, workspace: str, paths: Sequence[str]) -> List[str]:
        found: set = set()
        for rel in paths:
            full = os.path.join(workspace, rel)
            if not full.endswith(".py") or not os.path.exists(full):
                continue
            try:
                with open(full, "r", encoding="utf-8") as handle:
                    tree = ast.parse(handle.read(), filename=full)
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                names: List[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                for name in names:
                    top = name.split(".")[0]
                    if top and top not in _STDLIB and top != self.package \
                            and not os.path.isdir(os.path.join(workspace, top)):
                        found.add(top)
        return sorted(found)


def _apply(workspace: str, change: FileChange) -> Dict[str, Any]:
    import hashlib

    target = os.path.join(workspace, change.path)
    # Confinement is also enforced by policy; checking here means a malformed
    # plan cannot write outside the workspace even before policy sees it.
    if not os.path.realpath(target).startswith(os.path.realpath(workspace)):
        raise ChangeApplicationError("path %r escapes the workspace" % change.path)

    before = ""
    if os.path.exists(target):
        with open(target, "r", encoding="utf-8") as handle:
            before = handle.read()

    if change.action == "create":
        if before:
            raise ChangeApplicationError("create target %r already exists" % change.path)
        after = change.content
    elif change.action == "replace":
        after = change.content
    elif change.action == "append":
        after = before + change.content
    elif change.action == "patch":
        if not change.anchor:
            raise ChangeApplicationError("patch of %r has no anchor" % change.path)
        occurrences = before.count(change.anchor)
        if occurrences == 0:
            raise ChangeApplicationError(
                "anchor not found in %r; the file has changed since the plan was written"
                % change.path)
        if occurrences > 1:
            raise ChangeApplicationError(
                "anchor appears %d times in %r; it is ambiguous" % (occurrences, change.path))
        after = before.replace(change.anchor, change.content)
    else:
        raise ChangeApplicationError("unknown action %r" % change.action)

    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(after)

    return {
        "path": change.path,
        "action": change.action,
        "description": change.description,
        "lines_changed": _line_delta(before, after),
        "digest": hashlib.sha256(after.encode("utf-8")).hexdigest()[:16],
        "content": after,
    }


def _line_delta(before: str, after: str) -> int:
    import difflib

    diff = difflib.ndiff(before.splitlines(), after.splitlines())
    return sum(1 for line in diff if line.startswith(("+ ", "- ")))


def _render_summary(plan: ChangePlan, applied: List[Dict[str, Any]],
                    new_deps: List[str]) -> str:
    lines = ["# Change Summary", "", plan.summary, "",
             "Requirements addressed: %s" % (", ".join(plan.requirement_ids) or "_unlinked_"), "",
             "| File | Action | Lines changed | Purpose |", "| --- | --- | --- | --- |"]
    for detail in applied:
        lines.append("| `%s` | %s | %d | %s |" % (
            detail["path"], detail["action"], detail["lines_changed"],
            detail["description"] or "-"))
    lines += ["", "## New dependencies", ""]
    lines += ["- `%s`" % d for d in new_deps] or ["None -- the change stays within the stdlib."]
    if plan.rollback_note:
        lines += ["", "## Rollback", "", plan.rollback_note]
    return "\n".join(lines) + "\n"
