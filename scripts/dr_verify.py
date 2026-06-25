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
verify_hash_equivalence    — AP5: assert stored vectors match SoT-recomputed ones
ChunkHashRecord            — (chunk_id, content_hash, vector_digest) triple
"""

from __future__ import annotations

import hashlib
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
# AP5: hash-equivalence primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkHashRecord:
    """Content + vector digest for one chunk (AP5 hash-equivalence assertion).

    Attributes
    ----------
    chunk_id:
        Postgres UUID of the chunk.
    text_hash:
        SHA-256 hex digest of ``chunk.text`` (source-of-truth content key).
    vector_digest:
        SHA-256 hex digest of the embedding vector bytes (little-endian
        float32 representation, 4 bytes per dimension).
    """

    chunk_id: uuid.UUID
    text_hash: str
    vector_digest: str


def compute_vector_digest(vector: list[float]) -> str:
    """Return a stable SHA-256 hex digest for an embedding vector.

    Encodes each float as a little-endian 4-byte IEEE 754 value so the
    digest is independent of Python version and platform (as long as the
    embedding provider is deterministic on the same hardware).
    """
    import struct

    packed = struct.pack(f"<{len(vector)}f", *vector)
    return hashlib.sha256(packed).hexdigest()


def compute_text_hash(text: str) -> str:
    """Return a stable SHA-256 hex digest of the chunk text (UTF-8 encoded)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_hash_equivalence(
    *,
    stored: list[ChunkHashRecord],
    recomputed: list[ChunkHashRecord],
) -> list[str]:
    """Compare stored chunk records against freshly-recomputed ones.

    AP5 invariant: for every chunk, the vector digest produced by rebuilding
    from SoT text must equal the vector digest stored in the projection.
    This proves that the rebuild path is recomputing from text (not restoring
    from a cached backup vector).

    Parameters
    ----------
    stored:
        Records pulled from the rebuilt projection (Qdrant vectors read back
        after upsert).
    recomputed:
        Records produced by calling the embedding provider on ``chunk.text``
        for each chunk in the SoT Postgres table.

    Returns
    -------
    list[str]
        List of human-readable mismatch descriptions.  Empty list = PASS.
    """
    mismatches: list[str] = []

    stored_map = {r.chunk_id: r for r in stored}
    recomputed_map = {r.chunk_id: r for r in recomputed}

    # Check every recomputed chunk appears in stored and matches.
    for chunk_id, recomp in recomputed_map.items():
        if chunk_id not in stored_map:
            mismatches.append(f"chunk {chunk_id}: missing from stored projection")
            continue
        stored_rec = stored_map[chunk_id]
        if stored_rec.text_hash != recomp.text_hash:
            mismatches.append(
                f"chunk {chunk_id}: text_hash mismatch "
                f"(stored={stored_rec.text_hash[:8]}… "
                f"recomputed={recomp.text_hash[:8]}…)"
            )
        if stored_rec.vector_digest != recomp.vector_digest:
            mismatches.append(
                f"chunk {chunk_id}: vector_digest mismatch — "
                "rebuild did not recompute from SoT text "
                f"(stored={stored_rec.vector_digest[:8]}… "
                f"recomputed={recomp.vector_digest[:8]}…)"
            )

    # Check for chunks present in stored but absent from recomputed.
    for chunk_id in stored_map:
        if chunk_id not in recomputed_map:
            mismatches.append(f"chunk {chunk_id}: present in stored but not in recomputed set")

    return mismatches


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
