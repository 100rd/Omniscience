"""Runbook REST endpoints (issue #231).

Two routes under ``/api/v1``:

* ``POST /api/v1/incidents/{incident_id}/runbook-step``
  Records that a responder executed a step from a runbook against an
  incident.  The event flows into the bitemporal audit surfaces (in-
  memory ring buffer, structured log line, Prometheus counter).
* ``POST /api/v1/incidents/{incident_id}/suggest-runbook``
  Sugar over the MCP ``suggest_runbook`` tool — accepts the alert id in
  the JSON body so the path parameter slot is consistent with the
  step-recording endpoint.

Both routes:

* require scope ``incidents_write`` for the step-recording endpoint and
  ``search`` for suggest (mirrors the resolve_incident posture);
* require a workspace-scoped token — fail-closed 403;
* propagate workspace_id from the token, never the body.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from omniscience_core.auth.middleware import get_current_token, require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.workspace import get_workspace_id
from omniscience_core.db.models import ApiToken
from pydantic import BaseModel, ConfigDict, Field, field_validator

from omniscience_server.as_of import (
    INVALID_TIMEZONE_CODE,
    INVALID_TIMEZONE_MESSAGE,
    enforce_utc,
)
from omniscience_server.runbook import (
    ALERT_NOT_FOUND_CODE,
    DEFAULT_TOP_K,
    INVALID_ALERT_ID_CODE,
    INVALID_ALERT_ID_MESSAGE,
    INVALID_RUNBOOK_NAME_CODE,
    INVALID_RUNBOOK_NAME_MESSAGE,
    INVALID_STEP_ID_CODE,
    MAX_TOP_K,
    record_runbook_step,
    suggest_runbook,
)

log = structlog.get_logger(__name__)

router = APIRouter(tags=["runbook"])

# Module-level Depends singletons (avoids ruff B008).
_search_scope_dep: Any = Depends(require_scope(Scope.search))
_incidents_write_scope_dep: Any = Depends(require_scope(Scope.incidents_write))
_current_token_dep: Any = Depends(get_current_token)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_workspace(token: ApiToken) -> uuid.UUID:
    """Resolve the caller's workspace or fail 403 (no cross-workspace IDs)."""
    workspace_id = get_workspace_id(token)
    if workspace_id is None:
        log.warning(
            "runbook_request_rejected_no_workspace",
            token_prefix=token.token_prefix,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": "Runbook endpoints require a workspace-scoped token",
            },
        )
    return workspace_id


# ---------------------------------------------------------------------------
# Request / response bodies
# ---------------------------------------------------------------------------


class RunbookStepRequest(BaseModel):
    """POST /api/v1/incidents/{incident_id}/runbook-step body."""

    model_config = ConfigDict(extra="forbid")

    alert_id: str = Field(
        description="Canonical alert URI 'alert://{provider}/{provider_alert_id}'.",
    )
    runbook_name: str = Field(
        description="Canonical runbook URI 'runbook://{source_uid}/{rel_path}'.",
    )
    step_id: str = Field(
        description=(
            "Slugified heading text identifying the step.  Matches "
            "[A-Za-z0-9._-]+ — the same shape the parser emits."
        ),
    )
    outcome: str = Field(
        default="executed",
        description=(
            "Free-form outcome label.  Conventional values: 'executed', "
            "'skipped', 'failed', 'rolled_back'.  Stored verbatim."
        ),
        max_length=64,
    )
    actor: str | None = Field(
        default=None,
        description="Display-name or id of the responder who took the step.",
        max_length=256,
    )
    note: str | None = Field(
        default=None,
        description="Optional free-text note from the responder.",
        max_length=4096,
    )
    valid_from: datetime | None = Field(
        default=None,
        description=(
            "ISO-8601 UTC datetime at which the step actually executed. "
            "Default: server-side 'now'.  Naive datetimes are rejected "
            "with 400 invalid_timezone."
        ),
    )

    @field_validator("valid_from")
    @classmethod
    def _validate_valid_from(cls, value: datetime | None) -> datetime | None:
        try:
            return enforce_utc(value)
        except ValueError as exc:
            raise ValueError(INVALID_TIMEZONE_CODE) from exc


class SuggestRunbookRequest(BaseModel):
    """POST /api/v1/incidents/{incident_id}/suggest-runbook body."""

    model_config = ConfigDict(extra="forbid")

    alert_id: str = Field(description="Canonical alert URI to score against.")
    top_k: int = Field(
        default=DEFAULT_TOP_K,
        ge=1,
        le=MAX_TOP_K,
        description="Number of suggestions to return.",
    )
    as_of: datetime | None = Field(
        default=None,
        description="Optional ADR-0008 §5 bitemporal anchor.",
    )

    @field_validator("as_of")
    @classmethod
    def _validate_as_of(cls, value: datetime | None) -> datetime | None:
        try:
            return enforce_utc(value)
        except ValueError as exc:
            raise ValueError(INVALID_TIMEZONE_CODE) from exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


_IncidentIdPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=256,
        description="Caller-supplied incident identifier (PagerDuty id, etc.).",
    ),
]


@router.post(
    "/incidents/{incident_id}/runbook-step",
    status_code=201,
    summary="Record a runbook step execution as a bitemporal event",
    dependencies=[_incidents_write_scope_dep],
)
async def post_runbook_step(
    incident_id: _IncidentIdPath,
    body: RunbookStepRequest,
    request: Request,
    token: ApiToken = _current_token_dep,
) -> dict[str, Any]:
    """Record the step.  Returns the event envelope plus its UUID.

    Validation errors are mapped to the same envelope shape the
    resolve_incident endpoint uses so REST clients can share their error
    handlers.
    """
    workspace_id = _require_workspace(token)
    try:
        event = record_runbook_step(
            app=request.app,
            workspace_id=workspace_id,
            incident_id=incident_id,
            alert_id=body.alert_id,
            runbook_name=body.runbook_name,
            step_id=body.step_id,
            outcome=body.outcome,
            actor=body.actor,
            note=body.note,
            valid_from=body.valid_from,
        )
    except ValueError as exc:
        _raise_from_validation_error(exc)
        raise  # pragma: no cover - the helper always raises HTTPException

    return event.to_payload()


@router.post(
    "/incidents/{incident_id}/suggest-runbook",
    summary="Suggest matching runbooks for an alert (REST mirror of MCP tool)",
    dependencies=[_search_scope_dep],
)
async def post_suggest_runbook(
    incident_id: _IncidentIdPath,
    body: SuggestRunbookRequest,
    request: Request,
    token: ApiToken = _current_token_dep,
) -> dict[str, Any]:
    """REST mirror of ``suggest_runbook``.

    ``incident_id`` is recorded in the response envelope so the responder
    UI can correlate the suggestion with the incident timeline.
    """
    workspace_id = _require_workspace(token)
    try:
        response = await suggest_runbook(
            app=request.app,
            alert_id=body.alert_id,
            workspace_id=workspace_id,
            as_of=body.as_of,
            top_k=body.top_k,
        )
    except ValueError as exc:
        _raise_from_validation_error(exc)
        raise  # pragma: no cover - the helper always raises HTTPException
    response["incident_id"] = incident_id
    return response


@router.get(
    "/incidents/{incident_id}/runbook-steps",
    summary="List recent runbook-step events recorded for any incident",
    dependencies=[_search_scope_dep],
)
async def list_runbook_steps(
    incident_id: _IncidentIdPath,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    token: ApiToken = _current_token_dep,
) -> dict[str, Any]:
    """Read back the workspace's recent runbook-step events.

    Filters the in-memory recorder down to events whose ``incident_id``
    matches the path.  Workspace ACL is enforced inside
    :meth:`RunbookStepRecorder.recent`.
    """
    from omniscience_server.runbook import get_recorder

    workspace_id = _require_workspace(token)
    recorder = get_recorder(request.app)
    events = recorder.recent(workspace_id=workspace_id, limit=limit)
    matching = [e.to_payload() for e in events if e.incident_id == incident_id]
    return {
        "incident_id": incident_id,
        "events": matching,
        "meta": {"returned": len(matching)},
    }


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _raise_from_validation_error(exc: ValueError) -> None:
    """Translate the structured ValueError envelope into an HTTPException."""
    msg = str(exc)
    if msg.startswith(f"{INVALID_ALERT_ID_CODE}:"):
        raise HTTPException(
            status_code=400,
            detail={
                "code": INVALID_ALERT_ID_CODE,
                "message": INVALID_ALERT_ID_MESSAGE,
            },
        ) from exc
    if msg.startswith(f"{INVALID_RUNBOOK_NAME_CODE}:"):
        raise HTTPException(
            status_code=400,
            detail={
                "code": INVALID_RUNBOOK_NAME_CODE,
                "message": INVALID_RUNBOOK_NAME_MESSAGE,
            },
        ) from exc
    if msg.startswith(f"{INVALID_STEP_ID_CODE}:"):
        raise HTTPException(
            status_code=400,
            detail={"code": INVALID_STEP_ID_CODE, "message": msg.split(":", 1)[1]},
        ) from exc
    if msg.startswith(f"{ALERT_NOT_FOUND_CODE}:"):
        raise HTTPException(
            status_code=404,
            detail={
                "code": ALERT_NOT_FOUND_CODE,
                "message": f"Alert '{msg.split(':', 1)[1]}' not found",
            },
        ) from exc
    if msg.startswith("invalid_incident_id:"):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_incident_id", "message": msg.split(":", 1)[1]},
        ) from exc
    if msg == INVALID_TIMEZONE_CODE:
        raise HTTPException(
            status_code=400,
            detail={"code": INVALID_TIMEZONE_CODE, "message": INVALID_TIMEZONE_MESSAGE},
        ) from exc
    raise HTTPException(
        status_code=400,
        detail={"code": "bad_request", "message": msg},
    ) from exc


__all__ = ["router"]
