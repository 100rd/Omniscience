"""MCP server module for Omniscience.

Exposes a FastMCP server with four tools:
- search: hybrid retrieval
- get_document: fetch full document with all chunks
- list_sources: list configured sources with freshness
- source_stats: per-source details
"""

from .mount import create_mcp_asgi_app, run_stdio
from .server import mcp_server, set_fastapi_app

__all__ = ["create_mcp_asgi_app", "mcp_server", "run_stdio", "set_fastapi_app"]
