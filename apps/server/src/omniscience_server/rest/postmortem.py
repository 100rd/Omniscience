"""Post-mortem REST endpoint (issue #232).

``GET /api/v1/incidents/{incident_id}/postmortem``

Generates a templated post-mortem document from the bitemporal graph
timeline for ``alert_id`` (supplied as a query parameter).  Mirrors the
auth posture of ``GET /incidents/{id}/timeline`` — scope ``search``,
workspace-scoped token required, no existence leak for cross-workspace
alerts.

Query parameters
----------------

* ``alert_id`` (required): canonical ``alert://`` URI to anchor the
  timeline on.  Path-level ``incident_id`` is used to scope follow-ups
  and runbook-step lookups; the two ids are kept distinct because
  PagerDuty incident ids and alert ids are not interchangeable.
* ``template`` (default ``blameless``): one of ``blameless``,
  ``five_whys``, ``coe``.
* ``format`` (default ``markdown``): one of ``markdown``, ``html``,
  ``json``.
* ``incident_start`` / ``incident_end``: optional ISO-8601 UTC window
  bounds.  When omitted, the timeline is unbounded on the left and
  anchored to "now" on the right.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from omniscience_core.auth.middleware import get_current_token, require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.workspace import get_workspace_id
from omniscience_core.db.models import ApiToken

from omniscience_server.as_of import (
    INVALID_TIMEZONE_CODE,
    INVALID_TIMEZONE_MESSAGE,
    enforce_utc,
)
from omniscience_server.postmortem import (
    INCIDENT_NOT_FOUND_CODE,
    INVALID_FORMAT_CODE,
    INVALID_INCIDENT_ID_CODE,
    INVALID_TEMPLATE_CODE,
    PostmortemError,
    generate_postmortem,
    render,
)

log = structlog.get_logger(__name__)

router = APIRouter(tags=["incidents"])

# Module-level Depends singletons to avoid ruff B008 in the route signature.
_search_scope_dep: Any = Depends(require_scope(Scope.search))
_current_token_dep: Any = Depends(get_current_token)


# ---------------------------------------------------------------------------
# Query parameter annotations
# ---------------------------------------------------------------------------


_AlertIdQuery = Annotated[
    str,
    Query(
        min_length=1,
        max_length=512,
        description="Canonical alert URI 'alert://{provider}/{provider_alert_id}'.",
    ),
]
_TemplateQuery = Annotated[
    str,
    Query(description="One of: blameless, five_whys, coe."),
]
_FormatQuery = Annotated[
    str,
    Query(description="Render format: markdown (default), html, or json."),
]
_IncidentStartQuery = Annotated[
    datetime | None,
    Query(
        alias="incident_start",
        description=(
            "Inclusive UTC start of the incident window (ISO-8601). "
            "Naive datetimes rejected with 400 invalid_timezone."
        ),
    ),
]
_IncidentEndQuery = Annotated[
    datetime | None,
    Query(
        alias="incident_end",
        description=(
            "Exclusive UTC end of the incident window (ISO-8601). "
            "Defaults to server-side 'now' when omitted."
        ),
    ),
]


def _validate_window_bound(value: datetime | None, *, field: str) -> datetime | None:
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


def _require_workspace(token: ApiToken) -> Any:
    workspace_id = get_workspace_id(token)
    if workspace_id is None:
        log.warning(
            "postmortem_request_rejected_no_workspace",
            token_prefix=token.token_prefix,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": "Post-mortem endpoints require a workspace-scoped token",
            },
        )
    return workspace_id


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get(
    "/incidents/{incident_id}/postmortem",
    summary="Generate a templated post-mortem from the bitemporal timeline",
    dependencies=[_search_scope_dep],
)
async def get_postmortem(
    incident_id: str,
    request: Request,
    alert_id: _AlertIdQuery,
    template: _TemplateQuery = "blameless",
    format: _FormatQuery = "markdown",  # noqa: A002 — REST convention
    incident_start: _IncidentStartQuery = None,
    incident_end: _IncidentEndQuery = None,
    token: ApiToken = _current_token_dep,
) -> Any:
    """Render the templated post-mortem.

    Errors:

    * ``400 invalid_template`` — unknown template id.
    * ``400 invalid_format`` — unknown format.
    * ``400 invalid_incident_id`` — incident_id failed validation.
    * ``400 invalid_window`` — incident_start > incident_end.
    * ``400 invalid_timezone`` — naive / non-UTC datetime supplied.
    * ``403 forbidden`` — token is not workspace-scoped.
    * ``404 incident_not_found`` — alert not visible to the workspace.
    """
    workspace_id = _require_workspace(token)
    start = _validate_window_bound(incident_start, field="incident_start")
    end = _validate_window_bound(incident_end, field="incident_end")
    if start is not None and end is not None and start > end:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_window",
                "message": "incident_start must be <= incident_end",
            },
        )

    try:
        report = await generate_postmortem(
            app=request.app,
            incident_id=incident_id,
            alert_id=alert_id,
            workspace_id=workspace_id,
            template=template,
            incident_start=start,
            incident_end=end,
        )
    except PostmortemError as exc:
        _raise_from_postmortem_error(exc)
        raise  # pragma: no cover - the helper always raises HTTPException
    except ValueError as exc:
        msg = str(exc)
        # Surface the upstream ``invalid_alert_id`` envelope unchanged so
        # REST clients can share their error handlers.
        if msg.startswith("invalid_alert_id:"):
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_alert_id", "message": msg.split(":", 1)[1]},
            ) from exc
        raise

    try:
        rendered = render(report, fmt=format)
    except PostmortemError as exc:
        _raise_from_postmortem_error(exc)
        raise  # pragma: no cover

    if format == "html":
        return HTMLResponse(content=rendered)
    if format == "json":
        # We already have the JSON string; return it via JSONResponse so
        # the Content-Type header is correct.
        return JSONResponse(content=report.model_dump(mode="json"))
    return PlainTextResponse(content=rendered, media_type="text/markdown")


def _raise_from_postmortem_error(exc: PostmortemError) -> None:
    """Map a :class:`PostmortemError` to the canonical HTTPException envelope."""
    status_by_code = {
        INVALID_TEMPLATE_CODE: 400,
        INVALID_FORMAT_CODE: 400,
        INVALID_INCIDENT_ID_CODE: 400,
        INCIDENT_NOT_FOUND_CODE: 404,
    }
    status = status_by_code.get(exc.code, 400)
    raise HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": exc.message},
    ) from exc


__all__ = ["router"]
