"""Mount the FastMCP server into the FastAPI application.

Provides:
- create_mcp_asgi_app(app): wire the FastAPI app and return the Starlette
  ASGI app for streamable-http transport.
- run_stdio(): synchronous entry-point for stdio (CLI) usage.

Usage in app.py:
    from omniscience_server.mcp.mount import create_mcp_asgi_app
    app.mount("/mcp", create_mcp_asgi_app(app))

Connected-clients telemetry (issue #113)
----------------------------------------
:class:`McpSessionTelemetryMiddleware` wraps the FastMCP ASGI app to emit
connect / disconnect events into the in-process telemetry registry.  The
"streaming" flag is derived from the request's ``Accept`` header: a
client that says ``Accept: text/event-stream`` receives an SSE-style
streaming response and is logged as ``streaming="true"``; everything
else is logged as ``streaming="false"``.

We deliberately do not patch FastMCP internals — every observable signal
we need is on the ASGI scope.  This keeps the integration robust against
upstream MCP-server changes.
"""

from __future__ import annotations

import uuid
from typing import Final

import anyio
import structlog
from fastapi import FastAPI
from omniscience_core.telemetry.clients import (
    ClientTelemetryRegistry,
    get_client_registry,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from omniscience_server.mcp.server import mcp_server, set_fastapi_app

log = structlog.get_logger(__name__)

#: Header value that indicates the client wants a streaming response.
_SSE_ACCEPT_VALUE: Final[str] = "text/event-stream"


def _is_streaming(scope: Scope) -> bool:
    """Return True if the ASGI request asked for a streaming response."""
    headers = scope.get("headers") or []
    for name, value in headers:
        if name == b"accept" and _SSE_ACCEPT_VALUE.encode() in value.lower():
            return True
    return False


class McpSessionTelemetryMiddleware:
    """ASGI middleware that records MCP session connect / disconnect.

    Each HTTP exchange to the mounted MCP app is treated as one session
    boundary — connect on receive, disconnect on the outermost response
    completion (``http.response.body`` with ``more_body=False``) or on
    a client-initiated abort.  We use a synthetic UUID per request as
    the session id; cardinality stays bounded by the LRU in the
    telemetry registry.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        registry: ClientTelemetryRegistry | None = None,
    ) -> None:
        self._app = app
        self._registry = registry or get_client_registry()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        session_id = str(uuid.uuid4())
        streaming = _is_streaming(scope)
        connected = False
        try:
            self._registry.record_session_connect(
                session_id=session_id,
                streaming=streaming,
            )
            connected = True
        except Exception as exc:  # pragma: no cover - defensive only
            log.warning("mcp_session_connect_record_failed", error=str(exc))

        async def _send_wrapper(message: Message) -> None:
            await send(message)

        try:
            await self._app(scope, receive, _send_wrapper)
        finally:
            if connected:
                try:
                    self._registry.record_session_disconnect(
                        session_id=session_id,
                        streaming=streaming,
                    )
                except Exception as exc:  # pragma: no cover - defensive only
                    log.warning("mcp_session_disconnect_record_failed", error=str(exc))


def create_mcp_asgi_app(app: FastAPI) -> ASGIApp:
    """Wire the FastAPI app reference and return the MCP ASGI sub-app.

    Args:
        app: The parent FastAPI application whose ``app.state`` provides
             ``db_session_factory`` and ``retrieval_service``.

    Returns:
        A Starlette ASGI application handling streamable-http MCP traffic
        wrapped in :class:`McpSessionTelemetryMiddleware`.  Mount this at
        ``/mcp`` in the parent application.
    """
    set_fastapi_app(app)
    # Serve the transport at the sub-app root so, mounted at "/mcp" in the
    # parent, the endpoint is "/mcp" — not the default "/mcp/mcp" (FastMCP's
    # streamable_http_path defaults to "/mcp", which doubles under the mount).
    mcp_server.settings.streamable_http_path = "/"
    starlette_app = mcp_server.streamable_http_app()
    return McpSessionTelemetryMiddleware(starlette_app)


def run_stdio(app: FastAPI) -> None:
    """Run the MCP server in stdio mode (blocking).

    Args:
        app: The FastAPI application instance providing app.state deps.
    """
    set_fastapi_app(app)
    anyio.run(mcp_server.run_stdio_async)


__all__ = ["McpSessionTelemetryMiddleware", "create_mcp_asgi_app", "run_stdio"]
