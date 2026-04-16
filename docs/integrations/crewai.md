# Using Omniscience from CrewAI

You have CrewAI agents (role-based multi-agent) and want them to query Omniscience for grounded retrieval. CrewAI supports MCP tools natively.

## Prerequisites

- Running Omniscience instance
- Omniscience API token with `search` + `sources:read` scopes
- Python 3.10+, CrewAI installed

## Install

```bash
pip install crewai crewai-tools
```

## Connect

```python
import os
from crewai import Agent, Task, Crew
from crewai_tools import MCPServerAdapter

OMNISCIENCE_URL = os.environ["OMNISCIENCE_URL"]
OMNISCIENCE_TOKEN = os.environ["OMNISCIENCE_TOKEN"]

# Connect to Omniscience via MCP
server_params = {
    "url": f"{OMNISCIENCE_URL}/mcp",
    "transport": "streamable-http",
    "headers": {"Authorization": f"Bearer {OMNISCIENCE_TOKEN}"},
}

with MCPServerAdapter(server_params) as omniscience_tools:
    # Tools: search, get_document, list_sources, source_stats
    researcher = Agent(
        role="Codebase Researcher",
        goal="Find relevant code and docs grounding for engineering questions",
        backstory="You know where to look in a large codebase.",
        tools=omniscience_tools,
        verbose=True,
    )

    writer = Agent(
        role="Technical Writer",
        goal="Synthesize findings into accurate, cited answers",
        backstory="You turn raw research into readable answers with citations.",
        verbose=True,
    )

    research_task = Task(
        description="Research how authentication works in the payments service.",
        agent=researcher,
        expected_output="Bulleted findings with chunk_ids and source paths.",
    )

    write_task = Task(
        description="Write a clear explanation from the research findings.",
        agent=writer,
        expected_output="Markdown answer with inline citations.",
        context=[research_task],
    )

    crew = Crew(agents=[researcher, writer], tasks=[research_task, write_task])
    result = crew.kickoff()
    print(result)
```

## When CrewAI fits

- **Role-based decomposition** — separate Researcher, Verifier, Writer, etc.
- **Linear task dependencies** — research → synthesize → verify → report
- **Low-ceremony multi-agent** — CrewAI is simpler than LangGraph for basic fan-out/aggregate

For complex state machines (loops, branches, human-in-the-loop), prefer [LangGraph](langgraph.md).

## Recommended patterns

### Source-specialized agents

Give different agents different tool subsets via `list_sources()` inspection, or filter at `search()` call time with `sources=[...]`:

```python
code_researcher = Agent(
    role="Code Researcher",
    goal="Find code patterns — only look in source code, not docs or tickets",
    tools=omniscience_tools,
    # Instruction to the agent:
    backstory="Always pass sources filter to search: sources=['main-gitlab']",
)
```

### Verification before committing an answer

```python
verifier = Agent(
    role="Citation Verifier",
    goal="Re-fetch each cited chunk to confirm claims",
    tools=omniscience_tools,
    backstory="You call get_document() for each citation and flag any mismatch.",
)
```

## Scope and security

Same as [LangGraph guide](langgraph.md): narrow token scopes, never ship `admin` tokens to agents, propagate lineage in outputs.

## See also

- [LangGraph integration](langgraph.md)
- [PydanticAI integration](pydantic-ai.md)
- [MCP API reference](../api/mcp.md)
