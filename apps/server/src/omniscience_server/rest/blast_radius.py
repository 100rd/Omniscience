"""Blast-radius REST endpoint (issue #234, mirror of MCP tool).

GET /api/v1/blast-radius?entity_id=...&action=...&as_of=...

Workspace scoping (issue #117 / #234)
-------------------------------------

The caller's ``workspace_id`` is resolved from the authenticated bearer
token and propagated into every ``GraphStore`` call.  A token without a
workspace is rejected with HTTP 403; an ``entity_id`` not visible to
the caller's workspace is rejected with HTTP 404 ``entity_not_found`` —
indistinguishable from an entity that does not exist, by design (no
existence leak).

ADR refs
--------

- ADR-0005: workspace-scoped graph reads.
- ADR-0008 §5: bitemporal predicate / ``as_of`` semantics.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from omniscience_core.auth.middleware import get_current_token, require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.workspace import get_workspace_id
from omniscience_core.db.models import ApiToken

from omniscience_server.as_of import (
    INVALID_TIMEZONE_CODE,
    INVALID_TIMEZONE_MESSAGE,
    enforce_utc,
)
from omniscience_server.blast_radius import (
    ACTION_TYPES,
    DEFAULT_MAX_DEPTH,
    ENTITY_NOT_FOUND_CODE,
    INVALID_ACTION_TYPE_CODE,
    INVALID_ACTION_TYPE_MESSAGE,
    INVALID_ENTITY_ID_CODE,
    INVALID_ENTITY_ID_MESSAGE,
    MAX_MAX_DEPTH,
    MIN_MAX_DEPTH,
    ActionType,
    mcp_blast_radius,
)

log = structlog.get_logger(__name__)

router = APIRouter(tags=["blast-radius"])

# Module-level Depends singletons (avoids ruff B008).
_search_scope_dep: Any = Depends(require_scope(Scope.search))
_current_token_dep: Any = Depends(get_current_token)

# Query parameter annotations.
_EntityIdQuery = Annotated[
    str,
    Query(
        min_length=1,
        description=(
            "Canonical seed entity name (e.g. 'service://api', 'pod/api-7f9b'). "
            "The same canonical-name vocabulary the connectors plant."
        ),
    ),
]
_ActionQuery = Annotated[
    str,
    Query(
        alias="action",
        description=(
            "Which action's blast radius to compute. One of: "
            + ", ".join(ACTION_TYPES)
            + ". See docs/api/blast-radius.md for per-action semantics."
        ),
    ),
]
_MaxDepthQuery = Annotated[
    int,
    Query(
        ge=MIN_MAX_DEPTH,
        le=MAX_MAX_DEPTH,
        description="Maximum BFS hops from the seed entity.",
    ),
]
_AsOfQuery = Annotated[
    datetime | None,
    Query(
        description=(
            "Optional ISO-8601 timezone-aware UTC datetime "
            "(e.g. '2026-05-22T19:25:00Z'). When supplied, the seed and "
            "its downstream set are resolved against the bitemporal "
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


def _coerce_action(action: str) -> ActionType:
    """Validate and narrow ``action`` to :data:`ActionType`."""
    if action not in ACTION_TYPES:
        raise HTTPException(
            status_code=400,
            detail={
                "code": INVALID_ACTION_TYPE_CODE,
                "message": INVALID_ACTION_TYPE_MESSAGE,
            },
        )
    # Narrowing for mypy --strict: a runtime check above guarantees this.
    return action


@router.get(
    "/blast-radius",
    summary="Compute the downstream impact set for an action on an entity",
    dependencies=[_search_scope_dep],
)
async def blast_radius(
    request: Request,
    entity_id: _EntityIdQuery,
    action: _ActionQuery = "restart",
    max_depth: _MaxDepthQuery = DEFAULT_MAX_DEPTH,
    as_of: _AsOfQuery = None,
    token: ApiToken = _current_token_dep,
) -> dict[str, Any]:
    """REST mirror of the MCP ``blast_radius`` tool.

    Returns the same JSON shape as the MCP surface so the two transports
    are interchangeable.  Cross-workspace entities return
    ``404 entity_not_found`` to preserve the "no existence leak"
    invariant from issue #117 / ADR-0005.
    """
    normalised_as_of = _validate_as_of(as_of)
    action_type = _coerce_action(action)

    workspace_id = get_workspace_id(token)
    if workspace_id is None:
        log.warning(
            "blast_radius_rejected_no_workspace",
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
        return await mcp_blast_radius(
            app=request.app,
            entity_id=entity_id,
            workspace_id=workspace_id,
            action_type=action_type,
            as_of=normalised_as_of,
            max_depth=max_depth,
        )
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith(f"{INVALID_ENTITY_ID_CODE}:"):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": INVALID_ENTITY_ID_CODE,
                    "message": INVALID_ENTITY_ID_MESSAGE,
                },
            ) from exc
        if msg.startswith(f"{INVALID_ACTION_TYPE_CODE}:"):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": INVALID_ACTION_TYPE_CODE,
                    "message": INVALID_ACTION_TYPE_MESSAGE,
                },
            ) from exc
        if msg.startswith(f"{ENTITY_NOT_FOUND_CODE}:"):
            raise HTTPException(
                status_code=404,
                detail={
                    "code": ENTITY_NOT_FOUND_CODE,
                    "message": f"Entity '{entity_id}' not found",
                },
            ) from exc
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_request", "message": msg},
        ) from exc


__all__ = ["router"]
