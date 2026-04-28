"""FastMCP server setup for Omniscience.

Registers six tools:
- search              (requires scope: search)
- get_document        (requires scope: search)
- get_entity          (requires scope: search + workspace-scoped token)
- get_related_entities (requires scope: search + workspace-scoped token)
- list_sources        (requires scope: sources:read)
- source_stats        (requires scope: sources:read)

Auth:
- HTTP transport: Bearer token from Authorization header
- stdio transport: OMNISCIENCE_TOKEN environment variable

The FastMCP instance is module-level.  Before mounting, call
``set_fastapi_app(app)`` so tool handlers can reach the FastAPI
app.state (db_session_factory, retrieval_service).

Bitemporal reads (issue #133)
-----------------------------

``search``, ``get_entity`` and ``get_related_entities`` accept an
optional ``as_of`` parameter — an ISO-8601 timezone-aware UTC datetime
string (``Z`` suffix or ``+00:00``).  Naive datetimes are rejected with
the structured error code ``invalid_timezone``.  See ADR-0008 §5 for
the bitemporal predicate semantics.  ``resolve_incident`` is filed as
issue #153 and will surface its own ``as_of`` natively when it lands.
"""

from __future__ import annotations

import os
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
from omniscience_server.mcp.tools import (
    mcp_get_document,
    mcp_get_entity,
    mcp_get_related_entities,
    mcp_list_sources,
    mcp_search,
    mcp_source_stats,
)

log = structlog.get_logger(__name__)

mcp_server: FastMCP[None] = FastMCP("omniscience")

# FastAPI app reference — set via set_fastapi_app() before first request
_fastapi_app: FastAPI | None = None


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

    return await mcp_search(
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

    try:
        return await mcp_get_document(app=_get_app(), document_id=document_id)
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

    workspace_id = get_workspace_id(token)
    if workspace_id is None:
        log.warning(
            "mcp_graph_request_rejected_no_workspace",
            token_prefix=token.token_prefix,
        )
        raise ValueError("forbidden:Graph retrieval requires a workspace-scoped token")

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

    workspace_id = get_workspace_id(token)
    if workspace_id is None:
        log.warning(
            "mcp_get_entity_rejected_no_workspace",
            token_prefix=token.token_prefix,
        )
        raise ValueError("forbidden:Graph retrieval requires a workspace-scoped token")

    parsed_as_of = _parse_as_of(as_of)
    return await mcp_get_entity(
        app=_get_app(),
        entity_name=entity_name,
        workspace_id=workspace_id,
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

    return await mcp_list_sources(app=_get_app())


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

    try:
        return await mcp_source_stats(app=_get_app(), source_id=source_id)
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith("source_not_found:"):
            raise
        raise


__all__ = ["mcp_server", "set_fastapi_app"]
