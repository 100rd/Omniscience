"""MCP tool implementations for Omniscience.

Each function is a standalone async callable that accepts the FastAPI
app (for access to db_session_factory and retrieval_service) and the
validated tool arguments.  The server.py module wires them to the
FastMCP instance.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import FastAPI
from omniscience_core.db.models import Chunk, Document, IngestionRun, Source
from omniscience_retrieval.models import SearchRequest
from omniscience_retrieval.search import RetrievalService
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


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
) -> dict[str, Any]:
    """Execute hybrid retrieval and return hits with citations."""
    service: RetrievalService | None = getattr(app.state, "retrieval_service", None)
    if service is None:
        raise RuntimeError("retrieval_service not available on app.state")

    request = SearchRequest(
        query=query,
        top_k=top_k,
        sources=sources,
        types=types,
        max_age_seconds=max_age_seconds,
        filters=filters,
        include_tombstoned=include_tombstoned,
        retrieval_strategy=retrieval_strategy,  # type: ignore[arg-type]
    )
    result = await service.search(request)
    return result.model_dump(mode="json")


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
        is_stale = False
        if sla is not None and last_sync is not None:
            elapsed = (now - last_sync.replace(tzinfo=UTC)).total_seconds()
            is_stale = elapsed > sla
        elif sla is not None and last_sync is None:
            is_stale = True

        source_list.append(
            {
                "id": str(src.id),
                "name": src.name,
                "type": str(src.type),
                "status": str(src.status),
                "last_sync_at": last_sync.isoformat() if last_sync else None,
                "freshness_sla_seconds": sla,
                "is_stale": is_stale,
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

    return {
        "id": str(src.id),
        "name": src.name,
        "type": str(src.type),
        "status": str(src.status),
        "last_sync_at": src.last_sync_at.isoformat() if src.last_sync_at else None,
        "last_error": src.last_error,
        "last_error_at": src.last_error_at.isoformat() if src.last_error_at else None,
        "freshness_sla_seconds": src.freshness_sla_seconds,
        "indexed_document_count": doc_count,
        "indexed_chunk_count": chunk_count,
        "last_ingestion_run": last_run_dict,
    }
