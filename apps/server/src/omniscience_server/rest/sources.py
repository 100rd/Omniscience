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

ACL invariant (cross-tenant isolation)
---------------------------------------
All read and write endpoints extract the caller's workspace_id from the
bearer token (via ``get_workspace_id``) and enforce ``Source.tenant_id ==
workspace_id``.  A legacy token with no workspace_id is rejected fail-closed
with 403 — it is never permitted to see all tenants' data.

For single-resource endpoints (GET/PATCH/DELETE /sources/{id} and
/sources/{id}/stats) a mismatched tenant returns 404 (not 403) to avoid
leaking resource existence to other tenants.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from omniscience_core.auth.middleware import require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.workspace import get_workspace_id
from omniscience_core.db.models import (
    ApiToken,
    IngestionRunStatus,
    Source,
    SourceStatus,
    SourceType,
)
from omniscience_core.db.schemas import SourceCreate, SourceRead, SourceUpdate
from omniscience_core.queue.producer import QueueProducer
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from omniscience_server.ingestion.events import DocumentChangeEvent

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


def _require_workspace(token: ApiToken) -> uuid.UUID:
    """Return workspace_id from token.

    If the token has no workspace (legacy token, workspace_id is None),
    get_workspace_id raises PermissionError("forbidden:workspace_required").
    The global PermissionError handler in rest/errors.py converts that to
    HTTP 403 — no per-endpoint guard needed here.
    """
    return get_workspace_id(token)


async def _get_source_or_404(
    db: AsyncSession,
    source_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID,
) -> Source:
    """Fetch source by id; return 404 when absent OR when tenant mismatch.

    Using 404 for tenant mismatch deliberately avoids leaking cross-tenant
    resource existence (returning 403 would confirm the resource exists).
    """
    source = await db.get(Source, source_id)
    if source is None or source.tenant_id != workspace_id:
        raise HTTPException(
            status_code=404,
            detail={"code": "source_not_found", "message": f"Source {source_id} not found"},
        )
    return source


async def _publish_sync_event(
    request: Request,
    source_id: uuid.UUID,
    source_type: str,
    run_id: uuid.UUID,
) -> bool:
    """Publish a DocumentChangeEvent to NATS to trigger a full sync.

    Returns True when the event was enqueued, False when NATS is unavailable.
    Publish failures are logged but do not cause the HTTP request to fail —
    the IngestionRun record is already committed and can be retried.
    """
    nats_conn = getattr(request.app.state, "nats", None)
    if nats_conn is None:
        log.debug("sync_nats_unavailable", source_id=str(source_id))
        return False

    js = getattr(nats_conn, "jetstream", None)
    if js is None:
        log.debug("sync_nats_no_jetstream", source_id=str(source_id))
        return False

    event = DocumentChangeEvent(
        source_id=source_id,
        source_type=source_type,
        external_id="*",
        uri=f"sync://{source_id}",
        action="updated",
    )
    subject = f"ingest.changes.{source_type}"
    producer = QueueProducer(js)

    try:
        await producer.publish(subject=subject, payload=event)
        log.debug("sync_event_published", subject=subject, run_id=str(run_id))
        return True
    except Exception as exc:
        log.error(
            "sync_publish_error",
            source_id=str(source_id),
            run_id=str(run_id),
            error=str(exc),
        )
        return False


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
    token: ApiToken = _read_scope_dep,
) -> list[SourceRead]:
    """List sources scoped to the caller's workspace.

    Query params: ``source_type``, ``status``
    Requires scope: ``sources:read``

    ACL: token workspace_id must be present; results filtered to
    ``Source.tenant_id == workspace_id`` only.  Legacy tokens (no
    workspace_id) are rejected fail-closed with 403.
    """
    workspace_id = _require_workspace(token)
    factory = _get_db_factory(request)

    db: AsyncSession
    async with factory() as db:
        stmt = select(Source).where(Source.tenant_id == workspace_id)
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
    token: ApiToken = _read_scope_dep,
) -> SourceRead:
    """Retrieve a single source by ID scoped to the caller's workspace.

    Returns 404 when the source does not exist OR belongs to a different
    workspace — avoids leaking resource existence.
    Requires scope: ``sources:read``
    """
    workspace_id = _require_workspace(token)
    factory = _get_db_factory(request)

    db: AsyncSession
    async with factory() as db:
        source = await _get_source_or_404(db, source_id, workspace_id=workspace_id)
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
    token: ApiToken = _write_scope_dep,
) -> SourceRead:
    """Partially update a source scoped to the caller's workspace.

    Returns 404 when the source does not exist OR belongs to a different
    workspace.
    Requires scope: ``sources:write``
    """
    workspace_id = _require_workspace(token)
    factory = _get_db_factory(request)

    db: AsyncSession
    async with factory() as db:
        source = await _get_source_or_404(db, source_id, workspace_id=workspace_id)

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
    token: ApiToken = _write_scope_dep,
) -> None:
    """Remove a source scoped to the caller's workspace.

    Returns 404 when the source does not exist OR belongs to a different
    workspace.
    Requires scope: ``sources:write``
    """
    workspace_id = _require_workspace(token)
    factory = _get_db_factory(request)

    db: AsyncSession
    async with factory() as db:
        source = await _get_source_or_404(db, source_id, workspace_id=workspace_id)
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
    token: ApiToken = _write_scope_dep,
) -> SyncResponse:
    """Trigger an immediate manual sync for the given source.

    Creates an ingestion run record and enqueues a sync event to
    ``ingest.changes.{source_type}`` via NATS JetStream.  When NATS is
    unavailable the run record is still committed and the response is still
    202 — the caller can monitor run progress via
    ``GET /api/v1/ingestion-runs/{run_id}``.

    Returns 404 when the source does not exist OR belongs to a different
    workspace.
    Requires scope: ``sources:write``
    """
    from omniscience_core.db.models import IngestionRun

    workspace_id = _require_workspace(token)
    factory = _get_db_factory(request)

    db: AsyncSession
    async with factory() as db:
        # Verify source exists within the caller's workspace.
        source = await _get_source_or_404(db, source_id, workspace_id=workspace_id)
        source_type = str(source.type)

        # Create an ingestion run record.
        run = IngestionRun(
            source_id=source_id,
            status=IngestionRunStatus.running,
        )
        db.add(run)
        await db.flush()
        await db.refresh(run)
        await db.commit()

        run_id: uuid.UUID = run.id

    # Publish the sync trigger event to NATS JetStream.
    # Failure is non-fatal: the IngestionRun is already committed.
    await _publish_sync_event(request, source_id, source_type, run_id)

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
    token: ApiToken = _read_scope_dep,
) -> SourceStatsResponse:
    """Return statistics for a source scoped to the caller's workspace.

    Returns 404 when the source does not exist OR belongs to a different
    workspace.
    Requires scope: ``sources:read``
    """
    from omniscience_core.db.models import Chunk, Document, IngestionRun
    from sqlalchemy import func

    workspace_id = _require_workspace(token)
    factory = _get_db_factory(request)

    db: AsyncSession
    async with factory() as db:
        source = await _get_source_or_404(db, source_id, workspace_id=workspace_id)

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
