"""Workspace-id resolution for the legacy pgvector -> hybrid migration.

This module is the legacy import target for the migration runner and
its tests.  As of issue #126 the canonical implementation lives in
:mod:`omniscience_index.workspace` so the live ingestion worker can
import it without reaching into the migration package.  The names
exported from here are pure re-exports of the canonical module.
"""

from __future__ import annotations

from omniscience_index.workspace import (
    MissingWorkspaceError,
    resolve_source_workspace,
)

__all__ = ["MissingWorkspaceError", "resolve_source_workspace"]
