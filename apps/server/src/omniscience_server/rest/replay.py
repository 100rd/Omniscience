"""REST endpoint for the replay primitive (issue #243).

Two routes:

* ``POST /api/v1/replay`` — replay by ``{tool_name, arguments, at_time}``
  or by ``{audit_log_id}``.  Returns the inner response envelope plus
  the deterministic ``state_fingerprint`` hash.
* ``GET  /api/v1/replay/audit/{audit_log_id}`` — convenience accessor
  for the admin UI's "paste audit_log_id" form.  Equivalent to a POST
  with ``{"audit_log_id": "..."}``.

Both routes require:

* scope ``search`` — replay surfaces the same data the underlying
  tools surface, so the scope is shared.  No new scope is introduced.
* a workspace-scoped token — graph reads are workspace-scoped; replay
  is no exception.  Unscoped tokens are rejected 403 (fail-closed).

The handlers translate ``ValueError`` envelopes from
``omniscience_server.replay`` into structured HTTP error responses:

* ``unknown_replay_tool`` -> 400
* ``invalid_audit_arguments`` -> 422
* ``invalid_timezone`` -> 400  (from :func:`enforce_utc`)
* ``AuditLogNotFoundError`` -> 404
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Request
from omniscience_core.auth.middleware import get_current_token, require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.workspace import get_workspace_id
from omniscience_core.db.models import ApiToken
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import async_sessionmaker

from omniscience_server.as_of import (
    INVALID_TIMEZONE_CODE,
    INVALID_TIMEZONE_MESSAGE,
    enforce_utc,
)
from omniscience_server.replay import (
    INVALID_AT_TIME_CODE,
    INVALID_AUDIT_ARGS_CODE,
    SUPPORTED_REPLAY_TOOLS,
    UNKNOWN_TOOL_CODE,
    AuditLogNotFoundError,
    ReplayQuery,
    envelope_to_dict,
    replay_by_audit_id,
    replay_context,
)

log = structlog.get_logger(__name__)

router = APIRouter(tags=["replay"])

# Module-level Depends singletons — avoids ruff B008.
_search_scope_dep: Any = Depends(require_scope(Scope.search))
_current_token_dep: Any = Depends(get_current_token)


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class ReplayQueryBody(BaseModel):
    """Per-tool dispatch arguments for an inline replay (no audit row)."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(
        ...,
        description=(f"MCP tool name to replay.  One of {', '.join(SUPPORTED_REPLAY_TOOLS)}."),
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Wire arguments the tool was originally invoked with "
            "(workspace_id and as_of are supplied separately and "
            "ignored if present)."
        ),
    )

    @field_validator("tool_name")
    @classmethod
    def _validate_tool(cls, value: str) -> str:
        if value not in SUPPORTED_REPLAY_TOOLS:
            raise ValueError(f"{UNKNOWN_TOOL_CODE}:{value}")
        return value


class ReplayRequestBody(BaseModel):
    """POST /api/v1/replay request body.

    Either ``audit_log_id`` (replay by stored audit row) or
    ``(at_time, query)`` (inline replay) must be supplied.  Supplying
    both is rejected at validation time to avoid an ambiguous request.
    """

    model_config = ConfigDict(extra="forbid")

    audit_log_id: uuid.UUID | None = Field(
        default=None,
        description="Audit-log row id to replay.  Mutually exclusive with at_time/query.",
    )
    at_time: datetime | None = Field(
        default=None,
        description=(
            "ISO-8601 timezone-aware UTC datetime.  Required when "
            "audit_log_id is absent.  Naive datetimes are rejected "
            "with 400 invalid_timezone."
        ),
    )
    query: ReplayQueryBody | None = Field(
        default=None,
        description="Inline query.  Required when audit_log_id is absent.",
    )

    @field_validator("at_time")
    @classmethod
    def _enforce_utc(cls, value: datetime | None) -> datetime | None:
        try:
            return enforce_utc(value)
        except ValueError as exc:
            # Re-raise the structured code so FastAPI surfaces the
            # invalid_timezone payload our error handler expects.
            raise ValueError(INVALID_TIMEZONE_CODE) from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_workspace_or_403(token: ApiToken) -> uuid.UUID:
    """Return the workspace id or raise 403."""
    workspace_id = get_workspace_id(token)
    if workspace_id is None:
        log.warning(
            "replay_request_rejected_no_workspace",
            token_prefix=token.token_prefix,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": "Replay requires a workspace-scoped token",
            },
        )
    return workspace_id


def _session_factory_or_503(request: Request) -> async_sessionmaker[Any]:
    """Return the app's session factory or raise 503."""
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        log.warning("replay_db_session_factory_missing")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "service_unavailable",
                "message": "Database session factory not available",
            },
        )
    return factory  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/replay",
    summary="Replay a tool invocation at a point in time",
    dependencies=[_search_scope_dep],
)
async def post_replay(
    body: ReplayRequestBody,
    request: Request,
    token: ApiToken = _current_token_dep,
) -> dict[str, Any]:
    """Replay a tool call against the bitemporal graph at ``at_time``.

    Two modes:

    * ``audit_log_id`` — load arguments + T from the audit row.
    * ``at_time + query`` — inline; the caller specifies what to replay.

    Returns the standard replay envelope (see
    :func:`omniscience_server.replay.envelope_to_dict`).
    """
    workspace_id = _resolve_workspace_or_403(token)
    factory = _session_factory_or_503(request)

    if body.audit_log_id is not None and (body.at_time is not None or body.query is not None):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "bad_request",
                "message": "Supply either audit_log_id OR (at_time + query), not both",
            },
        )
    if body.audit_log_id is None and (body.at_time is None or body.query is None):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "bad_request",
                "message": "Must supply audit_log_id OR (at_time AND query)",
            },
        )

    try:
        if body.audit_log_id is not None:
            async with factory() as session:
                result = await replay_by_audit_id(
                    app=request.app,
                    session=session,
                    audit_log_id=body.audit_log_id,
                    workspace_id=workspace_id,
                )
        else:
            # Validated above — both fields are present.
            assert body.at_time is not None  # noqa: S101 — narrows type for mypy
            assert body.query is not None  # noqa: S101
            result = await replay_context(
                app=request.app,
                workspace_id=workspace_id,
                at_time=body.at_time,
                query=ReplayQuery(
                    tool_name=body.query.tool_name,
                    arguments=body.query.arguments,
                ),
            )
    except AuditLogNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "audit_log_not_found",
                "message": f"Audit log entry {exc} not found in this workspace",
            },
        ) from exc
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith(f"{UNKNOWN_TOOL_CODE}:"):
            raise HTTPException(
                status_code=400,
                detail={"code": UNKNOWN_TOOL_CODE, "message": msg},
            ) from exc
        if msg.startswith(f"{INVALID_AUDIT_ARGS_CODE}:"):
            raise HTTPException(
                status_code=422,
                detail={"code": INVALID_AUDIT_ARGS_CODE, "message": msg},
            ) from exc
        if msg.startswith(f"{INVALID_AT_TIME_CODE}:") or msg == INVALID_TIMEZONE_CODE:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": INVALID_TIMEZONE_CODE,
                    "message": INVALID_TIMEZONE_MESSAGE,
                },
            ) from exc
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_request", "message": msg},
        ) from exc

    return envelope_to_dict(result)


_AuditLogIdPath = Annotated[
    uuid.UUID,
    Path(description="The audit_log_id surfaced when the original tool was invoked."),
]


@router.get(
    "/replay/audit/{audit_log_id}",
    summary="Replay a tool invocation by audit_log_id",
    dependencies=[_search_scope_dep],
)
async def get_replay_by_audit_id(
    audit_log_id: _AuditLogIdPath,
    request: Request,
    token: ApiToken = _current_token_dep,
) -> dict[str, Any]:
    """Convenience accessor: replay the call captured in ``audit_log_id``.

    Equivalent to ``POST /api/v1/replay`` with body
    ``{"audit_log_id": "..."}``.  Provided so the admin UI does not
    need to construct a JSON body for the simplest case.
    """
    workspace_id = _resolve_workspace_or_403(token)
    factory = _session_factory_or_503(request)

    try:
        async with factory() as session:
            result = await replay_by_audit_id(
                app=request.app,
                session=session,
                audit_log_id=audit_log_id,
                workspace_id=workspace_id,
            )
    except AuditLogNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "audit_log_not_found",
                "message": f"Audit log entry {exc} not found in this workspace",
            },
        ) from exc

    return envelope_to_dict(result)


__all__ = ["router"]
