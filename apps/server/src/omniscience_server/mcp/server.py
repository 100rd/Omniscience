"""FastMCP server setup for Omniscience.

Registers seven tools:
- search              (requires scope: search)
- get_document        (requires scope: search)
- get_entity          (requires scope: search + workspace-scoped token)
- get_related_entities (requires scope: search + workspace-scoped token)
- list_entities       (requires scope: search + workspace-scoped token)
- list_sources        (requires scope: sources:read)
- source_stats        (requires scope: sources:read)
- resolve_incident    (requires scope: search + workspace-scoped token)
- blast_radius        (requires scope: search + workspace-scoped token)

Auth:
- HTTP transport: Bearer token from Authorization header
- stdio transport: OMNISCIENCE_TOKEN environment variable

The FastMCP instance is module-level.  Before mounting, call
``set_fastapi_app(app)`` so tool handlers can reach the FastAPI
app.state (db_session_factory, retrieval_service).

Bitemporal reads (issue #133)
-----------------------------

``search``, ``get_entity``, ``get_related_entities``, ``resolve_incident``
and ``blast_radius`` accept an optional ``as_of`` parameter — an
ISO-8601 timezone-aware UTC datetime string (``Z`` suffix or ``+00:00``).
Naive datetimes are rejected with the structured error code
``invalid_timezone``.  See ADR-0008 §5 for the bitemporal predicate
semantics.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import FastAPI
from mcp.server.fastmcp import Context, FastMCP
from omniscience_core.auth.middleware import _lookup_token
from omniscience_core.auth.scopes import Scope, check_scopes
from omniscience_core.auth.workspace import get_workspace_id
from omniscience_core.db.models import ApiToken
from omniscience_core.telemetry.clients import (
    ANONYMOUS_TOKEN_ID,
    get_client_registry,
)

from omniscience_server.as_of import (
    INVALID_TIMEZONE_CODE,
    INVALID_TIMEZONE_MESSAGE,
)
from omniscience_server.blast_radius import (
    ACTION_TYPES,
    INVALID_ACTION_TYPE_CODE,
    INVALID_ENTITY_ID_CODE,
    ActionType,
    mcp_blast_radius,
)
from omniscience_server.blast_radius import (
    DEFAULT_MAX_DEPTH as BLAST_DEFAULT_MAX_DEPTH,
)
from omniscience_server.blast_radius import (
    ENTITY_NOT_FOUND_CODE as BLAST_ENTITY_NOT_FOUND_CODE,
)
from omniscience_server.blast_radius import (
    MAX_MAX_DEPTH as BLAST_MAX_MAX_DEPTH,
)
from omniscience_server.blast_radius import (
    MIN_MAX_DEPTH as BLAST_MIN_MAX_DEPTH,
)
from omniscience_server.incidents import (
    ALERT_NOT_FOUND_CODE,
    DEFAULT_MAX_DEPTH,
    INVALID_ALERT_ID_CODE,
    MAX_MAX_DEPTH,
    MIN_MAX_DEPTH,
)
from omniscience_server.mcp.apps import (
    wrap_resolve_incident_response,
)
from omniscience_server.mcp.incident_timeline import (
    TIMELINE_MAX_DEPTH,
    incident_timeline_tool,
)
from omniscience_server.mcp.tools import (
    mcp_get_document,
    mcp_get_entity,
    mcp_get_related_entities,
    mcp_list_entities,
    mcp_list_sources,
    mcp_resolve_incident,
    mcp_search,
    mcp_source_stats,
)
from omniscience_server.replay import (
    SUPPORTED_REPLAY_TOOLS,
    AuditLogNotFoundError,
    ReplayQuery,
    envelope_to_dict,
    replay_by_audit_id,
    replay_context,
)
from omniscience_server.runbook import (
    ALERT_NOT_FOUND_CODE as RUNBOOK_ALERT_NOT_FOUND_CODE,
)
from omniscience_server.runbook import (
    DEFAULT_TOP_K as RUNBOOK_DEFAULT_TOP_K,
)
from omniscience_server.runbook import (
    INVALID_ALERT_ID_CODE as RUNBOOK_INVALID_ALERT_ID_CODE,
)
from omniscience_server.runbook import (
    MAX_TOP_K as RUNBOOK_MAX_TOP_K,
)
from omniscience_server.runbook import (
    suggest_runbook as mcp_suggest_runbook,
)
from omniscience_server.similar_incidents import (
    MAX_LIMIT as SIMILAR_MAX_LIMIT,
)
from omniscience_server.similar_incidents import (
    MAX_SINCE_DAYS as SIMILAR_MAX_SINCE_DAYS,
)
from omniscience_server.similar_incidents import (
    mcp_find_similar_incidents,
)

log = structlog.get_logger(__name__)

mcp_server: FastMCP[None] = FastMCP("omniscience")

# FastAPI app reference — set via set_fastapi_app() before first request
_fastapi_app: FastAPI | None = None

#: Global budget in characters for limited MCP tools (approx 20,000 tokens).
MCP_GLOBAL_BUDGET_CHARS = 80000


def _apply_mcp_budget(result: dict[str, Any]) -> dict[str, Any]:
    """Ensure the JSON serialized size of the result does not exceed the global MCP budget.
    If it exceeds, truncate and add a truncation warning."""
    import json

    s = json.dumps(result)
    if len(s) <= MCP_GLOBAL_BUDGET_CHARS:
        return result
    return {
        "error": "budget_exceeded",
        "message": (
            "The tool output exceeded the global MCP character budget."
            " Please refine your query or use depth limits."
        ),
        "truncated": True,
    }


def set_fastapi_app(app: FastAPI) -> None:
    """Store a reference to the FastAPI app for use in tool handlers."""
    global _fastapi_app
    _fastapi_app = app


def _get_app() -> FastAPI:
    if _fastapi_app is None:
        raise RuntimeError("FastAPI app not configured — call set_fastapi_app() first")
    return _fastapi_app


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _extract_bearer(raw_request: Any) -> str | None:
    """Return the bearer token string from an HTTP request, or None."""
    if raw_request is None:
        return None
    headers = getattr(raw_request, "headers", None)
    if headers is None:
        return None
    auth_header: str = headers.get("authorization", "") or ""
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:]
    return None


async def _resolve_token(ctx: Context[Any, Any, Any]) -> ApiToken | None:
    """Extract and verify the bearer token from the current request context."""
    raw_request = ctx._request_context.request if ctx._request_context else None
    plaintext = _extract_bearer(raw_request)

    if plaintext is None:
        # stdio transport: fall back to environment variable
        plaintext = os.environ.get("OMNISCIENCE_TOKEN")

    if not plaintext:
        return None

    app = _get_app()
    factory = getattr(app.state, "db_session_factory", None)
    if factory is None:
        return None

    async with factory() as session:
        return await _lookup_token(session, plaintext)


def _require_scope(token: ApiToken | None, scope: Scope) -> None:
    """Raise ValueError with MCP error code if token lacks the required scope."""
    if token is None:
        raise ValueError("unauthorized:Token missing or invalid")
    granted = {Scope(s) for s in token.scopes if s in Scope.__members__.values()}
    if not check_scopes({scope}, granted):
        raise ValueError(f"forbidden:Token lacks required scope '{scope}'")


def _require_workspace(
    token: ApiToken,
    *,
    log_event: str,
    forbidden_message: str,
) -> uuid.UUID:
    """Return the token's workspace UUID or raise the MCP ``forbidden:`` error.

    ``get_workspace_id`` is fail-closed and raises :class:`PermissionError`
    for a token with no ``workspace_id`` (see
    ``omniscience_core.auth.workspace``).  The MCP error envelope surfaces
    rejections as :class:`ValueError` with a ``forbidden:`` code prefix, so
    this helper translates the ``PermissionError`` into that contract while
    emitting the per-tool structured warning.
    """
    try:
        return get_workspace_id(token)
    except PermissionError as exc:
        log.warning(log_event, token_prefix=token.token_prefix)
        raise ValueError(f"forbidden:{forbidden_message}") from exc


def _parse_as_of(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 ``as_of`` string from the MCP wire format.

    Issue #133: ``as_of`` is exposed as a string parameter (the FastMCP
    JSON-Schema for tool inputs lacks a native datetime type) so this
    helper does the wire->datetime conversion.  Validation rules:

    - ``None`` / empty -> ``None`` (still-current read).
    - ``Z`` suffix is accepted as a synonym for ``+00:00``.
    - Naive datetimes and unparseable strings raise ``ValueError`` with
      the structured ``invalid_timezone:<message>`` error envelope so
      the FastMCP transport surfaces it as an MCP error.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):  # pragma: no cover - SDK robustness
        if raw.tzinfo is None:
            raise ValueError(f"{INVALID_TIMEZONE_CODE}:{INVALID_TIMEZONE_MESSAGE}")
        return raw
    if not raw:
        return None
    # ``fromisoformat`` accepts the Python 3.11+ ``Z`` suffix natively.
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{INVALID_TIMEZONE_CODE}:{INVALID_TIMEZONE_MESSAGE}") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError(f"{INVALID_TIMEZONE_CODE}:{INVALID_TIMEZONE_MESSAGE}")
    return parsed


def _record_tool_invocation(*, tool_name: str, token: ApiToken | None) -> None:
    """Record a single MCP tools/call invocation in the telemetry registry.

    Called from each tool handler *after* the scope check passes, so
    only authorised invocations count.  Failures inside the registry
    are swallowed — telemetry must never take down a real MCP request.
    """
    token_id = str(token.id) if token is not None else ANONYMOUS_TOKEN_ID
    try:
        get_client_registry().record_tool_invocation(
            tool_name=tool_name,
            token_id=token_id,
        )
    except Exception as exc:  # pragma: no cover - defensive only
        log.warning("mcp_tool_invocation_record_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Tool: search
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="search",
    description=(
        "Hybrid vector + BM25 retrieval over the Omniscience index. "
        "Returns ranked chunks with citations and lineage. "
        "Optional `as_of` (ISO-8601 UTC datetime) anchors the result "
        "to the graph state at that time per ADR-0008 §5."
    ),
)
async def search(
    query: str,
    ctx: Context[Any, Any, Any],
    top_k: int = 10,
    sources: list[str] | None = None,
    types: list[str] | None = None,
    max_age_seconds: int | None = None,
    filters: dict[str, Any] | None = None,
    include_tombstoned: bool = False,
    retrieval_strategy: str = "hybrid",
    as_of: str | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Search tool — requires scope 'search'.

    When the calling token is workspace-scoped the ``workspace_id`` is
    forwarded to :func:`mcp_search` so the :class:`GraphRAGComposer`
    (issue #107) can enforce its ACL invariant and, when the backend
    stack permits it, run the GraphRAG composed pipeline.  Tokens
    without a workspace still get legacy-path results.

    The optional ``as_of`` parameter (issue #133) is an ISO-8601
    timezone-aware UTC datetime; non-UTC and naive values are rejected
    with the structured ``invalid_timezone`` error code.
    """
    token = await _resolve_token(ctx)
    _require_scope(token, Scope.search)
    _record_tool_invocation(tool_name="search", token=token)
    workspace_id = get_workspace_id(token) if token is not None else None
    parsed_as_of = _parse_as_of(as_of)

    return _apply_mcp_budget(
        await mcp_search(
            app=_get_app(),
            query=query,
            top_k=top_k,
            sources=sources,
            types=types,
            max_age_seconds=max_age_seconds,
            filters=filters,
            include_tombstoned=include_tombstoned,
            retrieval_strategy=retrieval_strategy,
            workspace_id=workspace_id,
            as_of=parsed_as_of,
            cursor=cursor,
        )
    )


# ---------------------------------------------------------------------------
# Tool: get_document
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="get_document",
    description="Retrieve a full document and all its chunks by document id.",
)
async def get_document(
    document_id: str,
    ctx: Context[Any, Any, Any],
) -> dict[str, Any]:
    """get_document tool — requires scope 'search'."""
    token = await _resolve_token(ctx)
    _require_scope(token, Scope.search)
    _record_tool_invocation(tool_name="get_document", token=token)

    workspace_id = get_workspace_id(token) if token is not None else None
    try:
        return await mcp_get_document(
            app=_get_app(), document_id=document_id, workspace_id=workspace_id
        )
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith("document_not_found:"):
            raise ValueError(f"source_not_found:{document_id}") from exc
        raise


# ---------------------------------------------------------------------------
# Tool: get_related_entities
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="get_related_entities",
    description=(
        "Traverse the entity graph from a named entity. "
        "Returns the seed entity, related entities up to max_depth hops, "
        "and the edges that connect them. Each entity includes chunk text for context. "
        "Requires a workspace-scoped token. "
        "Optional `as_of` (ISO-8601 UTC datetime) anchors the traversal "
        "to the graph state at that time per ADR-0008 §5."
    ),
)
async def get_related_entities(
    entity_name: str,
    ctx: Context[Any, Any, Any],
    max_depth: int = 1,
    edge_types: list[str] | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """get_related_entities tool — requires scope 'search' AND a workspace-scoped token.

    The graph is workspace-scoped.  A token that is not bound to any
    workspace cannot silently traverse the global graph (issue #117);
    it is rejected with a ``forbidden:`` error.

    ``as_of`` (issue #133) anchors the traversal to ADR-0008 §5's
    bitemporal predicate.  See module docstring for the format and
    error envelope.
    """
    token = await _resolve_token(ctx)
    _require_scope(token, Scope.search)
    _record_tool_invocation(tool_name="get_related_entities", token=token)
    # _require_scope raised if token is None, so it is non-None here.
    assert token is not None  # noqa: S101 — narrows type for mypy

    workspace_id = _require_workspace(
        token,
        log_event="mcp_graph_request_rejected_no_workspace",
        forbidden_message="Graph retrieval requires a workspace-scoped token",
    )

    parsed_as_of = _parse_as_of(as_of)
    try:
        return await mcp_get_related_entities(
            app=_get_app(),
            entity_name=entity_name,
            workspace_id=workspace_id,
            max_depth=max_depth,
            edge_types=edge_types,
            as_of=parsed_as_of,
        )
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith("entity_not_found:"):
            raise
        raise


# ---------------------------------------------------------------------------
# Tool: get_entity (issue #133)
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="get_entity",
    description=(
        "Resolve a single entity by name within the caller's workspace. "
        "Optional `as_of` (ISO-8601 UTC datetime) returns the entity "
        "as it was at that time per ADR-0008 §5; default is the still-current "
        "state. Requires a workspace-scoped token."
    ),
)
async def get_entity(
    entity_name: str,
    ctx: Context[Any, Any, Any],
    as_of: str | None = None,
) -> dict[str, Any]:
    """get_entity tool — requires scope 'search' AND a workspace-scoped token.

    Mirrors the auth posture of :func:`get_related_entities` — graph
    reads are workspace-scoped, fail-closed, and reject unscoped
    tokens.  ``as_of`` is optional; when set it is parsed per the
    issue #133 contract and forwarded to ``GraphStore.get_entity``.
    """
    token = await _resolve_token(ctx)
    _require_scope(token, Scope.search)
    _record_tool_invocation(tool_name="get_entity", token=token)
    assert token is not None  # noqa: S101 — narrows type for mypy

    workspace_id = _require_workspace(
        token,
        log_event="mcp_get_entity_rejected_no_workspace",
        forbidden_message="Graph retrieval requires a workspace-scoped token",
    )

    parsed_as_of = _parse_as_of(as_of)
    return await mcp_get_entity(
        app=_get_app(),
        entity_name=entity_name,
        workspace_id=workspace_id,
        as_of=parsed_as_of,
    )


# ---------------------------------------------------------------------------
# Tool: list_entities
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="list_entities",
    description=(
        "List entities of a given kind within the caller's workspace, "
        "optionally filtered by the indexed `cluster` property and/or an "
        "exact `name`. Answers deterministic platform-state questions such "
        "as 'which StorageClasses exist in cluster X' or 'does StorageClass "
        '"gold" exist in cluster X\'. Returns a list of entities (same '
        "per-entity shape as get_entity). Optional `as_of` (ISO-8601 UTC "
        "datetime) returns the state at that time per ADR-0008 §5; default "
        "is the still-current state. Requires a workspace-scoped token."
    ),
)
async def list_entities(
    kind: str,
    ctx: Context[Any, Any, Any],
    cluster: str | None = None,
    name: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """list_entities tool — requires scope 'search' AND a workspace-scoped token.

    Mirrors the auth posture of :func:`get_entity` — graph reads are
    workspace-scoped, fail-closed, and reject unscoped tokens.  Delegates
    to ``mcp_list_entities`` -> ``GraphStore.list_entities`` (committed in
    f8240b7).  ``as_of`` is optional; when set it is parsed per the
    ADR-0008 §5 contract and forwarded to the bitemporal store query.
    """
    token = await _resolve_token(ctx)
    _require_scope(token, Scope.search)
    _record_tool_invocation(tool_name="list_entities", token=token)
    assert token is not None  # noqa: S101 — narrows type for mypy

    workspace_id = _require_workspace(
        token,
        log_event="mcp_list_entities_rejected_no_workspace",
        forbidden_message="Graph retrieval requires a workspace-scoped token",
    )

    parsed_as_of = _parse_as_of(as_of)
    return await mcp_list_entities(
        app=_get_app(),
        workspace_id=workspace_id,
        kind=kind,
        cluster=cluster,
        name=name,
        as_of=parsed_as_of,
    )


# ---------------------------------------------------------------------------
# Tool: list_sources
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="list_sources",
    description="List configured sources with freshness information.",
)
async def list_sources(
    ctx: Context[Any, Any, Any],
) -> dict[str, Any]:
    """list_sources tool — requires scope 'sources:read'."""
    token = await _resolve_token(ctx)
    _require_scope(token, Scope.sources_read)
    _record_tool_invocation(tool_name="list_sources", token=token)

    workspace_id = get_workspace_id(token) if token is not None else None
    return _apply_mcp_budget(await mcp_list_sources(app=_get_app(), workspace_id=workspace_id))


# ---------------------------------------------------------------------------
# Tool: source_stats
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="source_stats",
    description="Return detailed statistics for a single source by id.",
)
async def source_stats(
    source_id: str,
    ctx: Context[Any, Any, Any],
) -> dict[str, Any]:
    """source_stats tool — requires scope 'sources:read'."""
    token = await _resolve_token(ctx)
    _require_scope(token, Scope.sources_read)
    _record_tool_invocation(tool_name="source_stats", token=token)

    workspace_id = get_workspace_id(token) if token is not None else None
    try:
        return _apply_mcp_budget(
            await mcp_source_stats(app=_get_app(), source_id=source_id, workspace_id=workspace_id)
        )
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith("source_not_found:"):
            raise
        raise


# ---------------------------------------------------------------------------
# Tool: resolve_incident (issue #153)
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="resolve_incident",
    description=(
        "Compose a recommendation bundle for an alert: target resource, "
        "responsible PR, related Slack threads, and a v0.1 confidence score. "
        "alert_id MUST be of the form 'alert://{provider}/{provider_alert_id}'. "
        "Optional `as_of` (ISO-8601 UTC datetime) anchors the traversal to "
        "graph state at that time per ADR-0008 §5. Requires a workspace-scoped "
        "token. Confidence-score model: v0.1 deterministic placeholder per "
        "issue #153 §C; calibrated model lands in #155."
    ),
)
async def resolve_incident(
    alert_id: str,
    ctx: Context[Any, Any, Any],
    max_depth: int = DEFAULT_MAX_DEPTH,
    as_of: str | None = None,
) -> dict[str, Any]:
    """resolve_incident tool — requires scope 'search' AND a workspace token.

    Mirrors the auth posture of :func:`get_related_entities` — graph
    reads are workspace-scoped, fail-closed, and reject unscoped
    tokens.  Cross-workspace ``alert_id`` resolution returns
    ``alert_not_found`` to avoid leaking alert existence (issue #153
    §D / #117).
    """
    token = await _resolve_token(ctx)
    _require_scope(token, Scope.search)
    _record_tool_invocation(tool_name="resolve_incident", token=token)
    assert token is not None  # noqa: S101 — narrows type for mypy

    workspace_id = _require_workspace(
        token,
        log_event="mcp_resolve_incident_rejected_no_workspace",
        forbidden_message="Graph retrieval requires a workspace-scoped token",
    )

    parsed_as_of = _parse_as_of(as_of)
    clamped_depth = max(MIN_MAX_DEPTH, min(max_depth, MAX_MAX_DEPTH))
    try:
        legacy_response = await mcp_resolve_incident(
            app=_get_app(),
            alert_id=alert_id,
            workspace_id=workspace_id,
            as_of=parsed_as_of,
            max_depth=clamped_depth,
        )
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith(f"{INVALID_ALERT_ID_CODE}:"):
            raise
        if msg.startswith(f"{ALERT_NOT_FOUND_CODE}:"):
            raise
        raise
    # Issue #238: MCP Apps UI card wrapper.  When the connected client
    # advertised the ``omniscience/apps`` experimental capability, attach
    # the rendered card under ``_meta``; otherwise the legacy response
    # passes through byte-for-byte (backwards-compat invariant).
    return _apply_mcp_budget(wrap_resolve_incident_response(legacy_response, ctx=ctx))


# ---------------------------------------------------------------------------
# Tool: incident_timeline (issue #235)
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="incident_timeline",
    description=(
        "Render the bitemporal state-change timeline for entities in an "
        "alerts blast radius. alert_id MUST be of the form "
        "alert://{provider}/{provider_alert_id}. Optional from/to bound "
        "the event-time window; optional entity_types is an allowlist of "
        "entity kinds. Returns events sorted ascending by timestamp with "
        "before/after summaries and source provenance per ADR-0008 1. "
        "Requires a workspace-scoped token."
    ),
)
async def incident_timeline(
    alert_id: str,
    ctx: Context[Any, Any, Any],
    from_ts: str | None = None,
    to_ts: str | None = None,
    entity_types: list[str] | None = None,
    as_of: str | None = None,
    max_depth: int = TIMELINE_MAX_DEPTH,
) -> dict[str, Any]:
    """incident_timeline tool requires scope search AND a workspace token.

    Mirrors the auth posture of resolve_incident workspace-scoped,
    fail-closed, no existence leak for cross-workspace alert ids
    (issue #117 / ADR-0005).
    """
    return _apply_mcp_budget(
        await incident_timeline_tool(
            app=_get_app(),
            ctx=ctx,
            resolve_token=_resolve_token,
            require_scope=_require_scope,
            parse_as_of=_parse_as_of,
            record_invocation=_record_tool_invocation,
            alert_id=alert_id,
            from_ts=from_ts,
            to_ts=to_ts,
            entity_types=entity_types,
            as_of=as_of,
            max_depth=max_depth,
        )
    )


# ---------------------------------------------------------------------------
# Tool: blast_radius (issue #234)
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="blast_radius",
    description=(
        "Traverse the causal dependency graph from a seed entity and "
        "rank the downstream entities impacted by performing "
        "`action_type` on it.  Supported actions: "
        + ", ".join(ACTION_TYPES)
        + ".  Returns a list of `{entity_id, entity_type, dependency_path, "
        "impact_score, confidence}` ranked descending by impact_score.  "
        "Optional `as_of` (ISO-8601 UTC datetime) anchors the traversal "
        "to graph state at that time per ADR-0008 §5.  Requires a "
        "workspace-scoped token.  Impact-score model: v0.1 deterministic "
        "placeholder per issue #234 spec; calibrated model is a follow-up."
    ),
)
async def blast_radius(
    entity_id: str,
    ctx: Context[Any, Any, Any],
    action_type: str = "restart",
    max_depth: int = BLAST_DEFAULT_MAX_DEPTH,
    as_of: str | None = None,
) -> dict[str, Any]:
    """blast_radius tool — requires scope 'search' AND a workspace token.

    Mirrors the auth posture of :func:`resolve_incident` — graph reads
    are workspace-scoped, fail-closed, and reject unscoped tokens.
    Cross-workspace ``entity_id`` resolution returns ``entity_not_found``
    to avoid leaking entity existence (issue #117 / #234 §ACL).
    """
    token = await _resolve_token(ctx)
    _require_scope(token, Scope.search)
    _record_tool_invocation(tool_name="blast_radius", token=token)
    assert token is not None  # noqa: S101 — narrows type for mypy

    workspace_id = _require_workspace(
        token,
        log_event="mcp_blast_radius_rejected_no_workspace",
        forbidden_message="Graph retrieval requires a workspace-scoped token",
    )

    if action_type not in ACTION_TYPES:
        raise ValueError(
            f"{INVALID_ACTION_TYPE_CODE}:action_type must be one of " + ", ".join(ACTION_TYPES)
        )
    # Narrow to ActionType (runtime check above guarantees it).
    typed_action: ActionType = action_type

    parsed_as_of = _parse_as_of(as_of)
    clamped_depth = max(BLAST_MIN_MAX_DEPTH, min(max_depth, BLAST_MAX_MAX_DEPTH))
    try:
        return _apply_mcp_budget(
            await mcp_blast_radius(
                app=_get_app(),
                entity_id=entity_id,
                workspace_id=workspace_id,
                action_type=typed_action,
                as_of=parsed_as_of,
                max_depth=clamped_depth,
            )
        )
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith(f"{INVALID_ENTITY_ID_CODE}:"):
            raise
        if msg.startswith(f"{INVALID_ACTION_TYPE_CODE}:"):
            raise
        if msg.startswith(f"{BLAST_ENTITY_NOT_FOUND_CODE}:"):
            raise
        raise


# ---------------------------------------------------------------------------
# Tool: replay_context (issue #243) — ADP replay primitive
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="replay_context",
    description=(
        "Replay a previous tool invocation against the bitemporal graph at "
        "a specific time. Surfaces the ADR-0008 substrate as the public "
        "'what did the agent see at time T' primitive (ADP). Either supply "
        "audit_log_id (replay a stored invocation) or at_time + tool_name + "
        "arguments (inline replay). Returns the original-shape response plus "
        "a deterministic state_fingerprint hash. Requires a workspace-scoped "
        "token. Supported tool_name values: " + ", ".join(SUPPORTED_REPLAY_TOOLS) + "."
    ),
)
async def replay_context_tool(
    ctx: Context[Any, Any, Any],
    audit_log_id: str | None = None,
    at_time: str | None = None,
    tool_name: str | None = None,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """replay_context tool — requires scope 'search' AND workspace token."""
    token = await _resolve_token(ctx)
    _require_scope(token, Scope.search)
    _record_tool_invocation(tool_name="replay_context", token=token)
    assert token is not None  # noqa: S101 — narrows type for mypy

    workspace_id = _require_workspace(
        token,
        log_event="mcp_replay_rejected_no_workspace",
        forbidden_message="Replay requires a workspace-scoped token",
    )

    has_audit = audit_log_id is not None and audit_log_id != ""
    has_inline = at_time is not None or tool_name is not None
    if has_audit and has_inline:
        raise ValueError(
            "bad_request:Supply either audit_log_id OR (at_time + tool_name + arguments)"
        )
    if not has_audit and not (at_time and tool_name):
        raise ValueError("bad_request:Must supply audit_log_id OR (at_time AND tool_name)")

    app = _get_app()
    factory = getattr(app.state, "db_session_factory", None)
    if factory is None:
        raise RuntimeError("db_session_factory not available on app.state")

    try:
        if has_audit:
            import uuid as _uuid

            try:
                parsed_id = _uuid.UUID(audit_log_id)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"bad_request:audit_log_id must be a UUID, got {audit_log_id!r}"
                ) from exc
            async with factory() as session:
                result = await replay_by_audit_id(
                    app=app,
                    session=session,
                    audit_log_id=parsed_id,
                    workspace_id=workspace_id,
                )
        else:
            parsed_at = _parse_as_of(at_time)
            assert parsed_at is not None  # noqa: S101 — has_inline guards this
            assert tool_name is not None  # noqa: S101
            result = await replay_context(
                app=app,
                workspace_id=workspace_id,
                at_time=parsed_at,
                query=ReplayQuery(
                    tool_name=tool_name,
                    arguments=dict(arguments or {}),
                ),
            )
    except AuditLogNotFoundError as exc:
        raise ValueError(f"audit_log_not_found:{exc}") from exc

    return envelope_to_dict(result)


# ---------------------------------------------------------------------------
# Tool: suggest_runbook (issue #231)
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="suggest_runbook",
    description=(
        "Return the top matching runbooks for an alert. "
        "alert_id MUST be of the form 'alert://{provider}/{provider_alert_id}'. "
        "Returns suggestions ordered by confidence DESC with citation blocks "
        "(title, source, first step) so the responder UI can render previews "
        "without a second round-trip. Optional `as_of` (ISO-8601 UTC datetime) "
        "anchors the traversal to graph state at that time per ADR-0008 §5. "
        "Requires a workspace-scoped token. Confidence-score model: v0.1 "
        "deterministic placeholder per issue #231; calibrated model is a "
        "follow-up."
    ),
)
async def suggest_runbook(
    alert_id: str,
    ctx: Context[Any, Any, Any],
    top_k: int = RUNBOOK_DEFAULT_TOP_K,
    as_of: str | None = None,
) -> dict[str, Any]:
    """suggest_runbook tool requires scope 'search' AND workspace token."""
    token = await _resolve_token(ctx)
    _require_scope(token, Scope.search)
    _record_tool_invocation(tool_name="suggest_runbook", token=token)
    assert token is not None  # noqa: S101 — narrows type for mypy

    workspace_id = _require_workspace(
        token,
        log_event="mcp_suggest_runbook_rejected_no_workspace",
        forbidden_message="Runbook suggestion requires a workspace-scoped token",
    )

    parsed_as_of = _parse_as_of(as_of)
    clamped_top_k = max(1, min(top_k, RUNBOOK_MAX_TOP_K))
    try:
        return await mcp_suggest_runbook(
            app=_get_app(),
            alert_id=alert_id,
            workspace_id=workspace_id,
            as_of=parsed_as_of,
            top_k=clamped_top_k,
        )
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith(f"{RUNBOOK_INVALID_ALERT_ID_CODE}:"):
            raise
        if msg.startswith(f"{RUNBOOK_ALERT_NOT_FOUND_CODE}:"):
            raise
        raise


# ---------------------------------------------------------------------------
# Tool: find_similar_incidents (issue #233)
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="find_similar_incidents",
    description=(
        "Find past incidents similar to the given incident via the "
        "same-incident clustering primitive (alert_type + service + "
        "symptom-embedding signature with recency decay). incident_id "
        "MUST be of the form alert://{provider}/{provider_alert_id}. "
        "Returns ranked matches with per-component similarity breakdown. "
        "Optional `as_of` anchors the comparison window per ADR-0008 §5. "
        "Requires a workspace-scoped token."
    ),
)
async def find_similar_incidents(
    incident_id: str,
    ctx: Context[Any, Any, Any],
    limit: int = 5,
    since_days: int = 90,
    as_of: str | None = None,
) -> dict[str, Any]:
    """find_similar_incidents tool — requires scope 'search' AND workspace token."""
    token = await _resolve_token(ctx)
    _require_scope(token, Scope.search)
    _record_tool_invocation(tool_name="find_similar_incidents", token=token)
    assert token is not None  # noqa: S101 — narrows type for mypy

    workspace_id = _require_workspace(
        token,
        log_event="mcp_find_similar_incidents_rejected_no_workspace",
        forbidden_message="Similar-incident search requires a workspace-scoped token",
    )

    parsed_as_of = _parse_as_of(as_of)
    clamped_limit = max(1, min(limit, SIMILAR_MAX_LIMIT))
    clamped_since = max(1, min(since_days, SIMILAR_MAX_SINCE_DAYS))
    return await mcp_find_similar_incidents(
        app=_get_app(),
        incident_id=incident_id,
        workspace_id=workspace_id,
        limit=clamped_limit,
        since_days=clamped_since,
        as_of=parsed_as_of,
    )


__all__ = ["mcp_server", "set_fastapi_app"]


# ---------------------------------------------------------------------------
# Tool: generate_postmortem (issue #232)
# ---------------------------------------------------------------------------


# Late imports keep the module's import-time fast path unchanged and
# avoid widening the import graph for tools that don't need the post-
# mortem surface.
from omniscience_server.postmortem import (  # noqa: E402
    SUPPORTED_TEMPLATES as POSTMORTEM_SUPPORTED_TEMPLATES,
)
from omniscience_server.postmortem import (  # noqa: E402
    PostmortemError,
    generate_postmortem,
    render,
)


@mcp_server.tool(
    name="generate_postmortem",
    description=(
        "Generate a templated post-mortem from the bitemporal graph "
        "timeline for an incident. alert_id MUST be 'alert://{provider}/"
        "{provider_alert_id}'. template is one of "
        + ", ".join(POSTMORTEM_SUPPORTED_TEMPLATES)
        + " (default 'blameless'). format is one of 'markdown' (default), "
        "'html', or 'json'. Each timeline entry cites its entity with the "
        "as-of timestamp per ADR-0008 5. Action items are extracted into "
        "FollowUp entities. Requires a workspace-scoped token."
    ),
)
async def generate_postmortem_tool(
    incident_id: str,
    alert_id: str,
    ctx: Context[Any, Any, Any],
    template: str = "blameless",
    format: str = "markdown",  # noqa: A002 — MCP wire convention
    incident_start: str | None = None,
    incident_end: str | None = None,
) -> dict[str, Any]:
    """generate_postmortem tool — requires scope 'search' + workspace token."""
    token = await _resolve_token(ctx)
    _require_scope(token, Scope.search)
    _record_tool_invocation(tool_name="generate_postmortem", token=token)
    assert token is not None  # noqa: S101 — narrows type for mypy

    workspace_id = _require_workspace(
        token,
        log_event="mcp_generate_postmortem_rejected_no_workspace",
        forbidden_message="Post-mortem generation requires a workspace-scoped token",
    )

    parsed_start = _parse_as_of(incident_start)
    parsed_end = _parse_as_of(incident_end)

    try:
        report = await generate_postmortem(
            app=_get_app(),
            incident_id=incident_id,
            alert_id=alert_id,
            workspace_id=workspace_id,
            template=template,
            incident_start=parsed_start,
            incident_end=parsed_end,
        )
        rendered = render(report, fmt=format)
    except PostmortemError as exc:
        # Re-raise as a ValueError with the structured 'code:message'
        # envelope FastMCP serialises into a tool-error response.
        raise ValueError(str(exc)) from exc

    return {
        "incident_id": report.incident_id,
        "alert_id": report.alert_id,
        "template": report.template_id,
        "format": format,
        "rendered": rendered,
        "report": report.model_dump(mode="json"),
    }
