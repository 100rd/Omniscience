"""MCP tool implementations for Omniscience.

Each function is a standalone async callable that accepts the FastAPI
app (for access to db_session_factory and retrieval_service) and the
validated tool arguments.  The server.py module wires them to the
FastMCP instance.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import FastAPI
from omniscience_core.db.models import Chunk, Document, IngestionRun, Source
from omniscience_core.storage import EntityNodeView, GraphResultView, GraphStore
from omniscience_retrieval.graph_rag import GraphRAGComposer
from omniscience_retrieval.models import SearchRequest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from omniscience_server.as_of import (
    DEGRADED_PRE_HISTORY,
    enforce_utc,
    record_request_duration,
    resolve_effective_as_of,
)
from omniscience_server.incidents import mcp_resolve_incident as _mcp_resolve_incident

# Re-exported so ``omniscience_server.mcp.tools.mcp_resolve_incident`` is
# the canonical import path for the tool, matching the layout of the
# other MCP tools registered in ``omniscience_server.mcp.server``.
mcp_resolve_incident = _mcp_resolve_incident

log = structlog.get_logger(__name__)

# JSON-safe sentinel used instead of +inf for sources that have never synced.
_INFINITY_JSON_SAFE: float = 1e15


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


async def mcp_search(
    app: FastAPI,
    query: str,
    top_k: int = 10,
    sources: list[str] | None = None,
    types: list[str] | None = None,
    max_age_seconds: int | None = None,
    filters: dict[str, Any] | None = None,
    include_tombstoned: bool = False,
    retrieval_strategy: str = "hybrid",
    workspace_id: uuid.UUID | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Execute retrieval and return hits with citations.

    As of v0.2 (#105 cutover) the canonical path is the
    ``GraphRAGComposer`` (Neo4j + Qdrant).  Callers that cannot supply
    a ``workspace_id`` receive a ``RuntimeError`` — workspace scoping
    is a non-negotiable invariant (#117).

    The optional ``as_of`` parameter (issue #133) anchors the
    underlying graph traversal to ADR-0008 §5's bitemporal predicate;
    the vector step is unfiltered on time and scoped only by the
    chunk/source ids the graph traversal returned.  Qdrant's own
    ``as_of`` filter lands in #134.

    The legacy ``RetrievalService`` (pgvector + BM25) was removed in
    v0.2; ``app.state.retrieval_service`` is preserved as an optional
    escape hatch for operators that wire a custom federation-peer
    shim, but it is never populated by default.
    """
    normalised_as_of = enforce_utc(as_of)
    composer: GraphRAGComposer | None = getattr(app.state, "graph_rag_composer", None)
    request = SearchRequest(
        query=query,
        top_k=top_k,
        sources=sources,
        types=types,
        max_age_seconds=max_age_seconds,
        filters=filters,
        include_tombstoned=include_tombstoned,
        retrieval_strategy=retrieval_strategy,  # type: ignore[arg-type]
        as_of=normalised_as_of,
    )
    with record_request_duration(surface="mcp", tool="search", as_of=normalised_as_of) as patcher:
        if composer is not None and workspace_id is not None:
            result = await composer.search(
                request,
                workspace_id=workspace_id,
                as_of=normalised_as_of,
            )
            payload: dict[str, Any] = result.model_dump(mode="json")
            if (
                normalised_as_of is not None
                and isinstance(payload.get("meta"), dict)
                and payload["meta"].get("degraded_response") == DEGRADED_PRE_HISTORY
            ):
                patcher.mark_pre_history()
            return payload

        service: Any | None = getattr(app.state, "retrieval_service", None)
        if service is None:
            raise RuntimeError("retrieval_service not available on app.state")
        legacy_result = await service.search(request)
        legacy_payload: dict[str, Any] = legacy_result.model_dump(mode="json")
        # Legacy path doesn't honour as_of; still echo back the resolved
        # effective_as_of so contract tests are satisfied.
        legacy_payload.setdefault(
            "effective_as_of",
            resolve_effective_as_of(normalised_as_of).isoformat(),
        )
        return legacy_payload


# ---------------------------------------------------------------------------
# get_document
# ---------------------------------------------------------------------------


async def mcp_get_document(app: FastAPI, document_id: str) -> dict[str, Any]:
    """Fetch a full document and all its chunks by document id."""
    factory = getattr(app.state, "db_session_factory", None)
    if factory is None:
        raise RuntimeError("db_session_factory not available on app.state")

    doc_uuid = uuid.UUID(document_id)

    session: AsyncSession
    async with factory() as session:
        doc_row = await session.get(Document, doc_uuid)
        if doc_row is None:
            raise ValueError(f"document_not_found:{document_id}")

        source_row = await session.get(Source, doc_row.source_id)
        chunk_result = await session.execute(
            select(Chunk).where(Chunk.document_id == doc_uuid).order_by(Chunk.ord)
        )
        chunks = chunk_result.scalars().all()

    doc_dict: dict[str, Any] = {
        "id": str(doc_row.id),
        "source_id": str(doc_row.source_id),
        "external_id": doc_row.external_id,
        "uri": doc_row.uri,
        "title": doc_row.title,
        "doc_version": doc_row.doc_version,
        "indexed_at": doc_row.indexed_at.isoformat(),
        "tombstoned_at": (doc_row.tombstoned_at.isoformat() if doc_row.tombstoned_at else None),
        "metadata": doc_row.doc_metadata,
        "source": {
            "id": str(source_row.id) if source_row else None,
            "name": source_row.name if source_row else None,
            "type": str(source_row.type) if source_row else None,
        },
    }
    chunk_list: list[dict[str, Any]] = [
        {
            "id": str(c.id),
            "ord": c.ord,
            "text": c.text,
            "symbol": c.symbol,
            "embedding_model": c.embedding_model,
            "embedding_provider": c.embedding_provider,
            "parser_version": c.parser_version,
            "chunker_strategy": c.chunker_strategy,
            "metadata": c.chunk_metadata,
        }
        for c in chunks
    ]
    return {"document": doc_dict, "chunks": chunk_list}


# ---------------------------------------------------------------------------
# list_sources
# ---------------------------------------------------------------------------


async def mcp_list_sources(app: FastAPI) -> dict[str, Any]:
    """List all configured sources with freshness information."""
    factory = getattr(app.state, "db_session_factory", None)
    if factory is None:
        raise RuntimeError("db_session_factory not available on app.state")

    now = datetime.now(tz=UTC)

    session: AsyncSession
    async with factory() as session:
        result = await session.execute(select(Source))
        sources = result.scalars().all()

        # Count indexed documents per source
        count_result = await session.execute(
            select(Document.source_id, func.count(Document.id).label("cnt"))
            .where(Document.tombstoned_at.is_(None))
            .group_by(Document.source_id)
        )
        counts: dict[uuid.UUID, int] = {row.source_id: row.cnt for row in count_result}

    source_list: list[dict[str, Any]] = []
    for src in sources:
        sla = src.freshness_sla_seconds
        last_sync = src.last_sync_at

        # Inline staleness calculation (avoids extra DB round-trip per source).
        is_stale = False
        age_seconds: float = _INFINITY_JSON_SAFE
        staleness_margin_seconds: float | None = None

        if last_sync is not None:
            sync_aware = last_sync if last_sync.tzinfo else last_sync.replace(tzinfo=UTC)
            age_seconds = (now - sync_aware).total_seconds()

        if sla is not None:
            if last_sync is None:
                is_stale = True
                staleness_margin_seconds = _INFINITY_JSON_SAFE
            else:
                margin = age_seconds - sla
                is_stale = age_seconds > sla
                staleness_margin_seconds = (
                    margin if not math.isinf(margin) else _INFINITY_JSON_SAFE
                )

        source_list.append(
            {
                "id": str(src.id),
                "name": src.name,
                "type": str(src.type),
                "status": str(src.status),
                "last_sync_at": last_sync.isoformat() if last_sync else None,
                "freshness_sla_seconds": sla,
                "is_stale": is_stale,
                "age_seconds": age_seconds,
                "staleness_margin_seconds": staleness_margin_seconds,
                "indexed_document_count": counts.get(src.id, 0),
            }
        )
    return {"sources": source_list}


# ---------------------------------------------------------------------------
# source_stats
# ---------------------------------------------------------------------------


async def mcp_source_stats(app: FastAPI, source_id: str) -> dict[str, Any]:
    """Return detailed statistics for a single source."""
    factory = getattr(app.state, "db_session_factory", None)
    if factory is None:
        raise RuntimeError("db_session_factory not available on app.state")

    src_uuid = uuid.UUID(source_id)

    session: AsyncSession
    async with factory() as session:
        src = await session.get(Source, src_uuid)
        if src is None:
            raise ValueError(f"source_not_found:{source_id}")

        doc_count_result = await session.execute(
            select(func.count(Document.id)).where(
                Document.source_id == src_uuid,
                Document.tombstoned_at.is_(None),
            )
        )
        doc_count: int = doc_count_result.scalar_one()

        chunk_count_result = await session.execute(
            select(func.count(Chunk.id))
            .join(Document, Chunk.document_id == Document.id)
            .where(
                Document.source_id == src_uuid,
                Document.tombstoned_at.is_(None),
            )
        )
        chunk_count: int = chunk_count_result.scalar_one()

        last_run_result = await session.execute(
            select(IngestionRun)
            .where(IngestionRun.source_id == src_uuid)
            .order_by(IngestionRun.started_at.desc())
            .limit(1)
        )
        last_run = last_run_result.scalar_one_or_none()

    last_run_dict: dict[str, Any] | None = None
    if last_run is not None:
        last_run_dict = {
            "id": str(last_run.id),
            "started_at": last_run.started_at.isoformat(),
            "finished_at": (last_run.finished_at.isoformat() if last_run.finished_at else None),
            "status": str(last_run.status),
            "docs_new": last_run.docs_new,
            "docs_updated": last_run.docs_updated,
            "docs_removed": last_run.docs_removed,
            "errors": last_run.run_errors,
        }

    # Compute freshness fields inline.
    now = datetime.now(tz=UTC)
    sla = src.freshness_sla_seconds
    last_sync = src.last_sync_at
    is_stale = False
    age_seconds: float = _INFINITY_JSON_SAFE
    staleness_margin_seconds: float | None = None

    if last_sync is not None:
        sync_aware = last_sync if last_sync.tzinfo else last_sync.replace(tzinfo=UTC)
        age_seconds = (now - sync_aware).total_seconds()

    if sla is not None:
        if last_sync is None:
            is_stale = True
            staleness_margin_seconds = _INFINITY_JSON_SAFE
        else:
            margin = age_seconds - sla
            is_stale = age_seconds > sla
            staleness_margin_seconds = margin if not math.isinf(margin) else _INFINITY_JSON_SAFE

    return {
        "id": str(src.id),
        "name": src.name,
        "type": str(src.type),
        "status": str(src.status),
        "last_sync_at": src.last_sync_at.isoformat() if src.last_sync_at else None,
        "last_error": src.last_error,
        "last_error_at": src.last_error_at.isoformat() if src.last_error_at else None,
        "freshness_sla_seconds": sla,
        "is_stale": is_stale,
        "age_seconds": age_seconds,
        "staleness_margin_seconds": staleness_margin_seconds,
        "indexed_document_count": doc_count,
        "indexed_chunk_count": chunk_count,
        "last_ingestion_run": last_run_dict,
    }


# ---------------------------------------------------------------------------
# get_related_entities
# ---------------------------------------------------------------------------


async def mcp_get_related_entities(
    app: FastAPI,
    entity_name: str,
    workspace_id: uuid.UUID,
    max_depth: int = 1,
    edge_types: list[str] | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Traverse the entity graph starting from a named entity.

    The caller MUST supply ``workspace_id`` — the traversal is confined to
    entities and edges owned by that workspace.  See issue #117 for the
    background: graph retrieval used to bypass workspace scoping and leak
    cross-tenant entities.

    The optional ``as_of`` (issue #133) anchors the traversal to
    ADR-0008 §5's bitemporal predicate.  ``None`` (default) returns the
    still-current graph; an explicit value returns the graph as it was
    at T (ADR-0008 §5).

    Returns the seed entity, related entities (up to max_depth hops),
    and the edges connecting them.  Each entity includes its associated
    chunk text for context.  ``effective_as_of`` echoes the resolved
    bitemporal anchor; ``meta.degraded_response`` is set when the
    seed entity is unknown at ``as_of``.
    """
    normalised_as_of = enforce_utc(as_of)
    graph_store: GraphStore | None = getattr(app.state, "graph_store", None)
    if graph_store is None:
        raise RuntimeError("graph_store not available on app.state")

    with record_request_duration(
        surface="mcp", tool="get_related_entities", as_of=normalised_as_of
    ) as patcher:
        try:
            result = await graph_store.find_related(
                entity_name=entity_name,
                workspace_id=workspace_id,
                as_of=normalised_as_of,
                max_depth=max_depth,
                edge_types=edge_types,
            )
        except ValueError as exc:
            if normalised_as_of is not None and str(exc).startswith("entity_not_found:"):
                # Pre-history / unknown-at-T: degraded response, not error.
                patcher.mark_pre_history()
                return _empty_graph_payload(
                    entity_name=entity_name,
                    as_of=normalised_as_of,
                    degraded=True,
                )
            raise
        payload = _graph_view_to_dict(result)
        payload["effective_as_of"] = resolve_effective_as_of(normalised_as_of).isoformat()
        return payload


async def mcp_get_entity(
    app: FastAPI,
    entity_name: str,
    workspace_id: uuid.UUID,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Resolve a single entity by name within the caller's workspace.

    Issue #133 §A — pass-through to ``GraphStore.get_entity`` carrying
    ``as_of`` when supplied.  Returns the still-current ``:Entity``
    mirror when ``as_of`` is ``None``, or the ``:EntityState`` row that
    was valid at T per ADR-0008 §5.

    Empty/unknown-at-T responses surface the
    ``"as_of_before_recorded_history"`` degraded-response hint to
    distinguish "no record" from "deleted/unauthorised".  Cross-workspace
    entities are indistinguishable from "not found" by design — see
    ``GraphStore.get_entity`` docstring.
    """
    normalised_as_of = enforce_utc(as_of)
    graph_store: GraphStore | None = getattr(app.state, "graph_store", None)
    if graph_store is None:
        raise RuntimeError("graph_store not available on app.state")

    with record_request_duration(
        surface="mcp", tool="get_entity", as_of=normalised_as_of
    ) as patcher:
        node: EntityNodeView | None = await graph_store.get_entity(
            entity_name=entity_name,
            workspace_id=workspace_id,
            as_of=normalised_as_of,
        )
        if node is None:
            if normalised_as_of is not None:
                patcher.mark_pre_history()
            return {
                "entity": None,
                "effective_as_of": resolve_effective_as_of(normalised_as_of).isoformat(),
                "meta": (
                    {"degraded_response": DEGRADED_PRE_HISTORY}
                    if normalised_as_of is not None
                    else None
                ),
            }
        return {
            "entity": _entity_view_to_dict(node),
            "effective_as_of": resolve_effective_as_of(normalised_as_of).isoformat(),
            "meta": None,
        }


async def mcp_list_entities(
    app: FastAPI,
    workspace_id: uuid.UUID,
    kind: str,
    cluster: str | None = None,
    name: str | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """List entities of ``kind`` within the caller's workspace.

    Pass-through to ``GraphStore.list_entities`` (committed in f8240b7),
    optionally filtered by the first-class indexed ``cluster`` property
    and/or an exact ``name``.  Answers deterministic platform-state
    questions for the change-validation gate ("which StorageClasses exist
    in cluster X", "does StorageClass 'gold' exist in cluster X") via an
    index seek on ``(workspace_id, kind, cluster)`` — never a metadata
    substring scan.

    Mirrors :func:`mcp_get_entity` for the response envelope: each entity
    uses the same per-entity shape as ``get_entity`` (``_entity_view_to_dict``),
    and the envelope carries ``effective_as_of`` (ADR-0008 §5 echo-back)
    plus ``meta``.  ``as_of`` returns the version valid at T; ``None``
    returns the still-current state.
    """
    normalised_as_of = enforce_utc(as_of)
    graph_store: GraphStore | None = getattr(app.state, "graph_store", None)
    if graph_store is None:
        raise RuntimeError("graph_store not available on app.state")

    with record_request_duration(
        surface="mcp", tool="list_entities", as_of=normalised_as_of
    ) as patcher:
        nodes: list[EntityNodeView] = await graph_store.list_entities(
            workspace_id=workspace_id,
            kind=kind,
            cluster=cluster,
            name=name,
            as_of=normalised_as_of,
        )
        if not nodes and normalised_as_of is not None:
            patcher.mark_pre_history()
        return {
            "entities": [_entity_view_to_dict(node) for node in nodes],
            "effective_as_of": resolve_effective_as_of(normalised_as_of).isoformat(),
            "meta": (
                {"degraded_response": DEGRADED_PRE_HISTORY}
                if not nodes and normalised_as_of is not None
                else None
            ),
        }


def _entity_view_to_dict(node: EntityNodeView) -> dict[str, Any]:
    """Serialise an :class:`EntityNodeView` to the MCP/REST wire format."""
    return {
        "name": node.name,
        "kind": node.kind,
        "source": node.source,
        "chunk_text": node.chunk_text,
        "valid_from": node.valid_from.isoformat() if node.valid_from else None,
        "valid_to": node.valid_to.isoformat() if node.valid_to else None,
        "recorded_at": node.recorded_at.isoformat() if node.recorded_at else None,
    }


def _empty_graph_payload(
    *,
    entity_name: str,
    as_of: datetime | None,
    degraded: bool,
) -> dict[str, Any]:
    """Build the empty-result envelope for a pre-history graph traversal."""
    return {
        "seed": {
            "name": entity_name,
            "kind": None,
            "source": None,
            "chunk_text": None,
        },
        "related": [],
        "edges": [],
        "stats": {
            "entities_found": 0,
            "edges_traversed": 0,
            "depth_reached": 0,
        },
        "effective_as_of": resolve_effective_as_of(as_of).isoformat(),
        "meta": ({"degraded_response": DEGRADED_PRE_HISTORY} if degraded else None),
    }


def _graph_view_to_dict(result: GraphResultView) -> dict[str, Any]:
    """Serialise a ``GraphResultView`` to the MCP wire format.

    Mirrors ``omniscience_server.rest.entities._result_to_dict`` so
    the MCP and REST surfaces return identical shapes.  Kept local
    to avoid a rest -> mcp import.
    """
    depth_reached = max((n.depth for n in result.related), default=0)
    return {
        "seed": {
            "name": result.seed.name,
            "kind": result.seed.kind,
            "source": result.seed.source,
            "chunk_text": result.seed.chunk_text,
        },
        "related": [
            {
                "name": n.name,
                "kind": n.kind,
                "source": n.source,
                "chunk_text": n.chunk_text,
                "depth": n.depth,
                "edge_type": n.edge_type,
            }
            for n in result.related
        ],
        "edges": [
            {
                "from": e.from_entity,
                "to": e.to_entity,
                "type": e.edge_type,
            }
            for e in result.edges
        ],
        "stats": {
            "entities_found": 1 + len(result.related),
            "edges_traversed": len(result.edges),
            "depth_reached": depth_reached,
        },
    }
