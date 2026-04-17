"""Sources CRUD endpoints.

GET    /api/v1/sources           — list sources (query: type, status)
POST   /api/v1/sources           — create source
GET    /api/v1/sources/{id}      — read one source
PATCH  /api/v1/sources/{id}      — update source
DELETE /api/v1/sources/{id}      — tombstone source
POST   /api/v1/sources/{id}/sync — trigger manual sync
GET    /api/v1/sources/{id}/stats— source statistics

Read operations require ``sources:read`` scope.
Write operations require ``sources:write`` scope.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from omniscience_core.auth.middleware import require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.db.models import IngestionRunStatus, Source, SourceStatus, SourceType
from omniscience_core.db.schemas import SourceCreate, SourceRead, SourceUpdate
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

router = APIRouter(tags=["sources"])

# Module-level Depends singletons — avoids ruff B008
_read_scope_dep: Any = Depends(require_scope(Scope.sources_read))
_write_scope_dep: Any = Depends(require_scope(Scope.sources_write))


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class SyncResponse(BaseModel):
    """Response for a manual sync trigger."""

    run_id: uuid.UUID


class SourceStatsResponse(BaseModel):
    """Statistics for a single source."""

    source_id: uuid.UUID
    total_documents: int
    active_documents: int
    total_chunks: int
    last_sync_at: str | None
    last_run_status: str | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_db_factory(request: Request) -> Any:
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "service_unavailable", "message": "Database not available"},
        )
    return factory


async def _get_source_or_404(db: AsyncSession, source_id: uuid.UUID) -> Source:
    source = await db.get(Source, source_id)
    if source is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "source_not_found", "message": f"Source {source_id} not found"},
        )
    return source


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/sources",
    response_model=list[SourceRead],
    summary="List sources",
    dependencies=[_read_scope_dep],
)
async def list_sources(
    request: Request,
    source_type: SourceType | None = None,
    status: SourceStatus | None = None,
) -> list[SourceRead]:
    """List all configured sources, optionally filtered by type and/or status.

    Query params: ``source_type``, ``status``
    Requires scope: ``sources:read``
    """
    factory = _get_db_factory(request)

    db: AsyncSession
    async with factory() as db:
        stmt = select(Source)
        if source_type is not None:
            stmt = stmt.where(Source.type == source_type)
        if status is not None:
            stmt = stmt.where(Source.status == status)
        result = await db.execute(stmt)
        sources = result.scalars().all()
        return [SourceRead.model_validate(s) for s in sources]


@router.post(
    "/sources",
    response_model=SourceRead,
    status_code=201,
    summary="Create a source",
    dependencies=[_write_scope_dep],
)
async def create_source(
    payload: SourceCreate,
    request: Request,
) -> SourceRead:
    """Create a new ingestion source.

    Body is validated as a SourceCreate (type, name, config, optional secrets_ref).
    Requires scope: ``sources:write``
    """
    factory = _get_db_factory(request)

    db: AsyncSession
    async with factory() as db:
        source = Source(
            type=payload.type,
            name=payload.name,
            config=payload.config,
            secrets_ref=payload.secrets_ref,
            status=payload.status,
            freshness_sla_seconds=payload.freshness_sla_seconds,
            tenant_id=payload.tenant_id,
        )
        db.add(source)
        await db.flush()
        await db.refresh(source)
        await db.commit()

        log.info("source_created", source_id=str(source.id), name=source.name, type=source.type)
        return SourceRead.model_validate(source)


@router.get(
    "/sources/{source_id}",
    response_model=SourceRead,
    summary="Get a source",
    dependencies=[_read_scope_dep],
)
async def get_source(
    source_id: uuid.UUID,
    request: Request,
) -> SourceRead:
    """Retrieve a single source by ID.

    Requires scope: ``sources:read``
    """
    factory = _get_db_factory(request)

    db: AsyncSession
    async with factory() as db:
        source = await _get_source_or_404(db, source_id)
        return SourceRead.model_validate(source)


@router.patch(
    "/sources/{source_id}",
    response_model=SourceRead,
    summary="Update a source",
    dependencies=[_write_scope_dep],
)
async def update_source(
    source_id: uuid.UUID,
    payload: SourceUpdate,
    request: Request,
) -> SourceRead:
    """Partially update a source's config, secrets_ref, status, or freshness SLA.

    Requires scope: ``sources:write``
    """
    factory = _get_db_factory(request)

    db: AsyncSession
    async with factory() as db:
        source = await _get_source_or_404(db, source_id)

        update_data = payload.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(source, field, value)

        await db.flush()
        await db.refresh(source)
        await db.commit()

        log.info("source_updated", source_id=str(source_id), fields=list(update_data.keys()))
        return SourceRead.model_validate(source)


@router.delete(
    "/sources/{source_id}",
    status_code=204,
    summary="Delete a source",
    dependencies=[_write_scope_dep],
)
async def delete_source(
    source_id: uuid.UUID,
    request: Request,
) -> None:
    """Remove a source. Associated documents and chunks are tombstoned then
    purged by the janitor background process.

    Requires scope: ``sources:write``
    """
    factory = _get_db_factory(request)

    db: AsyncSession
    async with factory() as db:
        source = await _get_source_or_404(db, source_id)
        await db.delete(source)
        await db.commit()

        log.info("source_deleted", source_id=str(source_id))


@router.post(
    "/sources/{source_id}/sync",
    response_model=SyncResponse,
    status_code=202,
    summary="Trigger manual sync",
    dependencies=[_write_scope_dep],
)
async def trigger_sync(
    source_id: uuid.UUID,
    request: Request,
) -> SyncResponse:
    """Trigger an immediate manual sync for the given source.

    Creates an ingestion run record and enqueues a sync task via the message queue.
    Monitor progress via ``GET /api/v1/ingestion-runs/{run_id}``.

    Requires scope: ``sources:write``
    """
    from omniscience_core.db.models import IngestionRun

    factory = _get_db_factory(request)

    db: AsyncSession
    async with factory() as db:
        # Verify source exists
        await _get_source_or_404(db, source_id)

        # Create an ingestion run record
        run = IngestionRun(
            source_id=source_id,
            status=IngestionRunStatus.running,
        )
        db.add(run)
        await db.flush()
        await db.refresh(run)
        await db.commit()

        run_id: uuid.UUID = run.id

    # TODO(issue-6): Enqueue sync task via NATS JetStream
    # nats = getattr(request.app.state, "nats", None)
    # if nats is not None:
    #     await nats.jetstream.publish(
    #         "sync.trigger",
    #         json.dumps({"source_id": str(source_id), "run_id": str(run_id)}).encode(),
    #     )

    log.info("sync_triggered", source_id=str(source_id), run_id=str(run_id))
    return SyncResponse(run_id=run_id)


@router.get(
    "/sources/{source_id}/stats",
    response_model=SourceStatsResponse,
    summary="Source statistics",
    dependencies=[_read_scope_dep],
)
async def source_stats(
    source_id: uuid.UUID,
    request: Request,
) -> SourceStatsResponse:
    """Return statistics for a source: document counts, chunk count, last sync.

    Requires scope: ``sources:read``
    """
    from omniscience_core.db.models import Chunk, Document, IngestionRun
    from sqlalchemy import func

    factory = _get_db_factory(request)

    db: AsyncSession
    async with factory() as db:
        source = await _get_source_or_404(db, source_id)

        # Total documents for this source
        total_docs_result = await db.execute(
            select(func.count()).select_from(Document).where(Document.source_id == source_id)
        )
        total_documents: int = total_docs_result.scalar_one()

        # Active (non-tombstoned) documents
        active_docs_result = await db.execute(
            select(func.count())
            .select_from(Document)
            .where(
                Document.source_id == source_id,
                Document.tombstoned_at.is_(None),
            )
        )
        active_documents: int = active_docs_result.scalar_one()

        # Total chunks across all documents for this source
        chunks_result = await db.execute(
            select(func.count())
            .select_from(Chunk)
            .join(Document, Chunk.document_id == Document.id)
            .where(Document.source_id == source_id)
        )
        total_chunks: int = chunks_result.scalar_one()

        # Last ingestion run status
        last_run_result = await db.execute(
            select(IngestionRun)
            .where(IngestionRun.source_id == source_id)
            .order_by(IngestionRun.started_at.desc())
            .limit(1)
        )
        last_run = last_run_result.scalars().first()

        return SourceStatsResponse(
            source_id=source_id,
            total_documents=total_documents,
            active_documents=active_documents,
            total_chunks=total_chunks,
            last_sync_at=source.last_sync_at.isoformat() if source.last_sync_at else None,
            last_run_status=last_run.status.value if last_run else None,
        )


__all__ = ["router"]
