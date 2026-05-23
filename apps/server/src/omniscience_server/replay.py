"""Replay primitive: "what did the agent see at time T" (issue #243).

This module hosts the surface-neutral business logic for the replay
endpoint.  Two entry points:

* :func:`replay_context` — given ``(workspace_id, at_time, query)``,
  re-dispatch the original tool against the bitemporal graph at T,
  compute the deterministic ``state_fingerprint`` and return the
  envelope the agent would have seen.
* :func:`replay_by_audit_id` — convenience wrapper that loads the
  original ``AuditLog`` row and forwards its arguments to
  :func:`replay_context`.  Used by the admin UI's "paste audit_log_id"
  flow.

ADP rationale — see ``docs/api/replay.md``.

ACL invariant
-------------

Every call requires ``workspace_id``.  The audit-log read in
:func:`replay_by_audit_id` is workspace-scoped via
:func:`omniscience_core.audit.service.get_audit_entry`; the
underlying dispatch is workspace-scoped by the same protocol the
original tool used.  Cross-workspace replay is structurally
impossible.

Determinism contract
--------------------

Two replays at the same ``at_time`` against the same indexed graph
state MUST produce the same ``state_fingerprint``.  The function
delegates to the existing ``GraphStore.find_related`` / ``get_entity``
/ ``mcp_resolve_incident`` paths which already implement the
ADR-0008 §5 bitemporal predicate.  The fingerprint is computed by
:func:`omniscience_core.audit.fingerprint.compute_state_fingerprint`
over the **inner_response** (the original-shape payload) so that two
calls at different wall-clocks but identical T produce identical
hashes (the outer envelope's ``replayed_at`` is excluded).

Drift detection
---------------

The envelope carries both ``state_fingerprint`` (freshly computed)
and ``original_state_fingerprint`` (loaded from the audit-log row,
``None`` when called outside the audit-id flow).  The handler
surfaces ``fingerprint_match: bool`` so the admin UI can highlight
divergence without re-comparing the JSON.  Divergence at the same T
means a write landed with a ``recorded_at`` earlier than T but
*after* the original tool call — i.e. a retroactive correction.
That is a legitimate ADR-0008 §2 scenario; the UI marks it but
does not error.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from fastapi import FastAPI
from omniscience_core.audit import (
    FINGERPRINT_ALGORITHM,
    AuditLogNotFoundError,
    compute_state_fingerprint,
    get_audit_entry,
)
from sqlalchemy.ext.asyncio import AsyncSession

from omniscience_server.as_of import enforce_utc
from omniscience_server.incidents import mcp_resolve_incident
from omniscience_server.mcp.tools import (
    mcp_get_entity,
    mcp_get_related_entities,
    mcp_search,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Error codes — wired through to the MCP / REST surfaces
# ---------------------------------------------------------------------------

#: Caller supplied an unknown ``tool_name``.  The replay surface is a
#: tight allowlist — we never dispatch arbitrary callables.
UNKNOWN_TOOL_CODE = "unknown_replay_tool"

#: The audit row's stored arguments are not a JSON object.  Should be
#: structurally impossible (the audit-log writer always passes a dict),
#: but defended explicitly so a hand-corrupted row surfaces a clear
#: error rather than a TypeError deep in the dispatch.
INVALID_AUDIT_ARGS_CODE = "invalid_audit_arguments"

#: The replay payload included an ``at_time`` field that could not be
#: parsed.  Distinct from ``invalid_timezone`` so the admin UI can
#: tell the auditor "you typed a bad string" vs "the value is naive".
INVALID_AT_TIME_CODE = "invalid_at_time"

#: Allowlist of tool names accepted by :func:`replay_context`.  The
#: order is the order surfaced in the OpenAPI enum.
SUPPORTED_REPLAY_TOOLS: tuple[str, ...] = (
    "search",
    "get_entity",
    "get_related_entities",
    "resolve_incident",
)


@dataclass(slots=True)
class ReplayQuery:
    """Normalised replay request.

    ``tool_name`` MUST be a member of :data:`SUPPORTED_REPLAY_TOOLS`.
    ``arguments`` is the per-tool kwarg dict the original tool was
    invoked with (excluding ``workspace_id`` and ``as_of`` — those
    are supplied separately by the replay handler so the bitemporal
    anchor cannot drift between the audit row and the dispatch).
    """

    tool_name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ReplayResult:
    """Envelope returned by :func:`replay_context`.

    * ``response`` — the inner payload re-running the tool at
      ``at_time`` produced.  Same shape the original caller saw.
    * ``state_fingerprint`` — deterministic hash over ``response``.
    * ``original_state_fingerprint`` — hash from the audit-log row,
      ``None`` when the caller invoked the surface directly (not via
      ``audit_log_id``).
    * ``fingerprint_match`` — convenience boolean; ``None`` when
      ``original_state_fingerprint`` is unavailable.
    * ``at_time`` — the bitemporal anchor that was used.
    * ``tool_name`` — echoed for the UI / clients.
    * ``audit_log_id`` — present iff this was an audit-id replay.
    """

    response: dict[str, Any]
    state_fingerprint: str
    fingerprint_algorithm: str
    original_state_fingerprint: str | None
    fingerprint_match: bool | None
    at_time: datetime
    tool_name: str
    audit_log_id: uuid.UUID | None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def _dispatch_tool(
    *,
    app: FastAPI,
    tool_name: str,
    workspace_id: uuid.UUID,
    at_time: datetime,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Re-execute the original tool against the bitemporal graph at T.

    Defensive about which kwargs each tool accepts — callers should
    have stripped ``workspace_id``/``as_of`` before storage but we
    pop them defensively to avoid a duplicate-kwarg ``TypeError``
    when a hand-edited audit row sneaks them back in.
    """
    args = dict(arguments)  # defensive copy — never mutate caller's dict
    args.pop("workspace_id", None)
    args.pop("as_of", None)

    if tool_name == "search":
        return await mcp_search(
            app=app,
            workspace_id=workspace_id,
            as_of=at_time,
            **args,
        )
    if tool_name == "get_entity":
        return await mcp_get_entity(
            app=app,
            workspace_id=workspace_id,
            as_of=at_time,
            **args,
        )
    if tool_name == "get_related_entities":
        return await mcp_get_related_entities(
            app=app,
            workspace_id=workspace_id,
            as_of=at_time,
            **args,
        )
    if tool_name == "resolve_incident":
        return await mcp_resolve_incident(
            app=app,
            workspace_id=workspace_id,
            as_of=at_time,
            **args,
        )
    raise ValueError(f"{UNKNOWN_TOOL_CODE}:{tool_name}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def replay_context(
    *,
    app: FastAPI,
    workspace_id: uuid.UUID,
    at_time: datetime,
    query: ReplayQuery,
    original_state_fingerprint: str | None = None,
    audit_log_id: uuid.UUID | None = None,
) -> ReplayResult:
    """Re-run ``query`` against the bitemporal graph at ``at_time``.

    The dispatch goes through the existing per-tool ``mcp_*``
    handlers so the response shape is byte-identical to what the
    original tool produced — which is exactly the invariant the
    ``state_fingerprint`` binds.

    ``at_time`` is validated by :func:`enforce_utc` for parity with
    the rest of the bitemporal surface; passing a naive datetime
    raises ``ValueError`` carrying the standard ``invalid_timezone``
    code so the caller (MCP or REST) can map it to a structured
    error envelope.
    """
    normalised = enforce_utc(at_time)
    if normalised is None:  # pragma: no cover — enforce_utc only returns None for None input
        raise ValueError(f"{INVALID_AT_TIME_CODE}:at_time is required")

    if query.tool_name not in SUPPORTED_REPLAY_TOOLS:
        raise ValueError(f"{UNKNOWN_TOOL_CODE}:{query.tool_name}")

    response = await _dispatch_tool(
        app=app,
        tool_name=query.tool_name,
        workspace_id=workspace_id,
        at_time=normalised,
        arguments=query.arguments,
    )

    fingerprint = compute_state_fingerprint(response)
    match: bool | None = None
    if original_state_fingerprint is not None:
        match = fingerprint == original_state_fingerprint

    log.info(
        "replay_context_executed",
        workspace_id=str(workspace_id),
        tool_name=query.tool_name,
        at_time=normalised.isoformat(),
        state_fingerprint=fingerprint,
        fingerprint_match=match,
        audit_log_id=str(audit_log_id) if audit_log_id else None,
    )

    return ReplayResult(
        response=response,
        state_fingerprint=fingerprint,
        fingerprint_algorithm=FINGERPRINT_ALGORITHM,
        original_state_fingerprint=original_state_fingerprint,
        fingerprint_match=match,
        at_time=normalised,
        tool_name=query.tool_name,
        audit_log_id=audit_log_id,
    )


async def replay_by_audit_id(
    *,
    app: FastAPI,
    session: AsyncSession,
    audit_log_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> ReplayResult:
    """Replay the tool invocation captured in audit row ``audit_log_id``.

    The audit row's ``as_of`` is used as ``at_time`` when populated;
    otherwise we fall back to ``recorded_at`` — that captures the
    "still-current at the moment of the original call" intent.

    Raises
    ------
    AuditLogNotFoundError
        When the row does not exist OR belongs to a different
        workspace (existence is never leaked).
    """
    row = await get_audit_entry(
        session,
        audit_log_id=audit_log_id,
        workspace_id=workspace_id,
    )
    arguments = row.arguments if isinstance(row.arguments, dict) else None
    if arguments is None:
        raise ValueError(f"{INVALID_AUDIT_ARGS_CODE}:{audit_log_id}")

    at_time = row.as_of if row.as_of is not None else row.recorded_at
    return await replay_context(
        app=app,
        workspace_id=workspace_id,
        at_time=at_time,
        query=ReplayQuery(tool_name=row.tool_name, arguments=arguments),
        original_state_fingerprint=row.state_fingerprint,
        audit_log_id=row.id,
    )


def envelope_to_dict(result: ReplayResult) -> dict[str, Any]:
    """Serialise a :class:`ReplayResult` to the wire JSON envelope.

    Shared between the MCP and REST surfaces so they emit identical
    shapes — important for the admin UI which talks to REST but the
    contract tests that talk to MCP.
    """
    return {
        "tool_name": result.tool_name,
        "at_time": result.at_time.isoformat(),
        "state_fingerprint": result.state_fingerprint,
        "fingerprint_algorithm": result.fingerprint_algorithm,
        "original_state_fingerprint": result.original_state_fingerprint,
        "fingerprint_match": result.fingerprint_match,
        "audit_log_id": str(result.audit_log_id) if result.audit_log_id else None,
        "response": result.response,
    }


__all__ = [
    "INVALID_AT_TIME_CODE",
    "INVALID_AUDIT_ARGS_CODE",
    "SUPPORTED_REPLAY_TOOLS",
    "UNKNOWN_TOOL_CODE",
    "AuditLogNotFoundError",
    "ReplayQuery",
    "ReplayResult",
    "envelope_to_dict",
    "replay_by_audit_id",
    "replay_context",
]
