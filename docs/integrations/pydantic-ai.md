# Using Omniscience from PydanticAI

You have PydanticAI agents (typed, lightweight) and want them to query Omniscience. PydanticAI has native MCP support — connect Omniscience in three lines.

## Prerequisites

- Running Omniscience instance
- Omniscience API token with `search` + `sources:read` scopes
- Python 3.10+

If Omniscience is not yet running, follow the [deployment steps in the Claude Code guide](claude-code.md#step-1--deploy-omniscience) first.

## Step 1 — Create an API token

```bash
docker compose exec app omniscience tokens create \
  --name pydantic-ai \
  --scopes search,sources:read
```

Copy the printed token (`omni_dev_...`).

## Step 2 — Install dependencies

```bash
pip install pydantic-ai
```

## Step 3 — Connect and run

```python
import asyncio
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
    "anthropic:claude-sonnet-4-5",  # or "google-gla:gemini-2.5-flash"
    mcp_servers=[omniscience],
    system_prompt=(
        "You answer engineering questions. Use omniscience.search() to find "
        "grounded context from the codebase and docs. "
        "Cite chunk_id and uri for every claim."
    ),
)


async def main() -> None:
    async with agent.run_mcp_servers():
        result = await agent.run(
            "How does authentication work in the payments service?"
        )
        print(result.data)


if __name__ == "__main__":
    asyncio.run(main())
```

Run it:

```bash
export OMNISCIENCE_URL=http://localhost:8000
export OMNISCIENCE_TOKEN=omni_dev_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export ANTHROPIC_API_KEY=your-api-key
python agent.py
```

You should see the agent's answer with citations from Omniscience search results.

## When PydanticAI fits

- **Typed, structured output** — Pydantic models as `result_type` give you guaranteed schemas
- **Simple loops** — one or two tool calls per user turn, not complex state graphs
- **Clean API, low overhead** — the fastest way to ship a typed agent
- **Vendor-neutral** — `"anthropic:..."` and `"google-gla:..."` work identically with the same code

For complex workflows with branching, use [LangGraph](langgraph.md). For role-based multi-agent teams, use [CrewAI](crewai.md).

## Recommended patterns

### Structured retrieval responses

Get type-safe output with guaranteed citation fields:

```python
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerHTTP


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
    system_prompt=(
        "Return an Answer with at least 2 citations from omniscience.search. "
        "Every claim must have a chunk_id and uri."
    ),
)


async def main() -> None:
    async with agent.run_mcp_servers():
        result = await agent.run("Explain our rate-limiting strategy.")
        # result.data is a validated Answer instance
        print(result.data.text)
        for c in result.data.citations:
            print(f"  [{c.chunk_id}] {c.uri} — indexed {c.indexed_at}")


asyncio.run(main())
```

### Streaming responses

For long answers, stream text as it arrives:

```python
async with agent.run_mcp_servers():
    async with agent.run_stream("What changed in the last deploy?") as stream:
        async for chunk in stream.stream_text():
            print(chunk, end="", flush=True)
    print()
```

### Multi-source filtering

Steer retrieval to specific sources via system prompt instructions. PydanticAI passes tool args through to MCP:

```python
agent = Agent(
    "anthropic:claude-sonnet-4-5",
    mcp_servers=[omniscience],
    system_prompt=(
        "When the user asks about documentation or runbooks, call search with "
        "types=['confluence', 'fs']. "
        "When the user asks about code, call search with sources=['main-gitlab']. "
        "Always cite results."
    ),
)
```

### Freshness-aware agents

Add source freshness checking before answering time-sensitive questions:

```python
agent = Agent(
    "anthropic:claude-sonnet-4-5",
    mcp_servers=[omniscience],
    system_prompt=(
        "For questions about recent changes, call list_sources() first and check "
        "is_stale on relevant sources. If a source is stale, warn the user before "
        "answering. Then call search with max_age_seconds=3600 to limit to recent results."
    ),
)
```

### Reusing the session for multiple queries

`run_mcp_servers()` establishes the MCP connection. Keep it open for batches:

```python
async with agent.run_mcp_servers():
    questions = [
        "How does rate limiting work?",
        "What is the retry strategy for failed jobs?",
        "How are webhooks validated?",
    ]
    for q in questions:
        result = await agent.run(q)
        print(f"Q: {q}")
        print(f"A: {result.data}\n")
```

This avoids re-establishing the MCP connection for each query.

## Environment variables reference

| Variable | Description |
|---|---|
| `OMNISCIENCE_URL` | Base URL, e.g. `http://localhost:8000` |
| `OMNISCIENCE_TOKEN` | API token with `search` + `sources:read` scopes |
| `ANTHROPIC_API_KEY` | Anthropic API key (for `"anthropic:..."` models) |
| `GEMINI_API_KEY` | Gemini API key (for `"google-gla:..."` models) |

## Scope and security

Use `search` + `sources:read` scopes. Never use `admin` in agent tokens. Store `OMNISCIENCE_TOKEN` via your secret manager.

Omniscience returns `embedding_model`, `indexed_at`, and lineage on every hit. Use these in your `result_type` model's `citations` list so consumers know how fresh the data is.

## Troubleshooting

### `MCPServerHTTP` connection error

```
httpx.ConnectError: All connection attempts failed
```

- Confirm Omniscience is running: `curl http://localhost:8000/health`
- Confirm the URL is correct — `OMNISCIENCE_URL` should not end with `/mcp` (that's added by the client)
- Test MCP endpoint: `curl -H "Authorization: Bearer omni_dev_..." http://localhost:8000/mcp`

### Agent not calling Omniscience tools

Strengthen the system prompt:

```python
system_prompt=(
    "IMPORTANT: You MUST call omniscience.search() before answering any "
    "question about code or documentation. Do not use training knowledge."
)
```

### Result type validation error

If Pydantic raises a validation error on the structured output, the agent failed to populate required fields. Add field descriptions and examples to your model:

```python
class Citation(BaseModel):
    chunk_id: str = Field(description="The chunk_id from the search result hit")
    uri: str = Field(description="The citation.uri from the search result hit")
    title: str = Field(description="The citation.title from the search result hit")
    indexed_at: str = Field(description="The citation.indexed_at timestamp")
```

### Token errors

See [token troubleshooting in the Claude Code guide](claude-code.md#token-invalid-or-unauthorized).

## See also

- [LangGraph integration](langgraph.md)
- [CrewAI integration](crewai.md)
- [MCP API reference](../api/mcp.md)
- [Python client](python-client.md) — direct MCP access without PydanticAI
- [PydanticAI MCP docs](https://ai.pydantic.dev/mcp/)
