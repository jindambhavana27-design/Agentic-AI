"""Scenario workflows.

Each module exposes ``build(inputs) -> WorkflowGraph`` and ``default_inputs()``,
so the CLI can treat them uniformly and so a new scenario is a new module rather
than a change to the engine.
"""

from . import ambiguous, brownfield, greenfield
from .common import prepare_workspace, project_root, verification_tail

REGISTRY = {
    "greenfield": greenfield,
    "brownfield": brownfield,
    "ambiguous": ambiguous,
}

__all__ = ["REGISTRY", "greenfield", "brownfield", "ambiguous",
           "prepare_workspace", "project_root", "verification_tail"]
