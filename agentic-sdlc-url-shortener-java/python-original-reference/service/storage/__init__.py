"""Storage adapters."""

from __future__ import annotations

from ..config import Config
from .base import LinkStore
from .memory_store import MemoryLinkStore
from .sqlite_store import SQLiteLinkStore

__all__ = ["LinkStore", "MemoryLinkStore", "SQLiteLinkStore", "build_store"]


def build_store(config: Config) -> LinkStore:
    """Select a store adapter from configuration."""
    if config.db_path in (":memory:", "memory"):
        return MemoryLinkStore()
    return SQLiteLinkStore(config.db_path)
