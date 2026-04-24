"""Configuration and tuned constants for the pgvector -> hybrid migration.

All knobs live here so the CLI, the runner, the verifier, and the test
suite share a single source of truth.  Constants are ``Final`` and
carry a rationale comment pinned to the issue or an ADR.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Tunable constants (rationale pinned in comments; do NOT inline as magic
# numbers elsewhere)
# ---------------------------------------------------------------------------

# Streaming insert batch size.  Issue #108 specifies "something sane
# e.g. 500".  Chosen so each batch comfortably fits under Neo4j's
# default transaction memory ceiling and Qdrant's 1-MiB gRPC frame for
# 768-dim float vectors (~3 KiB per vector -> ~1.5 MiB at 500 chunks;
# Qdrant accepts this in one wait=True upsert).  Overridden per run via
# ``--batch-size``.
DEFAULT_BATCH_SIZE: Final[int] = 500

# Sample size for the entity round-trip verification pass.  Large enough
# to catch field-level drift (we compare every EntityNodeView attribute)
# without turning the verify step into an O(n) entity scan.
ROUND_TRIP_SAMPLE_SIZE: Final[int] = 20

# Retrieval regression threshold.  Mirrors the #107 GraphRAG regression
# suite so migration verification and composer regression use a single
# yardstick — a drift below this means retrieval quality regressed.
TOP_K_OVERLAP_THRESHOLD: Final[float] = 0.8

# Top-k cut for retrieval verification.  Matches the #107 regression
# fixture so the two gates stay in lockstep.
VERIFY_TOP_K: Final[int] = 5

# Default progress-file path (relative to cwd).  The CLI makes this
# overridable; unit tests point it at a tmp_path.
DEFAULT_PROGRESS_FILE: Final[str] = ".migration_progress.json"

# Hard ceiling on a single upsert_graph batch.  The adapter can handle
# much more, but keeping batches bounded makes progress reporting
# granular and keeps memory usage predictable.
MAX_ENTITIES_PER_SOURCE_BATCH: Final[int] = 5_000

# Transient write retry budget for the per-source upsert.  Both Neo4j
# (bolt retry) and Qdrant (grpc retry) already have internal retries;
# this is the outer loop for transient DNS / pool-exhaustion faults.
MAX_WRITE_RETRIES: Final[int] = 3


# ---------------------------------------------------------------------------
# MigrationConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MigrationConfig:
    """Immutable, fully-resolved configuration for one migration run.

    Built by the CLI from command-line arguments + environment.  The
    runner and verifier consume this object directly; they do not reach
    into env vars or the CLI layer.
    """

    # --- Filters ---
    workspace_id: uuid.UUID | None
    """Restrict migration to a single workspace.  ``None`` = all."""

    source_id: uuid.UUID | None
    """Restrict migration to a single source.  ``None`` = all."""

    default_workspace_id: uuid.UUID | None
    """Fallback workspace for sources with ``tenant_id IS NULL``.

    When ``None``, such sources raise :class:`MissingWorkspaceError`
    and the migration aborts before any write.  This is the
    migration-level ACL enforcement point (issue #108 §ACL).
    """

    # --- Behaviour ---
    batch_size: int
    """Streaming insert batch size; see ``DEFAULT_BATCH_SIZE``."""

    dry_run: bool
    """When True, read-only: no writes to Neo4j or Qdrant."""

    resume: bool
    """When True, skip sources already listed in the progress file."""

    verify_only: bool
    """When True, skip the write phase and run only verification."""

    # --- Storage ---
    progress_file: Path
    """JSON file where per-source completion is checkpointed."""

    def __post_init__(self) -> None:
        """Validate the config; fail loud on impossible combinations."""
        if self.batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {self.batch_size}")
        if self.dry_run and self.verify_only:
            raise ValueError("dry_run and verify_only are mutually exclusive")


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_PROGRESS_FILE",
    "MAX_ENTITIES_PER_SOURCE_BATCH",
    "MAX_WRITE_RETRIES",
    "ROUND_TRIP_SAMPLE_SIZE",
    "TOP_K_OVERLAP_THRESHOLD",
    "VERIFY_TOP_K",
    "MigrationConfig",
]
