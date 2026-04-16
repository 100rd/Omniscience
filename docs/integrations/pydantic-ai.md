# Using Omniscience from PydanticAI

You have PydanticAI agents (typed, lightweight) and want them to query Omniscience. PydanticAI has native MCP support.

## Prerequisites

- Running Omniscience instance
- Omniscience API token with `search` + `sources:read` scopes
- Python 3.10+, PydanticAI installed

## Install

```bash
pip install pydantic-ai
```

## Connect

```python
import os
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerHTTP

OMNISCIENCE_URL = os.environ["OMNISCIENCE_URL"]
OMNISCIENCE_TOKEN = os.environ["OMNISCIENCE_TOKEN"]

omniscience = MCPServerHTTP(
    url=f"{OMNISCIENCE_URL}/mcp",
    headers={"Authorization": f"Bearer {OMNISCIENCE_TOKEN}"},
)

agent = Agent(
    "anthropic:claude-sonnet-4-5",   # or "google-gla:gemini-2.5-flash"
    mcp_servers=[omniscience],
    system_prompt=(
        "You answer engineering questions. Use omniscience.search to find "
        "grounded context. Cite chunk_id + uri for every claim."
    ),
)

async def main():
    async with agent.run_mcp_servers():
        result = await agent.run(
            "How does authentication work in the payments service?"
        )
        print(result.data)
```

## When PydanticAI fits

- **Typed, structured output** — Pydantic models as `result_type` give you guaranteed schemas
- **Simple loops** — one or two tool calls per user turn, not complex state graphs
- **Clean API, low overhead** — the fastest way to ship a typed agent
- **Vendor-neutral** — `"anthropic:..."` and `"google-gla:..."` work identically

For complex workflows, use [LangGraph](langgraph.md). For role-based teams, use [CrewAI](crewai.md).

## Recommended patterns

### Structured retrieval responses

```python
from pydantic import BaseModel

class Citation(BaseModel):
    chunk_id: str
    uri: str
    title: str
    indexed_at: str

class Answer(BaseModel):
    text: str
    citations: list[Citation]

agent = Agent(
    "anthropic:claude-sonnet-4-5",
    mcp_servers=[omniscience],
    result_type=Answer,
    system_prompt="Return an Answer with at least 2 citations from omniscience.search.",
)

async with agent.run_mcp_servers():
    result = await agent.run("Explain our rate-limiting strategy.")
    # result.data is a validated Answer instance
    for c in result.data.citations:
        print(c.uri, c.indexed_at)
```

### Streaming

```python
async with agent.run_mcp_servers():
    async with agent.run_stream("What changed in the last deploy?") as result:
        async for chunk in result.stream_text():
            print(chunk, end="", flush=True)
```

### Multi-source filtering

PydanticAI passes through MCP tool args, so you can steer via system prompt or explicit tool args:

```python
# In your system prompt:
"When the user asks about docs, call search with sources=['company-wiki']. "
"When asking about code, call search with sources=['main-gitlab']."
```

## Scope and security

Same as other guides: narrow token scopes, store `OMNISCIENCE_TOKEN` via secret manager, propagate lineage fields.

## See also

- [LangGraph integration](langgraph.md)
- [CrewAI integration](crewai.md)
- [MCP API reference](../api/mcp.md)
- [PydanticAI MCP docs](https://ai.pydantic.dev/mcp/)
