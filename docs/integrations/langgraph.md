# Using Omniscience from LangGraph

You have LangGraph agents and want them to query Omniscience for grounded retrieval across your sources. Omniscience exposes an MCP server; LangGraph consumes MCP tools natively via `langchain-mcp-adapters`.

## Prerequisites

- Running Omniscience instance (see [quickstart](../../README.md))
- Omniscience API token with `search` + `sources:read` scopes
- Python 3.10+

If Omniscience is not yet running, follow the [deployment steps in the Claude Code guide](claude-code.md#step-1--deploy-omniscience) first.

## Step 1 — Create an API token

```bash
docker compose exec app omniscience tokens create \
  --name langgraph \
  --scopes search,sources:read
```

Copy the printed token (`omni_dev_...`).

## Step 2 — Install dependencies

```bash
pip install langgraph langchain-mcp-adapters langchain-anthropic
# or for Gemini:
# pip install langgraph langchain-mcp-adapters langchain-google-genai
```

## Step 3 — Connect and build agent

```python
import asyncio
import os

from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent
from langchain_anthropic import ChatAnthropic

OMNISCIENCE_URL = os.environ["OMNISCIENCE_URL"]
OMNISCIENCE_TOKEN = os.environ["OMNISCIENCE_TOKEN"]


async def build_agent():
    # Connect to Omniscience via MCP streamable-http
    client = MultiServerMCPClient(
        {
            "omniscience": {
                "url": f"{OMNISCIENCE_URL}/mcp",
                "transport": "streamable_http",
                "headers": {"Authorization": f"Bearer {OMNISCIENCE_TOKEN}"},
            }
        }
    )
    tools = await client.get_tools()
    # tools now includes: search, get_document, list_sources, source_stats

    agent = create_react_agent(
        model=ChatAnthropic(model="claude-sonnet-4-5"),
        tools=tools,
        prompt=(
            "You are a code-aware assistant. Use omniscience.search() to "
            "retrieve grounded context before answering. Always cite sources "
            "with chunk_id and uri."
        ),
    )
    return agent


async def main():
    agent = await build_agent()
    result = await agent.ainvoke({
        "messages": [("user", "How does authentication work in the payments service?")]
    })
    print(result["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main())
```

Set environment variables and run:

```bash
export OMNISCIENCE_URL=http://localhost:8000
export OMNISCIENCE_TOKEN=omni_dev_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export ANTHROPIC_API_KEY=your-api-key
python agent.py
```

You should see the agent invoke `omniscience.search` in its reasoning steps, then produce an answer with source citations.

## Recommended agent patterns

### Retrieval-augmented Q&A

The simplest pattern — a ReAct agent that searches on demand:

```python
agent = create_react_agent(
    model=ChatAnthropic(model="claude-sonnet-4-5"),
    tools=tools,  # includes omniscience.search
    prompt=(
        "Answer questions using omniscience.search for grounding. "
        "Cite every claim with the chunk_id and uri from the search result."
    ),
)
```

### Multi-step research with state

Use LangGraph's `StateGraph` when a single search is not enough (research → synthesize → verify):

```python
from langgraph.graph import StateGraph, START, END
from typing import TypedDict, Annotated
import operator


class ResearchState(TypedDict):
    question: str
    search_results: Annotated[list, operator.add]
    draft_answer: str
    verified: bool


async def research_node(state: ResearchState, tools: list) -> dict:
    search_tool = next(t for t in tools if t.name == "search")
    hits = await search_tool.ainvoke({
        "query": state["question"],
        "top_k": 10,
    })
    return {"search_results": [hits]}


async def synthesize_node(state: ResearchState, model) -> dict:
    context = "\n\n".join(
        f"[{h['citation']['uri']}]\n{h['text']}"
        for batch in state["search_results"]
        for h in batch.get("hits", [])
    )
    response = await model.ainvoke(
        f"Based on these sources:\n{context}\n\nAnswer: {state['question']}"
    )
    return {"draft_answer": response.content}


async def verify_node(state: ResearchState, tools: list) -> dict:
    # Re-fetch each cited document to confirm claims
    get_doc = next(t for t in tools if t.name == "get_document")
    for batch in state["search_results"]:
        for hit in batch.get("hits", []):
            await get_doc.ainvoke({"document_id": hit["document_id"]})
    return {"verified": True}


def build_research_graph(tools, model):
    graph = StateGraph(ResearchState)
    graph.add_node("research", lambda s: asyncio.run(research_node(s, tools)))
    graph.add_node("synthesize", lambda s: asyncio.run(synthesize_node(s, model)))
    graph.add_node("verify", lambda s: asyncio.run(verify_node(s, tools)))
    graph.add_edge(START, "research")
    graph.add_edge("research", "synthesize")
    graph.add_edge("synthesize", "verify")
    graph.add_edge("verify", END)
    return graph.compile()
```

### Freshness-aware retrieval

Pass `max_age_seconds` to exclude stale results:

```python
result = await search_tool.ainvoke({
    "query": "recent deployment config changes",
    "max_age_seconds": 3600,  # last hour only
    "top_k": 10,
})
```

If no results come back, the agent can fall back to a broader search without the freshness filter.

### Source-filtered retrieval

Restrict search to specific indexed sources:

```python
# Only search the main GitLab repo
result = await search_tool.ainvoke({
    "query": "error handling patterns",
    "sources": ["main-gitlab"],
    "top_k": 5,
})

# Only search documentation sources
result = await search_tool.ainvoke({
    "query": "deployment runbook",
    "types": ["confluence", "fs"],
    "top_k": 5,
})
```

## Checking source freshness before querying

```python
async def warn_if_stale(tools: list) -> None:
    list_sources_tool = next(t for t in tools if t.name == "list_sources")
    sources = await list_sources_tool.ainvoke({})
    for src in sources.get("sources", []):
        if src["is_stale"]:
            print(f"WARNING: source '{src['name']}' is stale — last synced {src['last_sync_at']}")
```

Call this before critical queries to surface freshness issues proactively.

## Scope and security

- Use a **narrowly scoped token** (`search` + `sources:read` only). Never use `admin` in an agent context.
- Store `OMNISCIENCE_TOKEN` via your secret manager, not in source code.
- Omniscience returns `embedding_model`, `indexed_at`, and `source.name` on every hit — propagate these in your agent's citations.

## Troubleshooting

### `MultiServerMCPClient` connection error

```
httpx.ConnectError: All connection attempts failed
```

- Confirm Omniscience is running: `curl http://localhost:8000/health`
- Confirm `OMNISCIENCE_URL` does not have a trailing slash
- For Docker-hosted agents, use the container's network hostname

### Agent not calling search tool

If the agent answers without using Omniscience, strengthen the system prompt:

```python
prompt=(
    "IMPORTANT: You MUST call omniscience.search() before answering any "
    "question about the codebase. Do not rely on training data for code questions."
)
```

### First call latency

MCP connection setup adds ~100ms. Reuse the `MultiServerMCPClient` across agent invocations — do not recreate it per query.

### Rate limits

Default 60 rpm per token. For high-throughput agents, request a higher limit via the admin API or create a dedicated token with a higher limit.

## See also

- [MCP API reference](../api/mcp.md)
- [Claude Code integration](claude-code.md) — same MCP, IDE-integrated
- [CrewAI integration](crewai.md) — alternative framework
- [PydanticAI integration](pydantic-ai.md) — typed output, lower ceremony
- [Python client](python-client.md) — direct MCP access without LangGraph
