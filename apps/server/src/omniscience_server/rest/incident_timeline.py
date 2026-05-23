"""Incident-timeline REST endpoint (issue #235).

``GET /api/v1/incidents/{alert_id}/timeline``

Returns the ordered list of bitemporal state changes for entities in
the alert's blast radius.  Optional query params:

- ``from``, ``to``       — half-open event-time window (ISO-8601 UTC).
- ``entity_types``       — repeatable allowlist of entity kinds.
- ``as_of``              — bitemporal anchor (ADR-0008 §5).
- ``format=mermaid``     — return ``text/plain`` Mermaid block instead
                           of JSON for embedding in markdown.

Workspace scoping mirrors the existing ``resolve_incident`` endpoint:
the caller's ``workspace_id`` is resolved from the bearer token; an
``alert_id`` not visible to the caller's workspace returns
``404 alert_not_found`` (no existence leak).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from urllib.parse import unquote

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from omniscience_core.auth.middleware import get_current_token, require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.workspace import get_workspace_id
from omniscience_core.db.models import ApiToken

from omniscience_server.as_of import (
    INVALID_TIMEZONE_CODE,
    INVALID_TIMEZONE_MESSAGE,
    enforce_utc,
)
from omniscience_server.incident_timeline import (
    TIMELINE_MAX_DEPTH,
    mcp_incident_timeline,
    render_mermaid,
)
from omniscience_server.incidents import (
    ALERT_NOT_FOUND_CODE,
    INVALID_ALERT_ID_CODE,
    INVALID_ALERT_ID_MESSAGE,
)

log = structlog.get_logger(__name__)

router = APIRouter(tags=["incidents"])

# Module-level Depends singletons (avoids ruff B008).
_search_scope_dep: Any = Depends(require_scope(Scope.search))
_current_token_dep: Any = Depends(get_current_token)

# Query parameter annotations.
_FromQuery = Annotated[
    datetime | None,
    Query(
        alias="from",
        description=(
            "Inclusive start of the event-time window (ISO-8601 UTC). "
            "Naive or non-UTC values are rejected with 400 invalid_timezone."
        ),
    ),
]
_ToQuery = Annotated[
    datetime | None,
    Query(
        alias="to",
        description=(
            "Exclusive end of the event-time window (ISO-8601 UTC). "
            "Naive or non-UTC values are rejected with 400 invalid_timezone."
        ),
    ),
]
_EntityTypesQuery = Annotated[
    list[str] | None,
    Query(
        description=(
            "Repeatable allowlist of entity kinds "
            "(e.g. 'k8s_pod', 'pull_request'). Omitted means no filter."
        ),
    ),
]
_AsOfQuery = Annotated[
    datetime | None,
    Query(
        description=(
            "Optional ISO-8601 timezone-aware UTC datetime anchoring the "
            "bitemporal traversal per ADR-0008 §5."
        ),
    ),
]
_FormatQuery = Annotated[
    str | None,
    Query(
        description=(
            "Output format: 'json' (default) or 'mermaid' to receive a "
            "text/plain Mermaid timeline block ready to embed in markdown."
        ),
    ),
]
_MaxDepthQuery = Annotated[
    int,
    Query(
        ge=1,
        le=5,
        description="BFS depth from the alert seed (1-5).",
    ),
]


def _validate_window(
    value: datetime | None,
    *,
    field: str,
) -> datetime | None:
    """Normalise an event-window bound to UTC, mapping errors to 400."""
    try:
        return enforce_utc(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": INVALID_TIMEZONE_CODE,
                "message": f"{field}: {INVALID_TIMEZONE_MESSAGE}",
            },
        ) from exc


@router.get(
    "/incidents/{alert_id:path}/timeline",
    summary="Render entity state changes around an incident window.",
    dependencies=[_search_scope_dep],
)
async def incident_timeline(
    alert_id: str,
    request: Request,
    from_ts: _FromQuery = None,
    to_ts: _ToQuery = None,
    entity_types: _EntityTypesQuery = None,
    as_of: _AsOfQuery = None,
    format: _FormatQuery = None,  # noqa: A002 — REST convention; shadows builtin
    max_depth: _MaxDepthQuery = TIMELINE_MAX_DEPTH,
    token: ApiToken = _current_token_dep,
) -> Any:
    """Return the ordered timeline of state changes for ``alert_id``.

    Mirrors :func:`omniscience_server.mcp.incident_timeline.incident_timeline`
    so the two transports are interchangeable.  Cross-workspace alerts
    return ``404 alert_not_found`` to preserve the "no existence leak"
    invariant (#117 / ADR-0005).
    """
    decoded_alert_id = unquote(alert_id)
    normalised_as_of = _validate_window(as_of, field="as_of")
    normalised_from = _validate_window(from_ts, field="from")
    normalised_to = _validate_window(to_ts, field="to")

    if (
        normalised_from is not None
        and normalised_to is not None
        and normalised_from > normalised_to
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_window",
                "message": "'from' must be <= 'to'",
            },
        )

    fmt = (format or "json").lower()
    if fmt not in {"json", "mermaid"}:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_format",
                "message": "format must be one of: json, mermaid",
            },
        )

    workspace_id = get_workspace_id(token)
    if workspace_id is None:
        log.warning(
            "incident_timeline_rejected_no_workspace",
            token_prefix=token.token_prefix,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": "Incident timeline requires a workspace-scoped token",
            },
        )

    try:
        payload = await mcp_incident_timeline(
            app=request.app,
            alert_id=decoded_alert_id,
            workspace_id=workspace_id,
            as_of=normalised_as_of,
            from_ts=normalised_from,
            to_ts=normalised_to,
            entity_types=entity_types,
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

    if fmt == "mermaid":
        return PlainTextResponse(content=render_mermaid(payload))
    return payload


__all__ = ["router"]
