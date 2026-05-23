"""Similar-incidents REST endpoint (issue #233, mirror of MCP tool).

GET /api/v1/incidents/{incident_id}/similar

Workspace scoping (issue #117 / #233)
-------------------------------------

The caller's ``workspace_id`` is resolved from the authenticated bearer
token and propagated into every ``GraphStore`` call.  A token without a
workspace is rejected with HTTP 403; an ``incident_id`` not visible to
the caller's workspace is rejected with HTTP 404 ``alert_not_found`` —
indistinguishable from an alert that does not exist, by design (no
existence leak).

ADR refs
--------

- ADR-0005: workspace-scoped graph reads.
- ADR-0008 §5: bitemporal predicate / ``as_of`` semantics.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from urllib.parse import unquote

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from omniscience_core.auth.middleware import get_current_token, require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.workspace import get_workspace_id
from omniscience_core.db.models import ApiToken
from omniscience_index.clustering import DEFAULT_LIMIT, DEFAULT_SINCE_DAYS

from omniscience_server.as_of import (
    INVALID_TIMEZONE_CODE,
    INVALID_TIMEZONE_MESSAGE,
    enforce_utc,
)
from omniscience_server.incidents import (
    ALERT_NOT_FOUND_CODE,
    INVALID_ALERT_ID_CODE,
    INVALID_ALERT_ID_MESSAGE,
)
from omniscience_server.similar_incidents import (
    MAX_LIMIT,
    MAX_SINCE_DAYS,
    MIN_LIMIT,
    MIN_SINCE_DAYS,
    mcp_find_similar_incidents,
)

log = structlog.get_logger(__name__)

router = APIRouter(tags=["incidents"])

# Module-level Depends singletons (avoids ruff B008).
_search_scope_dep: Any = Depends(require_scope(Scope.search))
_current_token_dep: Any = Depends(get_current_token)

# Query parameter annotations.
_LimitQuery = Annotated[
    int,
    Query(
        ge=MIN_LIMIT,
        le=MAX_LIMIT,
        description="Maximum number of similar past incidents to return.",
    ),
]
_SinceDaysQuery = Annotated[
    int,
    Query(
        ge=MIN_SINCE_DAYS,
        le=MAX_SINCE_DAYS,
        description=(
            "Lookback window in days; candidates older than this are filtered out before scoring."
        ),
    ),
]
_AsOfQuery = Annotated[
    datetime | None,
    Query(
        description=(
            "Optional ISO-8601 timezone-aware UTC datetime "
            "(e.g. '2026-05-22T19:25:00Z'). When supplied, the seed and the "
            "candidate set are resolved against the bitemporal state at this "
            "time per ADR-0008 §5. Naive or non-UTC values are rejected with "
            "400 INVALID_TIMEZONE."
        ),
    ),
]


def _validate_as_of(as_of: datetime | None) -> datetime | None:
    """Run the shared UTC enforcement, mapping errors to HTTP 400."""
    try:
        return enforce_utc(as_of)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": INVALID_TIMEZONE_CODE, "message": INVALID_TIMEZONE_MESSAGE},
        ) from exc


@router.get(
    "/incidents/{incident_id:path}/similar",
    summary="List ranked past incidents similar to the seed",
    dependencies=[_search_scope_dep],
)
async def find_similar_incidents(
    incident_id: str,
    request: Request,
    limit: _LimitQuery = DEFAULT_LIMIT,
    since_days: _SinceDaysQuery = DEFAULT_SINCE_DAYS,
    as_of: _AsOfQuery = None,
    token: ApiToken = _current_token_dep,
) -> dict[str, Any]:
    """REST mirror of the MCP ``find_similar_incidents`` tool.

    Returns the same JSON shape as the MCP surface so the two transports
    are interchangeable.  Cross-workspace incidents return
    ``404 alert_not_found`` to preserve the "no existence leak"
    invariant from issue #117 / ADR-0005.
    """
    decoded_incident_id = unquote(incident_id)
    normalised_as_of = _validate_as_of(as_of)

    workspace_id = get_workspace_id(token)
    if workspace_id is None:
        log.warning(
            "find_similar_incidents_rejected_no_workspace",
            token_prefix=token.token_prefix,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": "Graph retrieval requires a workspace-scoped token",
            },
        )

    try:
        return await mcp_find_similar_incidents(
            app=request.app,
            incident_id=decoded_incident_id,
            workspace_id=workspace_id,
            limit=limit,
            since_days=since_days,
            as_of=normalised_as_of,
        )
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith(f"{INVALID_ALERT_ID_CODE}:"):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": INVALID_ALERT_ID_CODE,
                    "message": INVALID_ALERT_ID_MESSAGE,
                },
            ) from exc
        if msg.startswith(f"{ALERT_NOT_FOUND_CODE}:"):
            raise HTTPException(
                status_code=404,
                detail={
                    "code": ALERT_NOT_FOUND_CODE,
                    "message": f"Incident '{decoded_incident_id}' not found",
                },
            ) from exc
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_request", "message": msg},
        ) from exc


__all__ = ["router"]
