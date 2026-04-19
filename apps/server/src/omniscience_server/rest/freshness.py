"""Freshness SLO REST endpoints.

GET /api/v1/freshness            — freshness report for all sources
GET /api/v1/freshness/{source_id} — freshness report for a single source

Both endpoints require the ``sources:read`` scope.

The response serialises the :class:`~omniscience_core.freshness.FreshnessReport`
Pydantic model directly.  ``age_seconds`` is capped at a large sentinel value
in JSON (instead of ``Infinity``) so that callers using strict JSON parsers
do not break.
"""

from __future__ import annotations

import math
import uuid
from typing import Any, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from omniscience_core.auth.middleware import require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.freshness import FreshnessChecker, FreshnessReport
from pydantic import BaseModel

log = structlog.get_logger(__name__)

router = APIRouter(tags=["freshness"])

# Module-level Depends singleton — avoids ruff B008.
_read_scope_dep: Any = Depends(require_scope(Scope.sources_read))

# JSON-safe sentinel used instead of ``Infinity`` for sources never synced.
_INFINITY_JSON_SAFE: float = 1e15


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class FreshnessReportResponse(BaseModel):
    """Wire format for a single source freshness report."""

    source_id: uuid.UUID
    source_name: str
    freshness_sla_seconds: int | None
    last_sync_at: str | None
    age_seconds: float
    is_stale: bool
    staleness_margin_seconds: float | None


class FreshnessAllResponse(BaseModel):
    """Wire format for the all-sources freshness endpoint."""

    sources: list[FreshnessReportResponse]
    total: int
    stale_count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _report_to_response(report: FreshnessReport) -> FreshnessReportResponse:
    """Convert a domain :class:`FreshnessReport` to the JSON-safe wire format."""
    age = _INFINITY_JSON_SAFE if math.isinf(report.age_seconds) else report.age_seconds
    margin: float | None
    if report.staleness_margin_seconds is None or math.isnan(report.staleness_margin_seconds):
        margin = None
    elif math.isinf(report.staleness_margin_seconds):
        margin = _INFINITY_JSON_SAFE
    else:
        margin = report.staleness_margin_seconds

    return FreshnessReportResponse(
        source_id=report.source_id,
        source_name=report.source_name,
        freshness_sla_seconds=report.freshness_sla_seconds,
        last_sync_at=report.last_sync_at.isoformat() if report.last_sync_at else None,
        age_seconds=age,
        is_stale=report.is_stale,
        staleness_margin_seconds=margin,
    )


def _get_checker(request: Request) -> FreshnessChecker:
    """Resolve or lazily create a :class:`FreshnessChecker` from app state."""
    existing = getattr(request.app.state, "freshness_checker", None)
    if existing is not None:
        return cast("FreshnessChecker", existing)

    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "service_unavailable", "message": "Database not available"},
        )
    return FreshnessChecker(factory)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/freshness",
    response_model=FreshnessAllResponse,
    summary="Freshness report for all sources",
    dependencies=[_read_scope_dep],
)
async def list_freshness(request: Request) -> FreshnessAllResponse:
    """Return a freshness SLO evaluation for every configured source.

    ``age_seconds`` is the elapsed time since ``last_sync_at``.  When a source
    has never been synced the value is set to ``1e15`` (a large finite sentinel
    that survives JSON round-trips).

    ``staleness_margin_seconds`` is ``null`` when no SLO is configured.  A
    positive value means the source is overdue; a negative value means it is
    within its SLO budget.

    Requires scope: ``sources:read``
    """
    checker = _get_checker(request)
    reports = await checker.check_all()
    responses = [_report_to_response(r) for r in reports]
    stale_count = sum(1 for r in reports if r.is_stale)
    return FreshnessAllResponse(
        sources=responses,
        total=len(responses),
        stale_count=stale_count,
    )


@router.get(
    "/freshness/{source_id}",
    response_model=FreshnessReportResponse,
    summary="Freshness report for a single source",
    dependencies=[_read_scope_dep],
)
async def get_source_freshness(
    source_id: uuid.UUID,
    request: Request,
) -> FreshnessReportResponse:
    """Return the freshness SLO evaluation for a single source.

    Requires scope: ``sources:read``
    """
    checker = _get_checker(request)
    try:
        report = await checker.check_source(source_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "source_not_found", "message": f"Source {source_id} not found"},
        ) from exc
    return _report_to_response(report)


__all__ = ["router"]
