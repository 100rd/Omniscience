"""Incident-resolution REST endpoint (issue #153, mirror of MCP tool).

POST /api/v1/incidents/{alert_id}/resolve

Workspace scoping (issue #117 / #153 §D)
----------------------------------------

The caller's ``workspace_id`` is resolved from the authenticated bearer
token and propagated into every ``GraphStore`` call.  A token without a
workspace is rejected with HTTP 403; an ``alert_id`` not visible to the
caller's workspace is rejected with HTTP 404 ``alert_not_found`` —
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
from pydantic import BaseModel, Field

from omniscience_server.as_of import (
    INVALID_TIMEZONE_CODE,
    INVALID_TIMEZONE_MESSAGE,
    enforce_utc,
)
from omniscience_server.incidents import (
    ALERT_NOT_FOUND_CODE,
    DEFAULT_MAX_DEPTH,
    INVALID_ALERT_ID_CODE,
    INVALID_ALERT_ID_MESSAGE,
    MAX_MAX_DEPTH,
    MIN_MAX_DEPTH,
    mcp_resolve_incident,
)

log = structlog.get_logger(__name__)

router = APIRouter(tags=["incidents"])

# Module-level Depends singletons (avoids ruff B008).
_search_scope_dep: Any = Depends(require_scope(Scope.search))
_current_token_dep: Any = Depends(get_current_token)

# Query parameter annotations.
_MaxDepthQuery = Annotated[
    int,
    Query(
        ge=MIN_MAX_DEPTH,
        le=MAX_MAX_DEPTH,
        description="Maximum BFS hops from the alert seed.",
    ),
]
_AsOfQuery = Annotated[
    datetime | None,
    Query(
        description=(
            "Optional ISO-8601 timezone-aware UTC datetime "
            "(e.g. '2026-04-12T19:25:00Z'). When supplied, the alert "
            "and its related entities are resolved against the bitemporal "
            "state at this time per ADR-0008 §5. Naive or non-UTC values "
            "are rejected with 400 INVALID_TIMEZONE."
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


@router.post(
    "/incidents/{alert_id:path}/resolve",
    summary="Resolve an alert into a recommendation bundle",
    dependencies=[_search_scope_dep],
)
async def resolve_incident(
    alert_id: str,
    request: Request,
    max_depth: _MaxDepthQuery = DEFAULT_MAX_DEPTH,
    as_of: _AsOfQuery = None,
    token: ApiToken = _current_token_dep,
) -> dict[str, Any]:
    """REST mirror of the MCP ``resolve_incident`` tool.

    Returns the same JSON shape as the MCP surface so the two transports
    are interchangeable.  ``alert_id`` is read from the URL path; other
    forms return ``400 invalid_alert_id``.  Cross-workspace alerts
    return ``404 alert_not_found`` to preserve the "no existence leak"
    invariant from issue #117 / ADR-0005.
    """
    decoded_alert_id = unquote(alert_id)
    normalised_as_of = _validate_as_of(as_of)

    workspace_id = get_workspace_id(token)
    if workspace_id is None:
        log.warning(
            "resolve_incident_rejected_no_workspace",
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
        return await mcp_resolve_incident(
            app=request.app,
            alert_id=decoded_alert_id,
            workspace_id=workspace_id,
            as_of=normalised_as_of,
            max_depth=max_depth,
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
                    "message": f"Alert '{decoded_alert_id}' not found",
                },
            ) from exc
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_request", "message": msg},
        ) from exc


class IncidentFeedback(BaseModel):
    """User feedback on an incident resolution."""

    predicted_confidence: float = Field(ge=0.0, le=1.0)
    true_label: int = Field(description="1 if correct, 0 if incorrect", ge=0, le=1)


@router.post(
    "/incidents/{alert_id:path}/feedback",
    summary="Submit user feedback on a resolved incident for calibration",
    status_code=204,
)
async def submit_feedback(
    alert_id: str,
    feedback: IncidentFeedback,
    request: Request,
    token: ApiToken = _current_token_dep,
) -> None:
    """Collects user feedback to calibrate the confidence_score model."""
    workspace_id = get_workspace_id(token)
    if workspace_id is None:
        raise HTTPException(status_code=403, detail="Workspace-scoped token required")

    # In a real implementation, this would persist the feedback to the database
    # For now, we emit a structured log event that can be aggregated
    log.info(
        "incident_feedback_collected",
        alert_id=unquote(alert_id),
        workspace_id=str(workspace_id),
        predicted_confidence=feedback.predicted_confidence,
        true_label=feedback.true_label,
        timestamp=datetime.utcnow().isoformat() + "Z",
    )


__all__ = ["router"]
