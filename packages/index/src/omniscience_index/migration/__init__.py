"""One-shot migration tooling: pgvector -> Neo4j + Qdrant (issue #108).

This package implements the Phase-4 cutover migration that copies the
existing pgvector graph (``entities`` / ``edges``) into a live Neo4j
instance and the existing pgvector chunk table into a live Qdrant
collection, via the protocol-shaped adapters landed in #104 / #106.

Public API
----------

``MigrationConfig``
    Immutable configuration object for a single run.

``MigrationRunner``
    Orchestrates the per-source export/import loop with resume support.

``VerificationReport``
    Result of the post-migration verification pass (counts, round-trip
    sampling, retrieval overlap, cross-workspace isolation).

ACL invariant
-------------

Every row written through this module carries a non-null ``workspace_id``.
Rows sourced from pgvector carry the ``workspace_id`` derived from the
owning ``Source.tenant_id`` — the legacy pre-multi-tenancy column that
already serves as the workspace key in today's ``workspace_filter``.
Sources whose ``tenant_id`` is NULL are handled by a single explicit
``--default-workspace-id`` CLI flag; without it the migration refuses
to run so no row is written without an explicit workspace binding.
"""

from __future__ import annotations

from omniscience_index.migration.config import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_PROGRESS_FILE,
    ROUND_TRIP_SAMPLE_SIZE,
    TOP_K_OVERLAP_THRESHOLD,
    MigrationConfig,
)
from omniscience_index.migration.progress import (
    ProgressState,
    load_progress,
    save_progress,
)
from omniscience_index.migration.runner import MigrationReport, MigrationRunner
from omniscience_index.migration.verify import (
    CountMismatch,
    VerificationReport,
    VerifyRunner,
)
from omniscience_index.migration.workspace import (
    MissingWorkspaceError,
    resolve_source_workspace,
)

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_PROGRESS_FILE",
    "ROUND_TRIP_SAMPLE_SIZE",
    "TOP_K_OVERLAP_THRESHOLD",
    "CountMismatch",
    "MigrationConfig",
    "MigrationReport",
    "MigrationRunner",
    "MissingWorkspaceError",
    "ProgressState",
    "VerificationReport",
    "VerifyRunner",
    "load_progress",
    "resolve_source_workspace",
    "save_progress",
]
