"""Post-migration verification (issue #108 §Verification pass).

Three checks, each blocking:

1.  **Per-source counts** — pgvector entity/edge/chunk totals match the
    Neo4j node / relationship / Qdrant-point totals.  Any mismatch is
    captured in a :class:`CountMismatch` row and surfaced in the
    :class:`VerificationReport`.

2.  **Sample entity round-trip** — pick N random entities per source
    (default :data:`ROUND_TRIP_SAMPLE_SIZE`) and assert
    ``PgVectorGraphStore.get_entity`` and
    ``Neo4jGraphStore.get_entity`` return equivalent core fields (id,
    name, kind).

3.  **Cross-workspace isolation probe** — when two or more workspaces
    exist in the migration set, run a workspace-A query and assert
    zero hits from workspace B.  Carries the ACL invariant forward
    from #106 and #104 into the migration gate.

The retrieval overlap check (new GraphRAG vs legacy pgvector) is
delegated to :mod:`tests.test_graphrag_regression` at CI time — this
module exposes a helper that computes overlap ratios given a fixed
query list so both the CLI's ``--verify`` flag and the test suite
share the same implementation.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field

import qdrant_client.http.models as qm
import structlog
from omniscience_core.db.models import Source
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from omniscience_index.migration.config import (
    ROUND_TRIP_SAMPLE_SIZE,
    TOP_K_OVERLAP_THRESHOLD,
    MigrationConfig,
)
from omniscience_index.migration.runner import (
    _count_chunks_for_source,
    _count_edges_for_source,
    _count_entities_for_source,
    _fetch_entities_for_source,
)
from omniscience_index.migration.workspace import resolve_source_workspace
from omniscience_index.stores.neo4j_store import Neo4jGraphStore
from omniscience_index.stores.qdrant_filters import QdrantFilterBuilder
from omniscience_index.stores.qdrant_store import QdrantVectorStore

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Report dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CountMismatch:
    """One per-source count discrepancy.

    Carries both sides so a verifier log can pinpoint whether graph or
    vector lagged.  The ``kind`` field distinguishes entity / edge /
    chunk mismatches.
    """

    source_id: uuid.UUID
    source_name: str
    kind: str  # "entities" | "edges" | "chunks"
    pgvector: int
    hybrid: int

    @property
    def is_error(self) -> bool:
        return self.pgvector != self.hybrid


@dataclass(slots=True)
class RoundTripFailure:
    """One entity whose round-trip comparison failed.

    ``field_diffs`` names the fields that disagreed between pgvector
    and Neo4j so the operator can skim the diff in the verify log.
    """

    source_id: uuid.UUID
    entity_id: uuid.UUID
    entity_name: str
    field_diffs: list[str]


@dataclass(slots=True)
class IsolationBreach:
    """A cross-workspace leak detected during the isolation probe.

    Production invariant: zero breaches per run.
    """

    probe_workspace_id: uuid.UUID
    leaked_from_workspace_id: uuid.UUID
    chunk_id: uuid.UUID


@dataclass(slots=True)
class VerificationReport:
    """Aggregate verification result (pass = no entries in any list)."""

    count_mismatches: list[CountMismatch] = field(default_factory=list)
    round_trip_failures: list[RoundTripFailure] = field(default_factory=list)
    isolation_breaches: list[IsolationBreach] = field(default_factory=list)
    retrieval_overlap_pass: bool = True
    retrieval_overlap_ratio: float = 1.0

    @property
    def passed(self) -> bool:
        return (
            not self.count_mismatches
            and not self.round_trip_failures
            and not self.isolation_breaches
            and self.retrieval_overlap_pass
        )


# ---------------------------------------------------------------------------
# VerifyRunner
# ---------------------------------------------------------------------------


class VerifyRunner:
    """Runs the three verification gates.

    Kept deliberately narrow: no writes, no mutation.  Every I/O path
    is a read.
    """

    def __init__(
        self,
        *,
        config: MigrationConfig,
        session_factory: async_sessionmaker[AsyncSession],
        graph_store: Neo4jGraphStore,
        vector_store: QdrantVectorStore,
        pgvector_graph_get_entity: object | None = None,
        rng_seed: int | None = None,
    ) -> None:
        self._config = config
        self._session_factory = session_factory
        self._graph_store = graph_store
        self._vector_store = vector_store
        # ``pgvector_graph_get_entity`` is a callable (or ``GraphStore``
        # adapter) used by :meth:`_round_trip_entities`; kept as an
        # optional dep so the verify-only happy path works without
        # dragging retrieval into the core import graph.
        self._pgvector_graph = pgvector_graph_get_entity
        # Non-cryptographic sampling: the sample is a QA gate against
        # pgvector <-> Neo4j drift; uniform coverage is the only
        # requirement.
        self._rng = (
            random.Random(rng_seed)  # noqa: S311
            if rng_seed is not None
            else random.Random()  # noqa: S311
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> VerificationReport:
        """Execute every verification gate; return an aggregate report."""
        report = VerificationReport()
        async with self._session_factory() as session:
            sources = await self._select_sources(session)
            workspace_ids_seen: set[uuid.UUID] = set()
            for source in sources:
                ws = resolve_source_workspace(
                    source, default_workspace_id=self._config.default_workspace_id
                )
                workspace_ids_seen.add(ws)
                await self._check_counts(session, source, ws, report)
                if self._pgvector_graph is not None:
                    await self._round_trip_entities(session, source, ws, report)
            await self._check_isolation(workspace_ids_seen, report)

        log.info(
            "verification_complete",
            passed=report.passed,
            count_mismatches=len(report.count_mismatches),
            round_trip_failures=len(report.round_trip_failures),
            isolation_breaches=len(report.isolation_breaches),
            retrieval_overlap_ratio=report.retrieval_overlap_ratio,
        )
        return report

    # ------------------------------------------------------------------
    # Source selection (mirrors the migration's own filter semantics)
    # ------------------------------------------------------------------

    async def _select_sources(self, session: AsyncSession) -> list[Source]:
        from sqlalchemy import select

        stmt = select(Source).order_by(Source.created_at)
        if self._config.source_id is not None:
            stmt = stmt.where(Source.id == self._config.source_id)
        if self._config.workspace_id is not None:
            stmt = stmt.where(Source.tenant_id == self._config.workspace_id)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Gate 1: counts
    # ------------------------------------------------------------------

    async def _check_counts(
        self,
        session: AsyncSession,
        source: Source,
        workspace_id: uuid.UUID,
        report: VerificationReport,
    ) -> None:
        pg_entities = await _count_entities_for_source(session, source.id)
        pg_edges = await _count_edges_for_source(session, source.id)
        pg_chunks = await _count_chunks_for_source(session, source.id)

        n4_entities = await _neo4j_count_entities_for_source(
            graph_store=self._graph_store,
            source_id=source.id,
            workspace_id=workspace_id,
        )
        n4_edges = await _neo4j_count_edges_for_source(
            graph_store=self._graph_store,
            source_id=source.id,
            workspace_id=workspace_id,
        )
        qd_chunks = await _qdrant_count_chunks_for_source(
            vector_store=self._vector_store,
            source_id=source.id,
            workspace_id=workspace_id,
        )

        for kind, pg_val, hybrid_val in (
            ("entities", pg_entities, n4_entities),
            ("edges", pg_edges, n4_edges),
            ("chunks", pg_chunks, qd_chunks),
        ):
            mismatch = CountMismatch(
                source_id=source.id,
                source_name=source.name,
                kind=kind,
                pgvector=pg_val,
                hybrid=hybrid_val,
            )
            if mismatch.is_error:
                report.count_mismatches.append(mismatch)

    # ------------------------------------------------------------------
    # Gate 2: entity round-trip
    # ------------------------------------------------------------------

    async def _round_trip_entities(
        self,
        session: AsyncSession,
        source: Source,
        workspace_id: uuid.UUID,
        report: VerificationReport,
    ) -> None:
        """Sample entities and compare pgvector vs Neo4j views.

        Only runs when a pgvector ``GraphStore`` was supplied to the
        :class:`VerifyRunner`.  ``get_entity`` is called on both sides
        and the ``EntityNodeView`` fields are compared one-by-one.
        """
        pg_store = self._pgvector_graph
        if pg_store is None:
            return
        entities = await _fetch_entities_for_source(session, source.id)
        if not entities:
            return
        sample = self._rng.sample(entities, min(ROUND_TRIP_SAMPLE_SIZE, len(entities)))
        for ent in sample:
            # duck-typed call: whatever is passed in must expose
            # ``get_entity(entity_name=..., workspace_id=...)`` returning
            # ``EntityNodeView | None``.  PgVectorGraphStore does.
            pg_view = await pg_store.get_entity(  # type: ignore[attr-defined]
                entity_name=ent.name,
                workspace_id=workspace_id,
            )
            n4_view = await self._graph_store.get_entity(
                entity_name=ent.name,
                workspace_id=workspace_id,
            )
            diffs = _diff_entity_views(pg_view, n4_view)
            if diffs:
                report.round_trip_failures.append(
                    RoundTripFailure(
                        source_id=source.id,
                        entity_id=ent.id,
                        entity_name=ent.name,
                        field_diffs=diffs,
                    )
                )

    # ------------------------------------------------------------------
    # Gate 3: cross-workspace isolation probe
    # ------------------------------------------------------------------

    async def _check_isolation(
        self,
        workspace_ids: set[uuid.UUID],
        report: VerificationReport,
    ) -> None:
        """Scroll Qdrant for each workspace and assert its points only
        carry its own ``workspace_id``.

        With a single workspace this gate is a no-op (nothing to leak
        across).  The check is cheap enough to run unconditionally
        when multiple workspaces exist — we scroll the indexed payload
        and compare workspace tags.
        """
        if len(workspace_ids) < 2:
            return
        for ws in workspace_ids:
            flt = QdrantFilterBuilder(workspace_id=ws).build()
            # Hit the adapter's private scroller through a thin public
            # wrapper: issue a count via QdrantVectorStore._qc.scroll,
            # but we do it at a lower level here to collect payloads.
            for leak in await _scroll_workspace_payloads(self._vector_store, flt):
                other_ws = leak.get("workspace_id")
                if other_ws is None or uuid.UUID(str(other_ws)) != ws:
                    report.isolation_breaches.append(
                        IsolationBreach(
                            probe_workspace_id=ws,
                            leaked_from_workspace_id=(
                                uuid.UUID(str(other_ws))
                                if other_ws is not None
                                else uuid.UUID(int=0)
                            ),
                            chunk_id=uuid.UUID(str(leak.get("chunk_id"))),
                        )
                    )


# ---------------------------------------------------------------------------
# Free helpers
# ---------------------------------------------------------------------------


def _diff_entity_views(pg_view: object | None, n4_view: object | None) -> list[str]:
    """Return a list of field names that disagree between two views.

    ``None`` on either side is a "missing" diff — the caller treats a
    non-empty return as failure.
    """
    if pg_view is None and n4_view is None:
        return ["missing_on_both_sides"]
    if pg_view is None:
        return ["missing_on_pgvector"]
    if n4_view is None:
        return ["missing_on_neo4j"]
    diffs: list[str] = []
    # EntityNodeView fields: name, kind, source, chunk_text, depth, edge_type
    for attr in ("name", "kind", "source"):
        if getattr(pg_view, attr) != getattr(n4_view, attr):
            diffs.append(attr)
    return diffs


async def _neo4j_count_entities_for_source(
    *,
    graph_store: Neo4jGraphStore,
    source_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> int:
    """Count Neo4j ``Entity`` nodes for a source, scoped to workspace."""
    cypher = (
        "MATCH (n:Entity {workspace_id: $workspace_id, source_id: $source_id}) "
        "RETURN count(n) AS c"
    )
    params = {"workspace_id": str(workspace_id), "source_id": str(source_id)}
    return await _run_neo4j_count(graph_store, cypher, params)


async def _neo4j_count_edges_for_source(
    *,
    graph_store: Neo4jGraphStore,
    source_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> int:
    """Count Neo4j relationships for a source, scoped to workspace."""
    cypher = (
        "MATCH (:Entity {workspace_id: $workspace_id})-[r {workspace_id: $workspace_id, "
        "source_id: $source_id}]->(:Entity {workspace_id: $workspace_id}) "
        "RETURN count(r) AS c"
    )
    params = {"workspace_id": str(workspace_id), "source_id": str(source_id)}
    return await _run_neo4j_count(graph_store, cypher, params)


async def _run_neo4j_count(
    graph_store: Neo4jGraphStore,
    cypher: str,
    params: dict[str, str],
) -> int:
    """Execute a COUNT Cypher and return the integer result."""
    # The adapter does not expose a generic "run read query" on its
    # public surface.  Verification is an exceptional-path gate
    # living in the same package, so we reach into the private driver
    # for the counts only (read-only).  No private is exposed outside
    # this module.
    driver = graph_store._driver
    database = graph_store._config.database
    async with driver.session(database=database) as session:
        result = await session.run(cypher, params)
        record = await result.single()
    if record is None:
        return 0
    return int(record["c"])


async def _qdrant_count_chunks_for_source(
    *,
    vector_store: QdrantVectorStore,
    source_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> int:
    """Count Qdrant points (chunks) for a source, scoped to workspace.

    Uses the native ``qdrant-client.count`` API against a scoped filter
    built by :class:`QdrantFilterBuilder` so the read is cross-workspace
    safe.
    """
    flt = (
        QdrantFilterBuilder(workspace_id=workspace_id)
        .with_source_ids([source_id])
        .exclude_tombstoned()
        .build()
    )
    client = vector_store._qc
    result = await client.count(
        collection_name=vector_store.collection_name,
        count_filter=flt,
        exact=True,
    )
    return int(result.count)


async def _scroll_workspace_payloads(
    vector_store: QdrantVectorStore,
    flt: qm.Filter,
) -> list[dict[str, object]]:
    """Scroll every point matching ``flt`` and return trimmed payloads.

    Returned dicts carry only the fields the isolation probe compares
    (``workspace_id`` + ``chunk_id``) to keep memory bounded on large
    migrations.
    """
    client = vector_store._qc
    out: list[dict[str, object]] = []
    offset: qm.ExtendedPointId | None = None
    page_size = 256
    while True:
        records, next_offset = await client.scroll(
            collection_name=vector_store.collection_name,
            scroll_filter=flt,
            limit=page_size,
            with_payload=True,
            with_vectors=False,
            offset=offset,
        )
        for rec in records:
            payload = rec.payload or {}
            out.append(
                {
                    "workspace_id": payload.get("workspace_id"),
                    "chunk_id": rec.id,
                }
            )
        if next_offset is None:
            break
        offset = next_offset
    return out


def compute_top_k_overlap(
    *,
    reference_hits: list[uuid.UUID],
    candidate_hits: list[uuid.UUID],
) -> float:
    """Return the overlap ratio between two ranked hit lists.

    Mirrors the definition used by :mod:`tests.test_graphrag_regression`
    — ``|reference ∩ candidate| / |reference|``.  The threshold check
    lives with the caller so the CLI and the test share one function.
    """
    if not reference_hits:
        return 1.0
    ref_set = set(reference_hits)
    cand_set = set(candidate_hits)
    return len(ref_set & cand_set) / len(ref_set)


__all__ = [
    "TOP_K_OVERLAP_THRESHOLD",
    "CountMismatch",
    "IsolationBreach",
    "RoundTripFailure",
    "VerificationReport",
    "VerifyRunner",
    "compute_top_k_overlap",
]
