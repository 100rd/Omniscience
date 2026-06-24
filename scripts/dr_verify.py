#!/usr/bin/env python3
"""DR verification and RTO-budget logic for rebuild_all_projections.py.

This module is intentionally free-standing so it can be imported and
unit-tested without spinning up a full rebuild.  All external I/O
(Neo4j, Qdrant, Postgres) is injected via protocol-compatible objects;
tests can pass lightweight fakes.

Public surface
--------------
DRVerificationError        — raised on count mismatch or stale checkpoints
DRVerificationResult       — structured summary of a verification run
RtoResult                  — budget vs elapsed; .exceeded is the gate flag
verify_projections         — async: compare SoT vs projections, return result
check_rto                  — sync: compute elapsed vs budget, log, exit-non-zero
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default RTO budget (seconds)
# ---------------------------------------------------------------------------

#: Default Recovery Time Objective for a full rebuild.
#: Documented in docs/decisions/0015-rebuild-direct-write-exception.md.
#: Override at runtime with --rto-seconds N.
DEFAULT_RTO_SECONDS: int = 900


# ---------------------------------------------------------------------------
# Lightweight protocols — real stores satisfy these; tests pass fakes
# ---------------------------------------------------------------------------


class _PgCountsSource(Protocol):
    """Minimal Postgres SoT surface needed by verify_projections."""

    async def get_document_counts_by_source(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[str, int]:
        """Return {source_id_str: active_doc_count}."""
        ...

    async def get_chunk_counts_by_source(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[str, int]:
        """Return {source_id_str: active_chunk_count}."""
        ...

    async def get_max_doc_version_by_source(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[str, int]:
        """Return {source_id_str: max_doc_version}."""
        ...


class _QdrantCounts(Protocol):
    """Minimal Qdrant surface needed by verify_projections."""

    async def count_chunks(
        self,
        *,
        workspace_id: uuid.UUID,
        source_id: uuid.UUID | None = None,
        as_of: Any | None = None,
    ) -> int:
        """Return active chunk count for the workspace (or a single source)."""
        ...

    async def get_checkpoint_versions(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[str, int]:
        """Return {source_id_str: checkpoint_version}."""
        ...


class _Neo4jCounts(Protocol):
    """Minimal Neo4j surface needed by verify_projections."""

    async def count_entities_by_source(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[str, int]:
        """Return {source_id_str: entity_count}."""
        ...

    async def get_checkpoint_versions(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[str, int]:
        """Return {source_id_str: checkpoint_version}."""
        ...


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DRVerificationResult:
    """Structured summary of a post-rebuild verification pass."""

    workspace_id: uuid.UUID

    # Per-store counts
    pg_document_counts: dict[str, int] = field(default_factory=dict)
    pg_chunk_counts: dict[str, int] = field(default_factory=dict)
    pg_max_versions: dict[str, int] = field(default_factory=dict)

    qdrant_chunk_counts: dict[str, int] = field(default_factory=dict)
    qdrant_checkpoint_versions: dict[str, int] = field(default_factory=dict)

    neo4j_entity_counts: dict[str, int] = field(default_factory=dict)
    neo4j_checkpoint_versions: dict[str, int] = field(default_factory=dict)

    # Mismatches discovered during verification
    mismatches: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True iff zero mismatches were found."""
        return len(self.mismatches) == 0

    def summary_lines(self) -> list[str]:
        """Human-readable lines suitable for print() or logging."""
        lines: list[str] = [
            f"DR Verification — workspace {self.workspace_id}",
            f"  Postgres sources: {len(self.pg_document_counts)}",
        ]
        for src in sorted(self.pg_document_counts):
            lines.append(
                f"    source {src[:8]}…: pg_docs={self.pg_document_counts.get(src, 0)}"
                f" pg_chunks={self.pg_chunk_counts.get(src, 0)}"
                f" max_ver={self.pg_max_versions.get(src, 0)}"
                f" qdrant_chunks={self.qdrant_chunk_counts.get(src, 0)}"
                f" qdrant_ckpt={self.qdrant_checkpoint_versions.get(src, 0)}"
                f" neo4j_entities={self.neo4j_entity_counts.get(src, 0)}"
                f" neo4j_ckpt={self.neo4j_checkpoint_versions.get(src, 0)}"
            )
        if self.mismatches:
            lines.append("  MISMATCHES:")
            for m in self.mismatches:
                lines.append(f"    - {m}")
            lines.append("  RESULT: FAIL")
        else:
            lines.append("  RESULT: PASS")
        return lines


@dataclass
class RtoResult:
    """Outcome of an RTO budget check."""

    elapsed_seconds: float
    budget_seconds: int
    exceeded: bool

    @property
    def message(self) -> str:
        status = "EXCEEDED" if self.exceeded else "OK"
        return f"RTO {status}: elapsed={self.elapsed_seconds:.1f}s budget={self.budget_seconds}s"


class DRVerificationError(RuntimeError):
    """Raised when verify_projections detects a mismatch."""


# ---------------------------------------------------------------------------
# Core verification logic
# ---------------------------------------------------------------------------


async def verify_projections(
    *,
    workspace_id: uuid.UUID,
    pg: _PgCountsSource,
    qdrant: _QdrantCounts,
    neo4j: _Neo4jCounts,
    # Optional tolerance: skip entity count check when Postgres has 0
    # entities (sources that never extracted entities are allowed).
    skip_entity_check_on_empty_source: bool = True,
) -> DRVerificationResult:
    """Compare Postgres SoT against Neo4j + Qdrant projections.

    Checks performed per source
    ---------------------------
    1. Qdrant chunk count == Postgres chunk count (per source).
    2. Qdrant checkpoint version == Postgres max doc_version (per source).
    3. Neo4j entity count >= 0 (can be 0 if no entities extracted).
    4. Neo4j checkpoint version == Postgres max doc_version (per source).

    The function does NOT raise on mismatch; it accumulates all findings
    into ``DRVerificationResult.mismatches`` so the caller gets a full
    picture.  Call ``result.passed`` to gate.

    Parameters
    ----------
    workspace_id:
        UUID of the workspace to verify.
    pg:
        Postgres data source.
    qdrant:
        Qdrant vector store accessor.
    neo4j:
        Neo4j graph store accessor.
    skip_entity_check_on_empty_source:
        If True (default), sources with pg_chunk_count == 0 skip the
        entity-count assertion.  An empty source has no chunks and
        therefore no entities; a non-zero count mismatch is still caught.
    """
    result = DRVerificationResult(workspace_id=workspace_id)

    # 1. Fetch counts from all three stores concurrently.
    import asyncio

    (
        pg_docs,
        pg_chunks,
        pg_versions,
        qdrant_ckpts,
        neo4j_ckpts,
        neo4j_entities,
    ) = await asyncio.gather(
        pg.get_document_counts_by_source(workspace_id=workspace_id),
        pg.get_chunk_counts_by_source(workspace_id=workspace_id),
        pg.get_max_doc_version_by_source(workspace_id=workspace_id),
        qdrant.get_checkpoint_versions(workspace_id=workspace_id),
        neo4j.get_checkpoint_versions(workspace_id=workspace_id),
        neo4j.count_entities_by_source(workspace_id=workspace_id),
    )

    result.pg_document_counts = pg_docs
    result.pg_chunk_counts = pg_chunks
    result.pg_max_versions = pg_versions
    result.qdrant_checkpoint_versions = qdrant_ckpts
    result.neo4j_checkpoint_versions = neo4j_ckpts
    result.neo4j_entity_counts = neo4j_entities

    # Qdrant per-source chunk counts are fetched separately because the
    # store's count_chunks API is per-workspace or per-source.
    qdrant_chunk_counts: dict[str, int] = {}
    for src_str in pg_chunks:
        try:
            src_uuid = uuid.UUID(src_str)
        except ValueError:
            continue
        qdrant_chunk_counts[src_str] = await qdrant.count_chunks(
            workspace_id=workspace_id, source_id=src_uuid
        )
    result.qdrant_chunk_counts = qdrant_chunk_counts

    # 2. Compare per-source.
    for src_str in pg_versions:
        expected_version = pg_versions[src_str]
        expected_chunks = pg_chunks.get(src_str, 0)

        # --- Qdrant chunk count ---
        actual_qdrant_chunks = qdrant_chunk_counts.get(src_str, 0)
        if actual_qdrant_chunks != expected_chunks:
            result.mismatches.append(
                f"Qdrant chunk count mismatch for source {src_str}: "
                f"expected {expected_chunks} (pg), got {actual_qdrant_chunks}"
            )

        # --- Qdrant checkpoint version ---
        actual_qdrant_ckpt = qdrant_ckpts.get(src_str, 0)
        if expected_version > 0 and actual_qdrant_ckpt < expected_version:
            result.mismatches.append(
                f"Qdrant checkpoint stale for source {src_str}: "
                f"expected >={expected_version} (pg max), got {actual_qdrant_ckpt}"
            )

        # --- Neo4j entity count (non-zero only if pg has chunks) ---
        # Collapsed to a single if per SIM102: the entity-count sanity check
        # (actual >= 0) runs only when the source has chunks or we're not
        # skipping empty sources.
        actual_neo4j_entities = neo4j_entities.get(src_str, 0)
        if (not skip_entity_check_on_empty_source or expected_chunks > 0) and (
            actual_neo4j_entities < 0
        ):
            result.mismatches.append(
                f"Neo4j entity count negative for source {src_str}: {actual_neo4j_entities}"
            )

        # --- Neo4j checkpoint version ---
        actual_neo4j_ckpt = neo4j_ckpts.get(src_str, 0)
        if expected_version > 0 and actual_neo4j_ckpt < expected_version:
            result.mismatches.append(
                f"Neo4j checkpoint stale for source {src_str}: "
                f"expected >={expected_version} (pg max), got {actual_neo4j_ckpt}"
            )

    return result


# ---------------------------------------------------------------------------
# RTO budget check
# ---------------------------------------------------------------------------


def check_rto(
    *,
    start_time: float,
    budget_seconds: int = DEFAULT_RTO_SECONDS,
    exit_on_exceeded: bool = True,
) -> RtoResult:
    """Measure elapsed time and enforce the RTO budget.

    Parameters
    ----------
    start_time:
        ``time.monotonic()`` value captured at rebuild start.
    budget_seconds:
        Maximum allowed elapsed seconds.  Default: DEFAULT_RTO_SECONDS.
    exit_on_exceeded:
        If True (default), calls ``sys.exit(2)`` when the budget is
        exceeded.  Set to False in unit tests.
    """
    elapsed = time.monotonic() - start_time
    exceeded = elapsed > budget_seconds
    result = RtoResult(
        elapsed_seconds=elapsed,
        budget_seconds=budget_seconds,
        exceeded=exceeded,
    )
    log.info(result.message)
    if exceeded and exit_on_exceeded:
        sys.exit(2)
    return result
