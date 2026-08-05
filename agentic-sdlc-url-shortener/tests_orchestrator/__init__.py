"""Tests for the orchestration layer.

Kept separate from ``tests/`` because that package is copied into every run
workspace and executed by the test agent. Putting these here avoids the
orchestrator testing itself recursively inside its own runs.
"""
