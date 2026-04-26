"""StatsService — workspace-scoped operator stats (issue #111).

Aggregates rollups from three stores:

* **Postgres** (operational metadata: sources, documents, ingestion runs)
  via SQLAlchemy.
* **Neo4j** (entities + edges) via the ``GraphStore`` protocol.
* **Qdrant** (chunks) via the ``VectorStore`` protocol.

Endpoints in ``apps/server/src/omniscience_server/rest/stats.py`` call this
service exclusively — they never import SQLAlchemy models or the storage
adapters directly.  This is the single place that knows how stats are
computed, which makes the admin-UI surface trivially backend-neutral.

ACL invariant
-------------
Every public method takes ``workspace_id: uuid.UUID`` keyword-only and
forwards it to all three stores.  The Postgres queries use
:func:`workspace_filter` so legacy rows with ``tenant_id IS NULL``
remain visible (matches the rest of the v0.2 query layer).  Neo4j and
Qdrant enforce strict workspace isolation natively — see ADR-0005 and
ADR-0006.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from omniscience_core.auth.workspace import workspace_filter
from omniscience_core.db.models import Chunk, Document, Source, SourceStatus
from omniscience_core.stats.cache import STATS_CACHE_TTL_SECONDS, TTLCache
from omniscience_core.stats.models import (
    EdgesByTypeResponse,
    EdgeTypeHistogramEntry,
    EntitiesByKindResponse,
    KindHistogramEntry,
    SourcesStatsResponse,
    SourceStatsRow,
    StatsOverview,
)
from omniscience_core.storage.graph import GraphStore
from omniscience_core.storage.vector import VectorStore

log = structlog.get_logger(__name__)

# Cache method-name constants so refactors flow through one place.  These
# values key the in-process TTL cache; a typo would silently cache-miss.
_METHOD_OVERVIEW: Final[str] = "overview"
_METHOD_SOURCES: Final[str] = "sources"
_METHOD_ENTITIES_BY_KIND: Final[str] = "entities_by_kind"
_METHOD_EDGES_BY_TYPE: Final[str] = "edges_by_type"

# Window for "last 24h" deltas.  A constant keeps the SQL trivially
# parameterised and the unit tests trivially controllable via fake clocks.
_DELTA_WINDOW: Final[timedelta] = timedelta(hours=24)


class StatsService:
    """Operator-stats aggregator across Postgres + Neo4j + Qdrant.

    All methods are coroutine-safe and bounded in latency by the TTL
    cache (default 10s).  The service is intended to be instantiated
    once per process and shared across requests.
    """

    def __init__(
        self,
        *,
        db_session_factory: async_sessionmaker[AsyncSession],
        graph_store: GraphStore,
        vector_store: VectorStore,
        cache: TTLCache[object] | None = None,
    ) -> None:
        self._session_factory = db_session_factory
        self._graph_store = graph_store
        self._vector_store = vector_store
        self._cache: TTLCache[object] = cache or TTLCache(ttl_seconds=STATS_CACHE_TTL_SECONDS)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def overview(self, *, workspace_id: uuid.UUID) -> StatsOverview:
        """Aggregate the single-call dashboard summary."""

        async def _build() -> StatsOverview:
            return await self._build_overview(workspace_id=workspace_id)

        result = await self._cache.get_or_compute(
            workspace_id=workspace_id,
            method=_METHOD_OVERVIEW,
            factory=_build,
        )
        return _as_overview(result)

    async def sources(self, *, workspace_id: uuid.UUID) -> SourcesStatsResponse:
        """Per-source table for the dashboard."""

        async def _build() -> SourcesStatsResponse:
            return await self._build_sources(workspace_id=workspace_id)

        result = await self._cache.get_or_compute(
            workspace_id=workspace_id,
            method=_METHOD_SOURCES,
            factory=_build,
        )
        return _as_sources(result)

    async def entities_by_kind(self, *, workspace_id: uuid.UUID) -> EntitiesByKindResponse:
        """Entity-kind histogram."""

        async def _build() -> EntitiesByKindResponse:
            histo = await self._graph_store.count_entities_by_kind(workspace_id=workspace_id)
            entries = [KindHistogramEntry(kind=k, count=v) for k, v in sorted(histo.items())]
            return EntitiesByKindResponse(entries=entries, total=sum(histo.values()))

        result = await self._cache.get_or_compute(
            workspace_id=workspace_id,
            method=_METHOD_ENTITIES_BY_KIND,
            factory=_build,
        )
        return _as_entities_kind(result)

    async def edges_by_type(self, *, workspace_id: uuid.UUID) -> EdgesByTypeResponse:
        """Edge-type histogram."""

        async def _build() -> EdgesByTypeResponse:
            histo = await self._graph_store.count_edges_by_type(workspace_id=workspace_id)
            entries = [
                EdgeTypeHistogramEntry(edge_type=k, count=v) for k, v in sorted(histo.items())
            ]
            return EdgesByTypeResponse(entries=entries, total=sum(histo.values()))

        result = await self._cache.get_or_compute(
            workspace_id=workspace_id,
            method=_METHOD_EDGES_BY_TYPE,
            factory=_build,
        )
        return _as_edges_type(result)

    # ------------------------------------------------------------------
    # Builders — actually hit the stores
    # ------------------------------------------------------------------

    async def _build_overview(self, *, workspace_id: uuid.UUID) -> StatsOverview:
        """Build the overview by fanning out to all three stores in parallel."""
        pg_task = asyncio.create_task(self._postgres_overview(workspace_id))
        chunks_task = asyncio.create_task(
            self._vector_store.count_chunks(workspace_id=workspace_id)
        )
        entities_task = asyncio.create_task(
            self._graph_store.count_entities(workspace_id=workspace_id)
        )
        edges_task = asyncio.create_task(
            self._graph_store.count_edges_by_type(workspace_id=workspace_id)
        )

        pg = await pg_task
        chunks = await chunks_task
        entities = await entities_task
        edges_by_type = await edges_task

        return StatsOverview(
            sources=pg.sources,
            active_sources=pg.active_sources,
            documents=pg.documents,
            active_documents=pg.active_documents,
            tombstoned_documents=pg.tombstoned_documents,
            chunks=chunks,
            entities=entities,
            edges_by_type=edges_by_type,
            total_indexed_bytes=pg.total_indexed_bytes,
            documents_added_24h=pg.documents_added_24h,
            documents_updated_24h=pg.documents_updated_24h,
            documents_tombstoned_24h=pg.documents_tombstoned_24h,
        )

    async def _build_sources(self, *, workspace_id: uuid.UUID) -> SourcesStatsResponse:
        """Build the per-source table.

        Strategy: read all sources + per-source document counts from
        Postgres (one query each), then ask Neo4j and Qdrant for their
        per-source histograms (one query each).  Total: 4 queries
        regardless of N sources — no N+1.
        """
        now = datetime.now(tz=UTC)
        async with self._session_factory() as session:
            sources = await _fetch_sources(session, workspace_id)
            docs_per_source = await _fetch_documents_per_source(session, workspace_id)

        chunks_per_source_task = asyncio.create_task(
            self._vector_store.count_chunks_by_source(workspace_id=workspace_id)
        )
        entities_per_source_task = asyncio.create_task(
            self._graph_store.count_entities_by_source(workspace_id=workspace_id)
        )

        chunks_per_source = await chunks_per_source_task
        entities_per_source = await entities_per_source_task

        rows: list[SourceStatsRow] = []
        for src in sources:
            sid = str(src.id)
            rows.append(
                _build_source_row(
                    src=src,
                    documents=docs_per_source.get(sid, 0),
                    chunks=chunks_per_source.get(sid, 0),
                    entities=entities_per_source.get(sid, 0),
                    now=now,
                )
            )
        return SourcesStatsResponse(sources=rows, total=len(rows))

    # ------------------------------------------------------------------
    # Postgres rollup
    # ------------------------------------------------------------------

    async def _postgres_overview(self, workspace_id: uuid.UUID) -> _PostgresOverview:
        """Single round trip to Postgres for the overview rollup.

        We issue a small set of focused aggregate queries.  Each query
        is workspace-filtered via :func:`workspace_filter`, so the
        invariant is enforced once at the SQL layer rather than scattered
        across handler code.
        """
        cutoff = datetime.now(tz=UTC) - _DELTA_WINDOW

        async with self._session_factory() as session:
            sources_stmt = workspace_filter(select(func.count()).select_from(Source), workspace_id)
            active_sources_stmt = workspace_filter(
                select(func.count())
                .select_from(Source)
                .where(Source.status == SourceStatus.active),
                workspace_id,
            )
            documents_stmt = workspace_filter(
                select(func.count()).select_from(Document).join(Source), workspace_id
            )
            active_documents_stmt = workspace_filter(
                select(func.count())
                .select_from(Document)
                .join(Source)
                .where(Document.tombstoned_at.is_(None)),
                workspace_id,
            )
            tombstoned_documents_stmt = workspace_filter(
                select(func.count())
                .select_from(Document)
                .join(Source)
                .where(Document.tombstoned_at.is_not(None)),
                workspace_id,
            )
            documents_added_stmt = workspace_filter(
                select(func.count())
                .select_from(Document)
                .join(Source)
                .where(
                    Document.indexed_at >= cutoff,
                    Document.doc_version == 1,
                ),
                workspace_id,
            )
            documents_updated_stmt = workspace_filter(
                select(func.count())
                .select_from(Document)
                .join(Source)
                .where(
                    Document.indexed_at >= cutoff,
                    Document.doc_version > 1,
                ),
                workspace_id,
            )
            documents_tombstoned_stmt = workspace_filter(
                select(func.count())
                .select_from(Document)
                .join(Source)
                .where(Document.tombstoned_at >= cutoff),
                workspace_id,
            )
            # Total indexed bytes proxy: sum of LENGTH(Chunk.text) across
            # active documents.  Document.size_bytes is not stored; this
            # measures what we actually have in the index, which is the
            # number operators care about for capacity planning.
            indexed_bytes_stmt = workspace_filter(
                select(func.coalesce(func.sum(func.length(Chunk.text)), 0))
                .select_from(Chunk)
                .join(Document, Chunk.document_id == Document.id)
                .join(Source, Document.source_id == Source.id)
                .where(Document.tombstoned_at.is_(None)),
                workspace_id,
            )

            sources = int((await session.execute(sources_stmt)).scalar_one())
            active_sources = int((await session.execute(active_sources_stmt)).scalar_one())
            documents = int((await session.execute(documents_stmt)).scalar_one())
            active_documents = int((await session.execute(active_documents_stmt)).scalar_one())
            tombstoned_documents = int(
                (await session.execute(tombstoned_documents_stmt)).scalar_one()
            )
            documents_added_24h = int((await session.execute(documents_added_stmt)).scalar_one())
            documents_updated_24h = int(
                (await session.execute(documents_updated_stmt)).scalar_one()
            )
            documents_tombstoned_24h = int(
                (await session.execute(documents_tombstoned_stmt)).scalar_one()
            )
            total_indexed_bytes = int((await session.execute(indexed_bytes_stmt)).scalar_one())

        return _PostgresOverview(
            sources=sources,
            active_sources=active_sources,
            documents=documents,
            active_documents=active_documents,
            tombstoned_documents=tombstoned_documents,
            documents_added_24h=documents_added_24h,
            documents_updated_24h=documents_updated_24h,
            documents_tombstoned_24h=documents_tombstoned_24h,
            total_indexed_bytes=total_indexed_bytes,
        )


# ---------------------------------------------------------------------------
# Internal aggregate dataclass (Postgres-side rollup)
# ---------------------------------------------------------------------------


class _PostgresOverview:
    """Container for the Postgres-side overview values.

    Plain class (not dataclass) to avoid an extra import; the field set
    is private to this module and never crosses the package boundary.
    """

    __slots__ = (
        "active_documents",
        "active_sources",
        "documents",
        "documents_added_24h",
        "documents_tombstoned_24h",
        "documents_updated_24h",
        "sources",
        "tombstoned_documents",
        "total_indexed_bytes",
    )

    def __init__(
        self,
        *,
        sources: int,
        active_sources: int,
        documents: int,
        active_documents: int,
        tombstoned_documents: int,
        documents_added_24h: int,
        documents_updated_24h: int,
        documents_tombstoned_24h: int,
        total_indexed_bytes: int,
    ) -> None:
        self.sources = sources
        self.active_sources = active_sources
        self.documents = documents
        self.active_documents = active_documents
        self.tombstoned_documents = tombstoned_documents
        self.documents_added_24h = documents_added_24h
        self.documents_updated_24h = documents_updated_24h
        self.documents_tombstoned_24h = documents_tombstoned_24h
        self.total_indexed_bytes = total_indexed_bytes


# ---------------------------------------------------------------------------
# SQL helpers (kept module-level so they are easy to unit-test)
# ---------------------------------------------------------------------------


async def _fetch_sources(session: AsyncSession, workspace_id: uuid.UUID) -> list[Source]:
    """Return every source visible to ``workspace_id``."""
    stmt = workspace_filter(select(Source), workspace_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _fetch_documents_per_source(
    session: AsyncSession, workspace_id: uuid.UUID
) -> dict[str, int]:
    """Return ``{source_id: active_document_count}`` map for the workspace."""
    stmt = workspace_filter(
        select(
            Document.source_id,
            func.count(Document.id),
        )
        .join(Source, Document.source_id == Source.id)
        .where(Document.tombstoned_at.is_(None))
        .group_by(Document.source_id),
        workspace_id,
    )
    result = await session.execute(stmt)
    return {str(row[0]): int(row[1]) for row in result.all()}


# ---------------------------------------------------------------------------
# Pure helpers (per-row builder + cache-typing crutches)
# ---------------------------------------------------------------------------


def _build_source_row(
    *,
    src: Source,
    documents: int,
    chunks: int,
    entities: int,
    now: datetime,
) -> SourceStatsRow:
    """Assemble one :class:`SourceStatsRow` from the per-source counts."""
    age, is_stale = _staleness(src=src, now=now)
    return SourceStatsRow(
        id=src.id,
        name=src.name,
        type=str(src.type),
        status=str(src.status),
        documents=documents,
        chunks=chunks,
        entities=entities,
        last_sync_at=src.last_sync_at.isoformat() if src.last_sync_at else None,
        freshness_sla_seconds=src.freshness_sla_seconds,
        age_seconds=age,
        is_stale=is_stale,
    )


# Sentinel for "never synced" age — JSON-safe (matches freshness.py).
_INFINITY_JSON_SAFE: Final[float] = 1e15


def _staleness(*, src: Source, now: datetime) -> tuple[float, bool]:
    """Compute (age_seconds, is_stale) for a source row.

    Mirrors ``omniscience_core.freshness._compute_report`` semantics so
    the dashboard table and the freshness endpoint agree on staleness.
    """
    sla = src.freshness_sla_seconds
    last_sync = src.last_sync_at
    if last_sync is None:
        age = _INFINITY_JSON_SAFE
        return age, sla is not None
    sync_aware = last_sync if last_sync.tzinfo else last_sync.replace(tzinfo=UTC)
    age = (now - sync_aware).total_seconds()
    if sla is None:
        return age, False
    return age, age > sla


# ---------------------------------------------------------------------------
# Cache-typing crutches
#
# ``TTLCache`` is generic in ``T``, but we store mixed pydantic models in
# one cache instance.  These thin casts validate the type at retrieval
# without forcing the cache to be invariant in a union type.
# ---------------------------------------------------------------------------


def _as_overview(value: object) -> StatsOverview:
    if not isinstance(value, StatsOverview):
        raise TypeError("stats cache returned wrong type for overview")
    return value


def _as_sources(value: object) -> SourcesStatsResponse:
    if not isinstance(value, SourcesStatsResponse):
        raise TypeError("stats cache returned wrong type for sources")
    return value


def _as_entities_kind(value: object) -> EntitiesByKindResponse:
    if not isinstance(value, EntitiesByKindResponse):
        raise TypeError("stats cache returned wrong type for entities_by_kind")
    return value


def _as_edges_type(value: object) -> EdgesByTypeResponse:
    if not isinstance(value, EdgesByTypeResponse):
        raise TypeError("stats cache returned wrong type for edges_by_type")
    return value


__all__ = ["StatsService"]
