"""Admin REST endpoints for ``resolve_incident`` calibration (issue #155).

Two endpoints under ``/api/v1/admin/incidents/scoring``:

* ``GET``  — read the active scoring config (workspace override or default).
* ``PUT``  — replace the workspace's weights + threshold.

ACL invariant
-------------

Both endpoints are workspace-scoped via the bearer token.  Workspace A
can never read or write workspace B's config — there is no ``tenant_id``
query parameter on either route by design.  The ``incidents:write``
scope (or ``admin``) is required for the PUT; ``stats:read`` (or higher)
is required for the GET — read access lives alongside other operator
read endpoints.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from omniscience_core.auth.middleware import get_current_token, require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.workspace import get_workspace_id
from omniscience_core.db.models import ApiToken
from sqlalchemy.ext.asyncio import AsyncSession

from omniscience_server.incidents_scoring import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_WEIGHTS,
    IncidentScoringConfig,
    IncidentScoringResponse,
    IncidentScoringUpdateRequest,
    IncidentScoringWeights,
    load_workspace_config,
    save_workspace_config,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin/incidents", tags=["incidents-admin"])

# Module-level Depends singletons — keeps ruff B008 happy.
_read_scope_dep: Any = Depends(require_scope(Scope.stats_read))
_write_scope_dep: Any = Depends(require_scope(Scope.incidents_write))
_current_token_dep: Any = Depends(get_current_token)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_workspace(token: ApiToken) -> uuid.UUID:
    """Return ``workspace_id`` or fail closed with 403 (no global override)."""
    workspace_id = get_workspace_id(token)
    if workspace_id is None:
        log.warning(
            "incidents_scoring_rejected_no_workspace",
            token_prefix=token.token_prefix,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": "Incident scoring admin requires a workspace-scoped token",
            },
        )
    return workspace_id


def _get_db_factory(request: Request) -> Any:
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "service_unavailable", "message": "Database not available"},
        )
    return factory


def _default_response() -> IncidentScoringResponse:
    """Build the response shown when the workspace has no override.

    Returns the equal-weights default plus the project-wide default
    threshold (vision §11).
    """
    return IncidentScoringResponse(
        weights=IncidentScoringWeights(**DEFAULT_WEIGHTS),
        confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD,
        weights_source="default",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/scoring",
    response_model=IncidentScoringResponse,
    summary="Read the active scoring config for the caller's workspace",
    dependencies=[_read_scope_dep],
)
async def get_scoring(
    request: Request,
    token: ApiToken = _current_token_dep,
) -> IncidentScoringResponse:
    """Return the active weights + threshold for the caller's workspace.

    When no override exists the response carries the default equal-weights
    fallback with ``weights_source = "default"``.  When the workspace has
    a configured override (or only a custom threshold), ``weights_source =
    "workspace"`` and the stored values are returned verbatim.

    Requires scope: ``stats:read`` (or ``admin``).
    """
    workspace_id = _require_workspace(token)
    factory = _get_db_factory(request)

    db: AsyncSession
    async with factory() as db:
        config = await load_workspace_config(db, workspace_id)

    if config is None:
        return _default_response()

    weights = config.weights or IncidentScoringWeights(**DEFAULT_WEIGHTS)
    return IncidentScoringResponse(
        weights=weights,
        confidence_threshold=config.confidence_threshold,
        weights_source="workspace",
    )


@router.put(
    "/scoring",
    response_model=IncidentScoringResponse,
    summary="Replace the scoring config for the caller's workspace",
    dependencies=[_write_scope_dep],
)
async def put_scoring(
    payload: IncidentScoringUpdateRequest,
    request: Request,
    token: ApiToken = _current_token_dep,
) -> IncidentScoringResponse:
    """Persist new weights + threshold for the caller's workspace.

    Pydantic enforces the sum-to-1.0 invariant (± 1e-3) and the per-field
    ``[0, 1]`` bound; FastAPI converts validation failures to 422 by
    default.  The workspace must already exist; missing workspaces map
    to 404.

    Requires scope: ``incidents:write`` (or ``admin``).
    """
    workspace_id = _require_workspace(token)
    factory = _get_db_factory(request)

    config = IncidentScoringConfig(
        weights=payload.weights,
        confidence_threshold=payload.confidence_threshold,
    )

    db: AsyncSession
    async with factory() as db:
        try:
            await save_workspace_config(db, workspace_id, config)
        except ValueError as exc:
            if str(exc).startswith("workspace_not_found:"):
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "workspace_not_found",
                        "message": f"Workspace {workspace_id} not found",
                    },
                ) from exc
            raise
        await db.commit()

    log.info(
        "incidents_scoring_updated",
        workspace_id=str(workspace_id),
        threshold=payload.confidence_threshold,
    )
    return IncidentScoringResponse(
        weights=payload.weights,
        confidence_threshold=payload.confidence_threshold,
        weights_source="workspace",
    )


__all__ = ["router"]
