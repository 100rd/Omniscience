"""Ingestion run read endpoints.

GET /api/v1/ingestion-runs         — list recent runs (query: source_id, status, limit)
GET /api/v1/ingestion-runs/{id}    — single run detail

Requires ``sources:read`` scope.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from omniscience_core.auth.middleware import require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.db.models import IngestionRun, IngestionRunStatus
from omniscience_core.db.schemas import IngestionRunRead
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

router = APIRouter(tags=["ingestion-runs"])

# Module-level Depends singleton — avoids ruff B008
_read_scope_dep: Any = Depends(require_scope(Scope.sources_read))

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def _get_db_factory(request: Request) -> Any:
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "service_unavailable", "message": "Database not available"},
        )
    return factory


@router.get(
    "/ingestion-runs",
    response_model=list[IngestionRunRead],
    summary="List ingestion runs",
    dependencies=[_read_scope_dep],
)
async def list_ingestion_runs(
    request: Request,
    source_id: uuid.UUID | None = None,
    status: IngestionRunStatus | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> list[IngestionRunRead]:
    """List recent ingestion runs, optionally filtered by source_id and/or status.

    Results are ordered newest-first.  Maximum ``limit`` is 200.

    Requires scope: ``sources:read``
    """
    clamped_limit = min(max(1, limit), _MAX_LIMIT)
    factory = _get_db_factory(request)

    db: AsyncSession
    async with factory() as db:
        stmt = select(IngestionRun).order_by(IngestionRun.started_at.desc()).limit(clamped_limit)
        if source_id is not None:
            stmt = stmt.where(IngestionRun.source_id == source_id)
        if status is not None:
            stmt = stmt.where(IngestionRun.status == status)

        result = await db.execute(stmt)
        runs = result.scalars().all()
        return [IngestionRunRead.model_validate(r) for r in runs]


@router.get(
    "/ingestion-runs/{run_id}",
    response_model=IngestionRunRead,
    summary="Get ingestion run",
    dependencies=[_read_scope_dep],
)
async def get_ingestion_run(
    run_id: uuid.UUID,
    request: Request,
) -> IngestionRunRead:
    """Retrieve a single ingestion run by ID.

    Requires scope: ``sources:read``
    """
    factory = _get_db_factory(request)

    db: AsyncSession
    async with factory() as db:
        run = await db.get(IngestionRun, run_id)
        if run is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "ingestion_run_not_found",
                    "message": f"Ingestion run {run_id} not found",
                },
            )
        return IngestionRunRead.model_validate(run)


__all__ = ["router"]
