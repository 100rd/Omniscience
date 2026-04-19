"""MCP client for the Omniscience knowledge retrieval service.

Wraps the official ``mcp`` SDK to call Omniscience MCP tools over the
streamable-HTTP transport.  Provides the same high-level interface as
:class:`~omniscience_client.client.OmniscienceClient` for tool-based callers.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from omniscience_client.types import Document, DocumentWithChunks, SearchResult


class OmniscienceMCP:
    """MCP client using the official ``mcp`` SDK (streamable-HTTP transport).

    Usage::

        async with OmniscienceMCP() as client:
            await client.connect("http://localhost:8000/mcp", token="omni_...")
            result = await client.search("retrieval augmented generation")

    The ``connect`` method establishes the MCP session.  Call ``close()``
    (or use as an async context manager) to tear down cleanly.
    """

    def __init__(self) -> None:
        self._session: ClientSession | None = None
        self._stack = AsyncExitStack()

    # ------------------------------------------------------------------
    # Context manager / lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> OmniscienceMCP:
        await self._stack.__aenter__()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def connect(self, url: str, token: str) -> None:
        """Open a streamable-HTTP MCP session.

        Args:
            url: Full MCP endpoint URL, e.g. ``http://localhost:8000/mcp``.
            token: Bearer token string for authentication.
        """
        headers = {"Authorization": f"Bearer {token}"}
        read_stream, write_stream, _ = await self._stack.enter_async_context(
            streamablehttp_client(url, headers=headers)
        )
        self._session = await self._stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await self._session.initialize()

    async def close(self) -> None:
        """Tear down the MCP session and release all resources."""
        await self._stack.aclose()
        self._session = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("Not connected — call await client.connect() first")
        return self._session

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        session = self._require_session()
        result = await session.call_tool(name, arguments)
        # FastMCP returns a list of content items; first item is typically text/json
        if result.content:
            first = result.content[0]
            raw = getattr(first, "text", None)
            if raw is not None:
                import json

                try:
                    return json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    return raw
        return None

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        top_k: int = 10,
        *,
        sources: list[str] | None = None,
        types: list[str] | None = None,
        max_age_seconds: int | None = None,
        filters: dict[str, Any] | None = None,
        include_tombstoned: bool = False,
        retrieval_strategy: str = "hybrid",
    ) -> SearchResult:
        """Invoke the MCP ``search`` tool.

        Args:
            query: Free-text search query.
            top_k: Maximum number of hits to return.
            sources: Restrict to specific source IDs or names.
            types: Restrict to specific document types.
            max_age_seconds: Only return documents indexed within this window.
            filters: Arbitrary metadata key/value filters.
            include_tombstoned: Include soft-deleted documents.
            retrieval_strategy: One of ``hybrid``, ``keyword``, ``structural``, ``auto``.

        Returns:
            A :class:`~omniscience_client.types.SearchResult` with ranked hits.
        """
        arguments: dict[str, Any] = {
            "query": query,
            "top_k": top_k,
            "include_tombstoned": include_tombstoned,
            "retrieval_strategy": retrieval_strategy,
        }
        if sources is not None:
            arguments["sources"] = sources
        if types is not None:
            arguments["types"] = types
        if max_age_seconds is not None:
            arguments["max_age_seconds"] = max_age_seconds
        if filters is not None:
            arguments["filters"] = filters

        data = await self._call_tool("search", arguments)
        return SearchResult.model_validate(data)

    async def get_document(self, document_id: str) -> Document:
        """Invoke the MCP ``get_document`` tool.

        Args:
            document_id: UUID string of the document to fetch.

        Returns:
            The :class:`~omniscience_client.types.Document` (without chunks —
            use the REST client's :meth:`get_document` to get chunks).
        """
        data = await self._call_tool("get_document", {"document_id": document_id})
        # The MCP tool returns DocumentWithChunks-shaped data; extract the document part
        if isinstance(data, dict) and "document" in data:
            return Document.model_validate(data["document"])
        return Document.model_validate(data)

    async def get_document_with_chunks(self, document_id: str) -> DocumentWithChunks:
        """Invoke the MCP ``get_document`` tool and return full document with chunks.

        Args:
            document_id: UUID string of the document to fetch.

        Returns:
            A :class:`~omniscience_client.types.DocumentWithChunks` instance.
        """
        data = await self._call_tool("get_document", {"document_id": document_id})
        return DocumentWithChunks.model_validate(data)


__all__ = ["OmniscienceMCP"]
