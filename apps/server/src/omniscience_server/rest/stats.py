"""Stats REST endpoints for the admin dashboard (issue #111, epic #109).

Endpoints
---------

* ``GET /api/v1/stats/overview``           — single-call dashboard summary
* ``GET /api/v1/stats/sources``            — per-source table
* ``GET /api/v1/stats/entities-by-kind``   — entity-kind histogram
* ``GET /api/v1/stats/edges-by-type``      — edge-type histogram
* ``GET /api/v1/stats/activity``           — windowed document-activity counts

All endpoints require the ``stats:read`` scope.  All responses are
workspace-scoped: counts include only sources, documents, chunks,
entities, and edges that belong to the caller's workspace.

Architecture
------------
Endpoints are intentionally thin.  Every count goes through
:class:`omniscience_core.stats.StatsService`, which is the single place
that knows how to read from Postgres + Neo4j + Qdrant.  This keeps the
REST surface backend-neutral and lets us evolve the storage backends
without touching the dashboard contract.

Workspace ACL
-------------
A token without an associated workspace fails closed with 403.  The
graph and chunk stores require an explicit ``workspace_id`` and we
refuse to invent one on behalf of the caller — see ``rest/entities.py``
for the same pattern.  This is the second line of defence after the
protocol-level ``workspace_id`` invariants on ``GraphStore`` and
``VectorStore``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from omniscience_core.auth.middleware import get_current_token, require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.workspace import get_workspace_id
from omniscience_core.db.models import ApiToken
from omniscience_core.stats import (
    ActivityResponse,
    ClientsStatsResponse,
    ClientsStatsService,
    EdgesByTypeResponse,
    EntitiesByKindResponse,
    SourcesStatsResponse,
    StatsOverview,
    StatsService,
)
from omniscience_core.stats.service import _ACTIVITY_HOURS_MAX, _ACTIVITY_HOURS_MIN
from omniscience_core.storage.graph import GraphStore
from omniscience_core.storage.vector import VectorStore

log = structlog.get_logger(__name__)

router = APIRouter(tags=["stats"])

# Module-level Depends singletons — avoids ruff B008
_stats_scope_dep: Any = Depends(require_scope(Scope.stats_read))
_current_token_dep: Any = Depends(get_current_token)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_service(request: Request) -> StatsService:
    """Resolve a :class:`StatsService` instance from app state.

    The service is cached on ``request.app.state.stats_service`` to keep
    the in-process TTL cache shared across requests.  When the slot is
    empty (e.g. during testing) we lazily build one from the wired
    stores; tests that mock the stores will therefore see a fresh
    service per app.
    """
    existing = getattr(request.app.state, "stats_service", None)
    if isinstance(existing, StatsService):
        return existing

    factory = getattr(request.app.state, "db_session_factory", None)
    graph_store: GraphStore | None = getattr(request.app.state, "graph_store", None)
    vector_store: VectorStore | None = getattr(request.app.state, "vector_store", None)
    if factory is None or graph_store is None or vector_store is None:
        log.warning("stats_service_unavailable")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "service_unavailable",
                "message": "Stats backend not available",
            },
        )

    service = StatsService(
        db_session_factory=factory,
        graph_store=graph_store,
        vector_store=vector_store,
    )
    request.app.state.stats_service = service
    return service


def _require_workspace(token: ApiToken) -> uuid.UUID:
    """Return the caller's workspace_id.

    Stats endpoints aggregate counts from Neo4j and Qdrant, both of
    which require an explicit workspace.  get_workspace_id raises
    PermissionError("forbidden:workspace_required") for legacy tokens;
    the global handler in rest/errors.py converts that to HTTP 403.
    """
    return get_workspace_id(token)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/stats/overview",
    response_model=StatsOverview,
    summary="Operator dashboard summary",
    dependencies=[_stats_scope_dep],
)
async def stats_overview(
    request: Request,
    token: ApiToken = _current_token_dep,
) -> StatsOverview:
    """Aggregate operator summary across Postgres + Neo4j + Qdrant.

    Returns totals, last-24h deltas, edge-type histogram, and total
    indexed bytes — everything the admin dashboard needs in one call.

    Requires scope: ``stats:read``
    Requires: token scoped to a workspace (fails closed with 403 otherwise).
    """
    workspace_id = _require_workspace(token)
    service = _resolve_service(request)
    overview = await service.overview(workspace_id=workspace_id)
    log.info(
        "stats_overview",
        workspace_id=str(workspace_id),
        sources=overview.sources,
        documents=overview.documents,
        chunks=overview.chunks,
    )
    return overview


@router.get(
    "/stats/activity",
    response_model=ActivityResponse,
    summary="Windowed document-activity counts",
    dependencies=[_stats_scope_dep],
)
async def stats_activity(
    request: Request,
    token: ApiToken = _current_token_dep,
    hours: Annotated[
        int,
        Query(
            ge=_ACTIVITY_HOURS_MIN,
            le=_ACTIVITY_HOURS_MAX,
            description=(
                f"Width of the time window in hours "
                f"({_ACTIVITY_HOURS_MIN}-{_ACTIVITY_HOURS_MAX}). "
                "Default: 24."
            ),
        ),
    ] = 24,
) -> ActivityResponse:
    """Windowed document-activity counts for the admin dashboard.

    Returns counts of new, updated, and tombstoned documents within the
    requested time window, all scoped to the caller's workspace.

    Field semantics:
    - ``new``        — first-time indexings (``doc_version == 1``) in window.
    - ``updated``    — re-indexed documents (``doc_version > 1``) in window.
    - ``tombstoned`` — documents soft-deleted in window.
    - ``window_hours`` — the window echoed back for client-side cross-check.

    Requires scope: ``stats:read``
    Requires: token scoped to a workspace (fails closed with 403 otherwise).
    """
    workspace_id = _require_workspace(token)
    service = _resolve_service(request)
    return await service.activity(workspace_id=workspace_id, window_hours=hours)


@router.get(
    "/stats/sources",
    response_model=SourcesStatsResponse,
    summary="Per-source stats table",
    dependencies=[_stats_scope_dep],
)
async def stats_sources(
    request: Request,
    token: ApiToken = _current_token_dep,
) -> SourcesStatsResponse:
    """Per-source dashboard table: documents, chunks, entities, freshness.

    Requires scope: ``stats:read``
    Requires: token scoped to a workspace (fails closed with 403 otherwise).
    """
    workspace_id = _require_workspace(token)
    service = _resolve_service(request)
    response = await service.sources(workspace_id=workspace_id)
    log.info(
        "stats_sources",
        workspace_id=str(workspace_id),
        sources=response.total,
    )
    return response


@router.get(
    "/stats/entities-by-kind",
    response_model=EntitiesByKindResponse,
    summary="Entity-kind histogram",
    dependencies=[_stats_scope_dep],
)
async def stats_entities_by_kind(
    request: Request,
    token: ApiToken = _current_token_dep,
) -> EntitiesByKindResponse:
    """Histogram of entity kinds (e.g. terraform_resource, k8s_resource).

    Requires scope: ``stats:read``
    Requires: token scoped to a workspace (fails closed with 403 otherwise).
    """
    workspace_id = _require_workspace(token)
    service = _resolve_service(request)
    response = await service.entities_by_kind(workspace_id=workspace_id)
    log.info(
        "stats_entities_by_kind",
        workspace_id=str(workspace_id),
        kinds=len(response.entries),
    )
    return response


def _resolve_clients_service(request: Request) -> ClientsStatsService:
    """Resolve a :class:`ClientsStatsService` from app state.

    Cached on ``request.app.state.clients_stats_service`` so the 5-second
    TTL cache survives across requests.  When not pre-wired (test path)
    we lazily build one from the session factory; the global telemetry
    registry is shared by every instance.
    """
    existing = getattr(request.app.state, "clients_stats_service", None)
    if isinstance(existing, ClientsStatsService):
        return existing

    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        log.warning("clients_stats_service_unavailable")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "service_unavailable",
                "message": "Clients-stats backend not available",
            },
        )

    service = ClientsStatsService(db_session_factory=factory)
    request.app.state.clients_stats_service = service
    return service


@router.get(
    "/stats/clients",
    response_model=ClientsStatsResponse,
    summary="Connected clients + token usage telemetry",
    dependencies=[_stats_scope_dep],
)
async def stats_clients(
    request: Request,
    token: ApiToken = _current_token_dep,
) -> ClientsStatsResponse:
    """Connected-clients telemetry rollup (issue #113).

    Returns: active MCP sessions, sessions in the last hour, per-token
    request volume (15m + 24h windows), and the top-N most-invoked
    tools in the last hour — all workspace-scoped: only tokens that
    belong to the caller's workspace are listed.

    Requires scope: ``stats:read``
    Requires: token scoped to a workspace (fails closed with 403 otherwise).
    """
    workspace_id = _require_workspace(token)
    service = _resolve_clients_service(request)
    response = await service.clients(workspace_id=workspace_id)
    log.info(
        "stats_clients",
        workspace_id=str(workspace_id),
        tokens=len(response.tokens),
        mcp_sessions_active=response.mcp_sessions_active,
        top_tools=len(response.top_tools_last_hour),
    )
    return response


@router.get(
    "/stats/edges-by-type",
    response_model=EdgesByTypeResponse,
    summary="Edge-type histogram",
    dependencies=[_stats_scope_dep],
)
async def stats_edges_by_type(
    request: Request,
    token: ApiToken = _current_token_dep,
) -> EdgesByTypeResponse:
    """Histogram of edge types (e.g. calls, imports, depends_on).

    Requires scope: ``stats:read``
    Requires: token scoped to a workspace (fails closed with 403 otherwise).
    """
    workspace_id = _require_workspace(token)
    service = _resolve_service(request)
    response = await service.edges_by_type(workspace_id=workspace_id)
    log.info(
        "stats_edges_by_type",
        workspace_id=str(workspace_id),
        edge_types=len(response.entries),
    )
    return response


__all__ = ["router"]
