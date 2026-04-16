# Using Omniscience from LangGraph

You have LangGraph agents and want them to query Omniscience for grounded retrieval across your sources. Omniscience exposes an MCP server; LangGraph consumes MCP tools natively via `langchain-mcp-adapters`.

## Prerequisites

- Running Omniscience instance (see [quickstart](../../README.md))
- Omniscience API token with `search` + `sources:read` scopes
- Python 3.10+, LangGraph installed

## Install

```bash
pip install langgraph langchain-mcp-adapters langchain-anthropic  # or langchain-google-genai
```

## Connect

```python
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

    # Tools now include: search, get_document, list_sources, source_stats
    agent = create_react_agent(
        model=ChatAnthropic(model="claude-sonnet-4-5"),
        tools=tools,
        prompt=(
            "You are a code-aware assistant. Use omniscience.search() to "
            "retrieve grounded context before answering. Always cite sources."
        ),
    )
    return agent

# Run it
async def main():
    agent = await build_agent()
    result = await agent.ainvoke({
        "messages": [("user", "How does authentication work in the payments service?")]
    })
    print(result["messages"][-1].content)
```

## Recommended agent patterns

### Retrieval-augmented Q&A

```python
agent = create_react_agent(
    model=ChatAnthropic(model="claude-sonnet-4-5"),
    tools=tools,  # includes omniscience.search
    prompt="Answer questions using omniscience.search for grounding. Cite.",
)
```

### Multi-step research with state

Use LangGraph's `StateGraph` when a single search isn't enough (research → synthesize → verify):

```python
from langgraph.graph import StateGraph, START, END
from typing import TypedDict, Annotated
import operator

class ResearchState(TypedDict):
    question: str
    search_results: Annotated[list, operator.add]
    draft_answer: str
    verified: bool

# ... build graph with nodes that call omniscience tools
```

### Freshness-aware retrieval

Pass `max_age_seconds` to limit stale results:

```python
# Via tool call kwargs
result = await search_tool.ainvoke({
    "query": "recent deployment config changes",
    "max_age_seconds": 3600,  # last hour only
    "top_k": 10,
})
```

## Scope and security

- Use a **narrowly scoped token** (`search` + `sources:read` only). Never use `admin` in an agent context.
- Store `OMNISCIENCE_TOKEN` via your secret manager, not in code.
- Omniscience lineage data (`embedding_model`, `indexed_at`, `source.name`) is returned on every hit — propagate it in your agent's citations.

## Gotchas

- **First call latency**: MCP connection setup adds ~100ms. Reuse `client` across agent invocations.
- **Stale results**: if your sources haven't synced recently, results may be outdated. Check `list_sources()` for `is_stale` flags.
- **Rate limits**: default 60 rpm per token. For high-throughput agents, request a higher limit via token config.

## See also

- [MCP API reference](../api/mcp.md)
- [Claude Code integration](claude-code.md) — same MCP, IDE-integrated
- [CrewAI integration](crewai.md) — alternative framework
- [PydanticAI integration](pydantic-ai.md) — alternative framework
