"""Rollback: workspace snapshots and compensating actions.

Two independent mechanisms, because they undo different kinds of damage:

* :class:`WorkspaceSnapshot` restores files. It is the blunt instrument that
  makes a failed implementation stage a non-event.
* :class:`CompensationRegistry` runs per-node undo callables in reverse
  dependency order -- the saga pattern. It exists for effects that live outside
  the filesystem (a published artifact, a created resource).

Compensation runs best-effort: one failing compensator must not prevent the
others from running, or a partial rollback leaves the system in a state nobody
designed. Failures are collected and reported rather than raised.
"""

from __future__ import annotations

import filecmp
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class SnapshotInfo:
    path: str
    created_at: float
    file_count: int
    source: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "source": self.source,
            "created_at": self.created_at,
            "file_count": self.file_count,
        }


# Module level, not a class attribute: `shutil.ignore_patterns` returns a plain
# function, which Python would bind as a method if it lived on the class.
IGNORE_PATTERNS = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".git", ".venv", "node_modules", "*.db", "*.db-wal", "*.db-shm"
)


class WorkspaceSnapshot:
    """Copy-on-create snapshot of a directory tree.

    Only used for agent workspaces, which are small by construction. Snapshotting
    a large repository this way would be wrong -- there, the right primitive is a
    VCS commit.
    """

    def __init__(self, source: str, root: Optional[str] = None, label: str = "snapshot") -> None:
        self.source = os.path.abspath(source)
        if not os.path.isdir(self.source):
            raise ValueError("snapshot source %r is not a directory" % source)
        base = root or tempfile.mkdtemp(prefix="orch-snap-")
        os.makedirs(base, exist_ok=True)
        self.path = os.path.join(base, "%s-%d" % (label, int(time.time() * 1000)))
        shutil.copytree(self.source, self.path, ignore=IGNORE_PATTERNS)
        self.created_at = time.time()
        self.restored = False

    @property
    def file_count(self) -> int:
        return sum(len(files) for _, _, files in os.walk(self.path))

    def restore(self) -> int:
        """Restore the source tree from the snapshot. Returns files written."""
        if not os.path.isdir(self.path):
            raise FileNotFoundError("snapshot %r no longer exists" % self.path)
        # Replace rather than merge: leaving files the agent created would be a
        # partial rollback, which is the state we are trying to avoid.
        if os.path.isdir(self.source):
            shutil.rmtree(self.source)
        shutil.copytree(self.path, self.source)
        self.restored = True
        return self.file_count

    def diff(self) -> Dict[str, List[str]]:
        """Files added, removed, or modified since the snapshot was taken."""
        return _tree_diff(self.path, self.source)

    def discard(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def describe(self) -> SnapshotInfo:
        return SnapshotInfo(self.path, self.created_at, self.file_count, self.source)


def _tree_diff(before: str, after: str) -> Dict[str, List[str]]:
    def listing(root: str) -> Dict[str, str]:
        found: Dict[str, str] = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
            for name in filenames:
                if name.endswith(".pyc"):
                    continue
                full = os.path.join(dirpath, name)
                found[os.path.relpath(full, root)] = full
        return found

    old, new = listing(before), listing(after)
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    modified = sorted(
        rel for rel in (set(old) & set(new))
        if not filecmp.cmp(old[rel], new[rel], shallow=False)
    )
    return {"added": added, "removed": removed, "modified": modified}


@dataclass
class CompensationOutcome:
    node_id: str
    succeeded: bool
    error: Optional[str] = None
    duration_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "succeeded": self.succeeded,
            "error": self.error,
            "duration_seconds": round(self.duration_seconds, 4),
        }


class CompensationRegistry:
    """Undo actions registered as nodes succeed, executed in reverse order."""

    def __init__(self) -> None:
        self._entries: List[tuple] = []  # (node_id, callable)

    def register(self, node_id: str, action: Callable[[Any], None]) -> None:
        self._entries.append((node_id, action))

    def __len__(self) -> int:
        return len(self._entries)

    def node_ids(self) -> List[str]:
        return [nid for nid, _ in self._entries]

    def run_all(self, context: Any) -> List[CompensationOutcome]:
        """Execute every compensation in reverse registration order.

        Reverse registration order is reverse *completion* order, which for a DAG
        is a valid reverse-topological order over the nodes that actually ran.
        """
        outcomes: List[CompensationOutcome] = []
        for node_id, action in reversed(self._entries):
            started = time.time()
            try:
                action(context)
                outcomes.append(CompensationOutcome(node_id, True,
                                                    duration_seconds=time.time() - started))
            except Exception as exc:  # best effort: keep unwinding
                outcomes.append(CompensationOutcome(node_id, False, "%s: %s" % (type(exc).__name__, exc),
                                                    time.time() - started))
        self._entries.clear()
        return outcomes


@dataclass
class RollbackReport:
    triggered_by: str
    reason: str
    compensations: List[CompensationOutcome] = field(default_factory=list)
    snapshot_restored: bool = False
    files_restored: int = 0
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    @property
    def clean(self) -> bool:
        return all(c.succeeded for c in self.compensations)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "triggered_by": self.triggered_by,
            "reason": self.reason,
            "clean": self.clean,
            "snapshot_restored": self.snapshot_restored,
            "files_restored": self.files_restored,
            "compensations": [c.to_dict() for c in self.compensations],
            "duration_seconds": round((self.completed_at or time.time()) - self.started_at, 4),
        }
