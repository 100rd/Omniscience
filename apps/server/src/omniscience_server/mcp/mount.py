"""Mount the FastMCP server into the FastAPI application.

Provides:
- create_mcp_asgi_app(app): wire the FastAPI app and return the Starlette
  ASGI app for streamable-http transport.
- run_stdio(): synchronous entry-point for stdio (CLI) usage.

Usage in app.py:
    from omniscience_server.mcp.mount import create_mcp_asgi_app
    app.mount("/mcp", create_mcp_asgi_app(app))
"""

from __future__ import annotations

import anyio
from fastapi import FastAPI
from starlette.types import ASGIApp

from omniscience_server.mcp.server import mcp_server, set_fastapi_app


def create_mcp_asgi_app(app: FastAPI) -> ASGIApp:
    """Wire the FastAPI app reference and return the MCP ASGI sub-app.

    Args:
        app: The parent FastAPI application whose ``app.state`` provides
             ``db_session_factory`` and ``retrieval_service``.

    Returns:
        A Starlette ASGI application handling streamable-http MCP traffic.
        Mount this at ``/mcp`` in the parent application.
    """
    set_fastapi_app(app)
    starlette_app = mcp_server.streamable_http_app()
    return starlette_app


def run_stdio(app: FastAPI) -> None:
    """Run the MCP server in stdio mode (blocking).

    Args:
        app: The FastAPI application instance providing app.state deps.
    """
    set_fastapi_app(app)
    anyio.run(mcp_server.run_stdio_async)


__all__ = ["create_mcp_asgi_app", "run_stdio"]
