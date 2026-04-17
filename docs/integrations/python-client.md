# Python Client for Omniscience

Call Omniscience directly from Python using the `mcp` SDK. Use this when you want programmatic control over retrieval without an agent framework — scripting, data pipelines, evaluation harnesses, custom tools, or embedding Omniscience into your own application.

## Prerequisites

- Running Omniscience instance
- Omniscience API token with `search` + `sources:read` scopes
- Python 3.10+

## Install

```bash
pip install mcp httpx
```

`mcp` is the official Python SDK for the Model Context Protocol. `httpx` is its HTTP transport dependency.

## Minimal example — search and print results

```python
#!/usr/bin/env python3
"""
Minimal Omniscience MCP client.
Connects, calls search, prints results with citations.

Usage:
    export OMNISCIENCE_URL=http://localhost:8000
    export OMNISCIENCE_TOKEN=omni_dev_...
    python omniscience_client.py
"""

import asyncio
import os
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


OMNISCIENCE_URL = os.environ["OMNISCIENCE_URL"]
OMNISCIENCE_TOKEN = os.environ["OMNISCIENCE_TOKEN"]


async def search(query: str, top_k: int = 5) -> None:
    url = f"{OMNISCIENCE_URL}/mcp"
    headers = {"Authorization": f"Bearer {OMNISCIENCE_TOKEN}"}

    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            result = await session.call_tool(
                "search",
                arguments={"query": query, "top_k": top_k},
            )

            # result.content is a list of TextContent or similar
            data = json.loads(result.content[0].text)

            print(f"Query: {query!r}")
            print(f"Results: {len(data['hits'])} hits in {data['query_stats']['duration_ms']}ms\n")

            for i, hit in enumerate(data["hits"], 1):
                citation = hit["citation"]
                print(f"  [{i}] score={hit['score']:.3f}")
                print(f"      uri={citation['uri']}")
                print(f"      indexed_at={citation['indexed_at']}")
                print(f"      text={hit['text'][:120]!r}...")
                print()


if __name__ == "__main__":
    asyncio.run(search("authentication implementation", top_k=5))
```

Run it:

```bash
export OMNISCIENCE_URL=http://localhost:8000
export OMNISCIENCE_TOKEN=omni_dev_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
python omniscience_client.py
```

Expected output:

```
Query: 'authentication implementation'
Results: 5 hits in 28ms

  [1] score=0.923
      uri=https://github.com/org/repo/blob/abc123/apps/server/auth.py#L42-L60
      indexed_at=2026-04-16T10:32:15Z
      text='def authenticate_token(token: str) -> User:\n    """Validate Bearer token and return...'...

  [2] score=0.871
      ...
```

## Full client with all tools

```python
#!/usr/bin/env python3
"""
Full Omniscience MCP client demonstrating all four tools.
"""

import asyncio
import os
import json
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


OMNISCIENCE_URL = os.environ["OMNISCIENCE_URL"]
OMNISCIENCE_TOKEN = os.environ["OMNISCIENCE_TOKEN"]


class OmniscienceClient:
    """Thin wrapper around the MCP client for reuse across calls."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def search(
        self,
        query: str,
        top_k: int = 10,
        sources: list[str] | None = None,
        max_age_seconds: int | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"query": query, "top_k": top_k}
        if sources:
            args["sources"] = sources
        if max_age_seconds is not None:
            args["max_age_seconds"] = max_age_seconds

        result = await self._session.call_tool("search", arguments=args)
        return json.loads(result.content[0].text)

    async def get_document(self, document_id: str) -> dict[str, Any]:
        result = await self._session.call_tool(
            "get_document", arguments={"document_id": document_id}
        )
        return json.loads(result.content[0].text)

    async def list_sources(self) -> dict[str, Any]:
        result = await self._session.call_tool("list_sources", arguments={})
        return json.loads(result.content[0].text)

    async def source_stats(self, source_id: str) -> dict[str, Any]:
        result = await self._session.call_tool(
            "source_stats", arguments={"source_id": source_id}
        )
        return json.loads(result.content[0].text)


async def demo() -> None:
    url = f"{OMNISCIENCE_URL}/mcp"
    headers = {"Authorization": f"Bearer {OMNISCIENCE_TOKEN}"}

    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            client = OmniscienceClient(session)

            # List all sources
            sources_resp = await client.list_sources()
            print("Sources:")
            for src in sources_resp["sources"]:
                stale = " [STALE]" if src["is_stale"] else ""
                print(f"  {src['name']} ({src['type']}) — {src['indexed_document_count']} docs{stale}")
            print()

            # Search with filters
            results = await client.search(
                query="rate limiting middleware",
                top_k=3,
                sources=["main-gitlab"],
            )
            print(f"Search 'rate limiting middleware' (source=main-gitlab): {len(results['hits'])} hits")
            for hit in results["hits"]:
                print(f"  {hit['citation']['uri']}")
            print()

            # Expand a document
            if results["hits"]:
                doc_id = results["hits"][0]["document_id"]
                doc = await client.get_document(doc_id)
                print(f"Full document: {len(doc['chunks'])} chunks, uri={doc['document']['uri']}")

            # Freshness-restricted search
            fresh = await client.search(
                query="deployment config",
                max_age_seconds=3600,  # last hour only
            )
            print(f"\nFreshness-restricted (last 1h): {len(fresh['hits'])} hits")


if __name__ == "__main__":
    asyncio.run(demo())
```

## Using stdio transport

If Omniscience is running locally and you have the `omniscience` CLI installed, you can use the stdio transport instead of HTTP:

```python
import asyncio
import os
import json

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def search_via_stdio(query: str) -> None:
    server_params = StdioServerParameters(
        command="omniscience",
        args=["mcp", "serve", "--transport", "stdio"],
        env={
            "OMNISCIENCE_URL": os.environ["OMNISCIENCE_URL"],
            "OMNISCIENCE_TOKEN": os.environ["OMNISCIENCE_TOKEN"],
        },
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("search", arguments={"query": query})
            data = json.loads(result.content[0].text)
            print(f"{len(data['hits'])} hits for {query!r}")


if __name__ == "__main__":
    asyncio.run(search_via_stdio("authentication"))
```

## Listing available tools

To inspect what tools Omniscience exposes at runtime:

```python
async with streamablehttp_client(url, headers=headers) as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        for tool in tools.tools:
            print(f"{tool.name}: {tool.description}")
```

Expected output:

```
search: Hybrid vector + BM25 retrieval across indexed sources
get_document: Retrieve a full document by ID
list_sources: List configured sources with freshness metadata
source_stats: Detailed stats for a single source
```

## Error handling

```python
from mcp import McpError

try:
    result = await client.search("my query")
except McpError as e:
    if e.error.code == "unauthorized":
        print("Token invalid or missing — check OMNISCIENCE_TOKEN")
    elif e.error.code == "rate_limited":
        print("Rate limit hit — back off and retry")
    elif e.error.code == "embedding_provider_unavailable":
        print("Embedding service down — retry in a few seconds")
    else:
        print(f"MCP error: {e.error.code} — {e.error.message}")
```

## Connection reuse

The `ClientSession` setup adds ~100ms for the initial handshake. For scripts that run many queries, keep the session open for the full batch:

```python
async with streamablehttp_client(url, headers=headers) as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        client = OmniscienceClient(session)

        # Run all your queries inside this block
        for query in queries:
            results = await client.search(query)
            process(results)
```

Do not create a new session per query.

## Environment variables reference

| Variable | Description |
|---|---|
| `OMNISCIENCE_URL` | Base URL, e.g. `http://localhost:8000` or `https://omniscience.company.com` |
| `OMNISCIENCE_TOKEN` | API token with `search` + `sources:read` scopes |

Store the token via your secrets manager, not in source code. For local dev, a `.env` file loaded with `python-dotenv` is acceptable.

## See also

- [MCP API reference](../api/mcp.md) — full tool input/output contracts
- [LangGraph integration](langgraph.md) — if you want agent loop orchestration
- [PydanticAI integration](pydantic-ai.md) — if you want typed structured output
- [REST API](../api/rest.md) — for source management and health checks (not search)
