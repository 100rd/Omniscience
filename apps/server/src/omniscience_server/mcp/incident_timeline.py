"""MCP tool: ``incident_timeline`` (issue #235).

Same shape as the REST surface ``GET /api/v1/incidents/{id}/timeline``.
Registered in ``omniscience_server.mcp.server`` alongside the other
incident tools.

Wire signature
--------------

``incident_timeline(alert_id, from_ts=None, to_ts=None, entity_types=None,
as_of=None, max_depth=2)`` -> ``dict`` matching
:class:`~omniscience_server.incident_timeline.IncidentTimelineResponse`.

ACL & auth
----------

- Scope: ``search`` (same as ``resolve_incident``).
- Workspace-scoped token REQUIRED; unscoped tokens are rejected with
  ``forbidden``.  Cross-workspace alert ids return ``alert_not_found``
  (no existence leak — #117 / ADR-0005).
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI
from mcp.server.fastmcp import Context
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.workspace import get_workspace_id

from omniscience_server.incident_timeline import (
    TIMELINE_MAX_DEPTH,
    mcp_incident_timeline,
)

log = structlog.get_logger(__name__)


async def incident_timeline_tool(
    *,
    app: FastAPI,
    ctx: Context[Any, Any, Any],
    resolve_token: Any,  # async callable -> ApiToken | None
    require_scope: Any,  # callable(token, scope) -> None
    parse_as_of: Any,  # callable(str | None) -> datetime | None
    record_invocation: Any,  # callable(*, tool_name, token) -> None
    alert_id: str,
    from_ts: str | None,
    to_ts: str | None,
    entity_types: list[str] | None,
    as_of: str | None,
    max_depth: int,
) -> dict[str, Any]:
    """Inner implementation invoked by the FastMCP-registered shim.

    The function is parameterised over the auth / parsing helpers
    (``resolve_token``, ``require_scope``, ``parse_as_of``,
    ``record_invocation``) so this module can be unit-tested without
    pulling in the FastMCP server-module globals.  The shim in
    :mod:`omniscience_server.mcp.server` wires the real implementations.
    """
    token = await resolve_token(ctx)
    require_scope(token, Scope.search)
    record_invocation(tool_name="incident_timeline", token=token)
    assert token is not None  # noqa: S101 — narrows type for mypy

    workspace_id = get_workspace_id(token)
    if workspace_id is None:
        log.warning(
            "mcp_incident_timeline_rejected_no_workspace",
            token_prefix=token.token_prefix,
        )
        raise ValueError("forbidden:Incident timeline requires a workspace-scoped token")

    parsed_as_of = parse_as_of(as_of)
    parsed_from = parse_as_of(from_ts)
    parsed_to = parse_as_of(to_ts)
    clamped_depth = max(1, min(max_depth, 5))

    return await mcp_incident_timeline(
        app=app,
        alert_id=alert_id,
        workspace_id=workspace_id,
        as_of=parsed_as_of,
        from_ts=parsed_from,
        to_ts=parsed_to,
        entity_types=entity_types,
        max_depth=clamped_depth,
    )


__all__ = ["TIMELINE_MAX_DEPTH", "incident_timeline_tool"]
