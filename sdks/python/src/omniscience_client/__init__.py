"""omniscience-client — Python SDK for the Omniscience knowledge retrieval service.

Quick start::

    import asyncio
    from omniscience_client import OmniscienceClient

    async def main():
        async with OmniscienceClient(
            base_url="http://localhost:8000",
            token="omni_your_token_here",
        ) as client:
            result = await client.search("retrieval augmented generation", top_k=5)
            for hit in result.hits:
                print(f"[{hit.score:.3f}] {hit.citation.title or hit.citation.uri}")
                print(hit.text[:200])
                print()

    asyncio.run(main())

MCP usage::

    from omniscience_client import OmniscienceMCP

    async with OmniscienceMCP() as mcp:
        await mcp.connect("http://localhost:8000/mcp", token="omni_...")
        result = await mcp.search("vector database comparison")
"""

from omniscience_client.client import OmniscienceClient
from omniscience_client.exceptions import (
    APIError,
    AuthenticationError,
    NotFoundError,
    OmniscienceError,
    RateLimitError,
    ServerError,
)
from omniscience_client.exceptions import PermissionError as OmnisciencePermissionError
from omniscience_client.mcp import OmniscienceMCP
from omniscience_client.types import (
    ApiToken,
    Chunk,
    ChunkLineage,
    Citation,
    Document,
    DocumentWithChunks,
    IngestionRun,
    QueryStats,
    SearchHit,
    SearchRequest,
    SearchResult,
    Source,
    SourceInfo,
    TokenCreateResponse,
)

__version__ = "0.1.0"

__all__ = [
    "APIError",
    "ApiToken",
    "AuthenticationError",
    "Chunk",
    "ChunkLineage",
    "Citation",
    "Document",
    "DocumentWithChunks",
    "IngestionRun",
    "NotFoundError",
    "OmniscienceClient",
    "OmniscienceError",
    "OmniscienceMCP",
    "OmnisciencePermissionError",
    "QueryStats",
    "RateLimitError",
    "SearchHit",
    "SearchRequest",
    "SearchResult",
    "ServerError",
    "Source",
    "SourceInfo",
    "TokenCreateResponse",
    "__version__",
]
