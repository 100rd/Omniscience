"""Main migration orchestrator: pgvector -> Neo4j + Qdrant.

The :class:`MigrationRunner` is the single place that knows the
migration control flow.  It delegates reads to SQLAlchemy, writes to
the two protocol adapters (``Neo4jGraphStore`` /
``QdrantVectorStore``), tracks progress through
:mod:`~omniscience_index.migration.progress`, and honours the filters
on :class:`~omniscience_index.migration.config.MigrationConfig`.

Design notes
------------

1.  **Reuse the adapters as-is.**  Issue #108 forbids bypassing
    ``Neo4jGraphStore.upsert_entity`` / ``upsert_edge`` and
    ``QdrantVectorStore.upsert_chunks``.  Every write here goes
    through those public surfaces.

2.  **Per-source iteration.**  Sources are the natural unit of work
    because (a) ``upsert_graph`` and ``upsert_chunks`` are both
    idempotent per source, and (b) the progress file keys on
    ``source_id`` so a mid-run crash can resume cleanly.

3.  **Workspace fail-closed.**  The runner computes the effective
    workspace BEFORE calling any adapter; a missing workspace surfaces
    as :class:`MissingWorkspaceError` at the migration layer, naming
    the offending source — not as an adapter-level ValueError that
    would obscure which row caused the abort.

4.  **Dry-run is a first-class mode.**  The runner takes exactly one
    branch depending on ``config.dry_run``: read-only counts + schema
    checks, or full write path.  There is no half-measure.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import structlog
from omniscience_core.db.models import Chunk, Document, Edge, Entity, Source
from omniscience_core.storage.graph import EdgeUpsert, EntityUpsert
from omniscience_core.storage.vector import ChunkPayload
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from omniscience_index.migration.config import (
    MAX_ENTITIES_PER_SOURCE_BATCH,
    MigrationConfig,
)
from omniscience_index.migration.progress import (
    ProgressState,
    load_progress,
    new_progress_state,
    save_progress,
)
from omniscience_index.migration.workspace import (
    MissingWorkspaceError,
    resolve_source_workspace,
)
from omniscience_index.stores.neo4j_store import Neo4jGraphStore
from omniscience_index.stores.qdrant_store import QdrantVectorStore

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SourceReport:
    """Per-source migration outcome (counts + status)."""

    source_id: uuid.UUID
    source_name: str
    workspace_id: uuid.UUID
    entities_copied: int = 0
    edges_copied: int = 0
    chunks_copied: int = 0
    documents_scanned: int = 0
    skipped: bool = False
    skip_reason: str | None = None


@dataclass(slots=True)
class MigrationReport:
    """Top-level result returned from :meth:`MigrationRunner.run`."""

    dry_run: bool
    source_reports: list[SourceReport] = field(default_factory=list)

    @property
    def total_entities(self) -> int:
        return sum(r.entities_copied for r in self.source_reports)

    @property
    def total_edges(self) -> int:
        return sum(r.edges_copied for r in self.source_reports)

    @property
    def total_chunks(self) -> int:
        return sum(r.chunks_copied for r in self.source_reports)

    @property
    def sources_processed(self) -> int:
        return sum(1 for r in self.source_reports if not r.skipped)

    @property
    def sources_skipped(self) -> int:
        return sum(1 for r in self.source_reports if r.skipped)


# ---------------------------------------------------------------------------
# MigrationRunner
# ---------------------------------------------------------------------------


class MigrationRunner:
    """Orchestrates the migration of pgvector data into Neo4j + Qdrant.

    Lifecycle
    ---------
    Call sites are expected to manage the store lifecycles externally
    (``graph_store.connect()`` / ``vector_store.connect()`` before the
    first :meth:`run` call).  The runner does not own these — the CLI
    opens/closes them symmetrically.
    """

    def __init__(
        self,
        *,
        config: MigrationConfig,
        session_factory: async_sessionmaker[AsyncSession],
        graph_store: Neo4jGraphStore,
        vector_store: QdrantVectorStore,
    ) -> None:
        self._config = config
        self._session_factory = session_factory
        self._graph_store = graph_store
        self._vector_store = vector_store

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> MigrationReport:
        """Execute the migration per :attr:`self._config`."""
        report = MigrationReport(dry_run=self._config.dry_run)
        progress = self._load_or_create_progress()

        async with self._session_factory() as session:
            sources = await self._select_sources(session)
            for source in sources:
                source_report = await self._process_source(
                    session=session,
                    source=source,
                    progress=progress,
                )
                report.source_reports.append(source_report)

        log.info(
            "migration_complete",
            dry_run=self._config.dry_run,
            sources_processed=report.sources_processed,
            sources_skipped=report.sources_skipped,
            entities=report.total_entities,
            edges=report.total_edges,
            chunks=report.total_chunks,
        )
        return report

    # ------------------------------------------------------------------
    # Progress-file plumbing
    # ------------------------------------------------------------------

    def _load_or_create_progress(self) -> ProgressState:
        """Read the progress file when ``--resume`` is set, else new state.

        When ``--resume`` is passed and the file is absent, we treat
        that as "nothing to resume from" and start fresh; this keeps
        the first run ergonomic.  A malformed progress file is raised
        up — see :func:`load_progress` for the rationale.
        """
        if not self._config.resume:
            return new_progress_state(batch_size=self._config.batch_size)
        existing = load_progress(self._config.progress_file)
        if existing is None:
            log.info(
                "migration_resume_no_progress_file",
                path=str(self._config.progress_file),
            )
            return new_progress_state(batch_size=self._config.batch_size)
        log.info(
            "migration_resume_from_progress",
            path=str(self._config.progress_file),
            completed=len(existing.completed_source_ids),
        )
        return existing

    # ------------------------------------------------------------------
    # Source selection
    # ------------------------------------------------------------------

    async def _select_sources(self, session: AsyncSession) -> list[Source]:
        """Return the list of sources that pass the CLI filters."""
        stmt = select(Source).order_by(Source.created_at)
        if self._config.source_id is not None:
            stmt = stmt.where(Source.id == self._config.source_id)
        if self._config.workspace_id is not None:
            stmt = stmt.where(Source.tenant_id == self._config.workspace_id)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Per-source loop
    # ------------------------------------------------------------------

    async def _process_source(
        self,
        *,
        session: AsyncSession,
        source: Source,
        progress: ProgressState,
    ) -> SourceReport:
        """Migrate one source (or report skip when resume/dry-run says so).

        The workspace is resolved BEFORE any adapter call so a missing
        workspace aborts loud with a migration-level error naming the
        offending source.
        """
        workspace_id = resolve_source_workspace(
            source, default_workspace_id=self._config.default_workspace_id
        )
        src_report = SourceReport(
            source_id=source.id,
            source_name=source.name,
            workspace_id=workspace_id,
        )
        if self._config.resume and progress.is_completed(source.id):
            src_report.skipped = True
            src_report.skip_reason = "already_completed"
            log.info(
                "migration_skip_completed_source",
                source_id=str(source.id),
                source_name=source.name,
            )
            return src_report

        if self._config.dry_run:
            await self._dry_run_source(session, source, src_report)
            return src_report

        await self._migrate_source_graph(session, source, workspace_id, src_report)
        await self._migrate_source_chunks(session, source, workspace_id, src_report)

        progress.mark_source_completed(source.id)
        save_progress(progress, self._config.progress_file)
        return src_report

    # ------------------------------------------------------------------
    # Dry-run path
    # ------------------------------------------------------------------

    async def _dry_run_source(
        self,
        session: AsyncSession,
        source: Source,
        src_report: SourceReport,
    ) -> None:
        """Read-only: count rows per store, write nothing."""
        entities = await _count_entities_for_source(session, source.id)
        edges = await _count_edges_for_source(session, source.id)
        chunks = await _count_chunks_for_source(session, source.id)
        documents = await _count_documents_for_source(session, source.id)
        src_report.entities_copied = entities
        src_report.edges_copied = edges
        src_report.chunks_copied = chunks
        src_report.documents_scanned = documents
        log.info(
            "migration_dry_run_source",
            source_id=str(source.id),
            source_name=source.name,
            entities=entities,
            edges=edges,
            chunks=chunks,
            documents=documents,
        )

    # ------------------------------------------------------------------
    # Graph migration
    # ------------------------------------------------------------------

    async def _migrate_source_graph(
        self,
        session: AsyncSession,
        source: Source,
        workspace_id: uuid.UUID,
        src_report: SourceReport,
    ) -> None:
        """Copy all entities + edges for one source into Neo4j.

        Uses ``upsert_entity`` / ``upsert_edge`` on the adapter so each
        MERGE is idempotent.  Entities are inserted before edges so the
        MATCH endpoints in the edge upsert resolve.
        """
        entities = await _fetch_entities_for_source(session, source.id)
        if len(entities) > MAX_ENTITIES_PER_SOURCE_BATCH:
            log.warning(
                "migration_large_entity_batch",
                source_id=str(source.id),
                count=len(entities),
                ceiling=MAX_ENTITIES_PER_SOURCE_BATCH,
            )
        for batch in _chunked(entities, self._config.batch_size):
            for ent in batch:
                await self._graph_store.upsert_entity(
                    entity=EntityUpsert(
                        id=ent.id,
                        source_id=ent.source_id,
                        entity_type=ent.entity_type,
                        name=ent.name,
                        display_name=ent.display_name,
                        chunk_id=ent.chunk_id,
                        metadata=dict(ent.entity_metadata or {}),
                    ),
                    workspace_id=workspace_id,
                )
            src_report.entities_copied += len(batch)

        edges = await _fetch_edges_for_source(session, source.id)
        for batch in _chunked(edges, self._config.batch_size):
            for edge in batch:
                edge_metadata = dict(edge.edge_metadata or {})
                edge_metadata.setdefault("source_id", str(source.id))
                await self._graph_store.upsert_edge(
                    edge=EdgeUpsert(
                        source_entity_id=edge.source_entity_id,
                        target_entity_id=edge.target_entity_id,
                        edge_type=edge.edge_type,
                        metadata=edge_metadata,
                    ),
                    workspace_id=workspace_id,
                )
            src_report.edges_copied += len(batch)

        log.info(
            "migration_graph_source_done",
            source_id=str(source.id),
            entities=src_report.entities_copied,
            edges=src_report.edges_copied,
        )

    # ------------------------------------------------------------------
    # Chunk (vector) migration
    # ------------------------------------------------------------------

    async def _migrate_source_chunks(
        self,
        session: AsyncSession,
        source: Source,
        workspace_id: uuid.UUID,
        src_report: SourceReport,
    ) -> None:
        """Copy all documents+chunks for one source into Qdrant.

        Groups by document so each ``upsert_chunks`` call lines up with
        the adapter's decision table (created / updated / unchanged).
        ``workspace_id`` is injected into ``metadata[workspace_id]`` so
        :func:`QdrantVectorStore._extract_workspace_id` accepts the
        upsert — the adapter's ACL gate is never bypassed.
        """
        async for document, chunks in _iter_document_chunks(session, source.id):
            src_report.documents_scanned += 1
            if not chunks:
                continue
            for batch in _chunked(chunks, self._config.batch_size):
                payloads = [_chunk_to_payload(c) for c in batch]
                doc_metadata = _build_document_metadata(
                    document=document,
                    workspace_id=workspace_id,
                )
                await self._vector_store.upsert_chunks(
                    source_id=source.id,
                    external_id=document.external_id,
                    uri=document.uri,
                    title=document.title,
                    content_hash=document.content_hash,
                    metadata=doc_metadata,
                    chunks=payloads,
                )
                src_report.chunks_copied += len(batch)

        log.info(
            "migration_chunks_source_done",
            source_id=str(source.id),
            documents=src_report.documents_scanned,
            chunks=src_report.chunks_copied,
        )


# ---------------------------------------------------------------------------
# Free helpers (kept small + pure so unit tests can exercise them alone)
# ---------------------------------------------------------------------------


def _chunked(items: list[Any], size: int) -> list[list[Any]]:
    """Split ``items`` into fixed-size chunks; last chunk may be short."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def _chunk_to_payload(chunk: Chunk) -> ChunkPayload:
    """Translate a pgvector :class:`Chunk` row to a :class:`ChunkPayload`.

    The embedding is normalised to ``list[float]`` because pgvector
    returns ``numpy.ndarray`` from the ORM and Qdrant's client expects
    plain Python floats on the wire.
    """
    raw_embedding = chunk.embedding
    embedding: list[float] = [] if raw_embedding is None else [float(x) for x in raw_embedding]
    payload: ChunkPayload = {
        "ord": int(chunk.ord),
        "text": str(chunk.text),
        "embedding": embedding,
        "symbol": chunk.symbol,
        "metadata": dict(chunk.chunk_metadata or {}),
        "embedding_model": str(chunk.embedding_model or ""),
        "embedding_provider": str(chunk.embedding_provider or ""),
        "parser_version": str(chunk.parser_version or ""),
        "chunker_strategy": str(chunk.chunker_strategy or ""),
    }
    return payload


def _build_document_metadata(
    *,
    document: Document,
    workspace_id: uuid.UUID,
) -> dict[str, Any]:
    """Construct the document-level metadata dict the Qdrant adapter needs.

    The ``workspace_id`` key is mandatory — it is the ACL enforcement
    point inside ``QdrantVectorStore._extract_workspace_id`` (ADR-0006
    §ACL).  Missing / null causes the adapter to reject the upsert.
    """
    metadata: dict[str, Any] = dict(document.doc_metadata or {})
    metadata["workspace_id"] = str(workspace_id)
    return metadata


async def _fetch_entities_for_source(
    session: AsyncSession,
    source_id: uuid.UUID,
) -> list[Entity]:
    stmt = select(Entity).where(Entity.source_id == source_id).order_by(Entity.created_at)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _fetch_edges_for_source(
    session: AsyncSession,
    source_id: uuid.UUID,
) -> list[Edge]:
    """Fetch edges whose source-entity belongs to ``source_id``.

    Edges sit on ``entities`` transitively (Edge has no ``source_id``
    column of its own) — an edge is considered "owned" by the source
    that owns its ``source_entity``.  Cross-source edges (source_entity
    and target_entity live in different sources) will be returned from
    whichever source is being processed; idempotent upsert makes a
    second insertion from the other side a no-op.
    """
    stmt = (
        select(Edge)
        .join(Entity, Entity.id == Edge.source_entity_id)
        .where(Entity.source_id == source_id)
        .order_by(Edge.created_at)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _iter_document_chunks(
    session: AsyncSession,
    source_id: uuid.UUID,
) -> AsyncIterator[tuple[Document, list[Chunk]]]:
    """Yield (document, its_chunks) one document at a time.

    Tombstoned documents are skipped — we do not migrate rows the old
    path would hide from search.  Chunks come back ordered by ``ord``
    so the downstream upsert preserves in-document ordering.
    """
    doc_stmt = (
        select(Document)
        .where(Document.source_id == source_id)
        .where(Document.tombstoned_at.is_(None))
        .order_by(Document.indexed_at)
    )
    doc_result = await session.execute(doc_stmt)
    documents = list(doc_result.scalars().all())
    for doc in documents:
        chunk_stmt = select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.ord)
        chunk_result = await session.execute(chunk_stmt)
        chunks = list(chunk_result.scalars().all())
        yield doc, chunks


async def _count_entities_for_source(
    session: AsyncSession,
    source_id: uuid.UUID,
) -> int:
    stmt = select(func.count()).select_from(Entity).where(Entity.source_id == source_id)
    result = await session.execute(stmt)
    return int(result.scalar_one())


async def _count_edges_for_source(
    session: AsyncSession,
    source_id: uuid.UUID,
) -> int:
    stmt = (
        select(func.count())
        .select_from(Edge)
        .join(Entity, Entity.id == Edge.source_entity_id)
        .where(Entity.source_id == source_id)
    )
    result = await session.execute(stmt)
    return int(result.scalar_one())


async def _count_chunks_for_source(
    session: AsyncSession,
    source_id: uuid.UUID,
) -> int:
    stmt = (
        select(func.count())
        .select_from(Chunk)
        .join(Document, Document.id == Chunk.document_id)
        .where(Document.source_id == source_id)
        .where(Document.tombstoned_at.is_(None))
    )
    result = await session.execute(stmt)
    return int(result.scalar_one())


async def _count_documents_for_source(
    session: AsyncSession,
    source_id: uuid.UUID,
) -> int:
    stmt = (
        select(func.count())
        .select_from(Document)
        .where(Document.source_id == source_id)
        .where(Document.tombstoned_at.is_(None))
    )
    result = await session.execute(stmt)
    return int(result.scalar_one())


__all__ = [
    "MigrationReport",
    "MigrationRunner",
    "MissingWorkspaceError",
    "SourceReport",
]
