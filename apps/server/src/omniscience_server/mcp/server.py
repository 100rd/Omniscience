"""FastMCP server setup for Omniscience.

Registers four tools:
- search       (requires scope: search)
- get_document (requires scope: search)
- list_sources (requires scope: sources:read)
- source_stats (requires scope: sources:read)

Auth:
- HTTP transport: Bearer token from Authorization header
- stdio transport: OMNISCIENCE_TOKEN environment variable

The FastMCP instance is module-level.  Before mounting, call
``set_fastapi_app(app)`` so tool handlers can reach the FastAPI
app.state (db_session_factory, retrieval_service).
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from fastapi import FastAPI
from mcp.server.fastmcp import Context, FastMCP
from omniscience_core.auth.middleware import _lookup_token
from omniscience_core.auth.scopes import Scope, check_scopes
from omniscience_core.db.models import ApiToken

from omniscience_server.mcp.tools import (
    mcp_get_document,
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


# ---------------------------------------------------------------------------
# Tool: search
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="search",
    description=(
        "Hybrid vector + BM25 retrieval over the Omniscience index. "
        "Returns ranked chunks with citations and lineage."
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
) -> dict[str, Any]:
    """Search tool — requires scope 'search'."""
    token = await _resolve_token(ctx)
    _require_scope(token, Scope.search)

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

    try:
        return await mcp_get_document(app=_get_app(), document_id=document_id)
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith("document_not_found:"):
            raise ValueError(f"source_not_found:{document_id}") from exc
        raise


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

    try:
        return await mcp_source_stats(app=_get_app(), source_id=source_id)
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith("source_not_found:"):
            raise
        raise


__all__ = ["mcp_server", "set_fastapi_app"]
