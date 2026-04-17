# Using Omniscience from CrewAI

You have CrewAI agents (role-based multi-agent) and want them to query Omniscience for grounded retrieval. CrewAI supports MCP tools natively via `crewai-tools`.

## Prerequisites

- Running Omniscience instance
- Omniscience API token with `search` + `sources:read` scopes
- Python 3.10+

If Omniscience is not yet running, follow the [deployment steps in the Claude Code guide](claude-code.md#step-1--deploy-omniscience) first.

## Step 1 — Create an API token

```bash
docker compose exec app omniscience tokens create \
  --name crewai \
  --scopes search,sources:read
```

Copy the printed token (`omni_dev_...`).

## Step 2 — Install dependencies

```bash
pip install crewai crewai-tools
```

## Step 3 — Connect Omniscience and define your crew

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
        goal="Find relevant code and documentation to ground answers to engineering questions",
        backstory=(
            "You are an expert at searching large codebases. You always use "
            "omniscience.search() with targeted queries and cite every finding "
            "with chunk_id and URI."
        ),
        tools=omniscience_tools,
        verbose=True,
    )

    writer = Agent(
        role="Technical Writer",
        goal="Synthesize research findings into accurate, well-cited answers",
        backstory=(
            "You turn raw research into clear, readable answers. You include "
            "inline citations from the researcher's chunk_ids."
        ),
        verbose=True,
    )

    research_task = Task(
        description="Research how authentication works in the payments service.",
        agent=researcher,
        expected_output=(
            "Bulleted list of findings. Each bullet must include: "
            "chunk_id, source URI, and a one-line summary."
        ),
    )

    write_task = Task(
        description=(
            "Write a clear technical explanation of authentication in the "
            "payments service, based on the research findings."
        ),
        agent=writer,
        expected_output="Markdown answer with inline citations (chunk_id + URI).",
        context=[research_task],
    )

    crew = Crew(
        agents=[researcher, writer],
        tasks=[research_task, write_task],
        verbose=True,
    )
    result = crew.kickoff()
    print(result)
```

Set environment variables and run:

```bash
export OMNISCIENCE_URL=http://localhost:8000
export OMNISCIENCE_TOKEN=omni_dev_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export ANTHROPIC_API_KEY=your-api-key  # or OPENAI_API_KEY, etc.
python crew.py
```

You should see CrewAI print tool calls like `[research] Calling omniscience.search(...)` as the researcher agent retrieves context, followed by a final synthesis from the writer agent.

## When CrewAI fits

- **Role-based decomposition** — separate Researcher, Verifier, Writer, Architect roles
- **Linear task dependencies** — research → synthesize → verify → report
- **Low-ceremony multi-agent** — CrewAI is simpler than LangGraph for straightforward pipelines

For complex state machines (loops, branches, human-in-the-loop), prefer [LangGraph](langgraph.md).

## Recommended patterns

### Source-specialized agents

Give different agents different retrieval focus by steering via `backstory` and task description:

```python
code_researcher = Agent(
    role="Code Researcher",
    goal="Find code patterns — focus on source code, not docs or tickets",
    tools=omniscience_tools,
    backstory=(
        "Always pass sources=['main-gitlab'] to search calls. "
        "You are looking for code, not documentation."
    ),
)

docs_researcher = Agent(
    role="Documentation Researcher",
    goal="Find documentation, runbooks, and architecture decisions",
    tools=omniscience_tools,
    backstory=(
        "Always pass types=['confluence', 'fs'] to search calls. "
        "You are looking for written documentation, not code."
    ),
)
```

### Citation verifier

Add a verifier agent that re-fetches cited documents to confirm claims:

```python
verifier = Agent(
    role="Citation Verifier",
    goal="Confirm that cited chunks actually support the claimed findings",
    tools=omniscience_tools,
    backstory=(
        "For each cited chunk_id, call get_document() and verify that "
        "the text in the research output matches what is actually there. "
        "Flag any discrepancy."
    ),
)

verify_task = Task(
    description="Verify all citations from the research task.",
    agent=verifier,
    expected_output="List of verified citations (PASS) and discrepancies (FAIL).",
    context=[research_task],
)
```

### Freshness check before research

Have the first task check source staleness before proceeding:

```python
freshness_task = Task(
    description=(
        "Call omniscience.list_sources() and report any sources where "
        "is_stale=true. If critical sources are stale, stop and report "
        "instead of proceeding with research."
    ),
    agent=researcher,
    expected_output="Source freshness report. Proceed=true if all relevant sources are fresh.",
)

research_task = Task(
    description="Research authentication in the payments service.",
    agent=researcher,
    context=[freshness_task],
    expected_output="Bulleted findings with citations.",
)
```

## Scope and security

Same as other guides: `search` + `sources:read` scopes only. Never pass `admin` to agents. Store `OMNISCIENCE_TOKEN` via your secrets manager.

Omniscience returns `embedding_model`, `indexed_at`, and source lineage on every hit. Propagate these fields in the crew's output artifacts so reviewers can assess freshness.

## Troubleshooting

### `MCPServerAdapter` connection error

- Confirm Omniscience is running: `curl http://localhost:8000/health`
- Confirm the URL does not have a trailing slash
- Test the MCP endpoint directly: `curl -H "Authorization: Bearer omni_dev_..." http://localhost:8000/mcp`

### Agent not using Omniscience tools

If the agent answers without calling tools, strengthen its backstory:

```python
backstory=(
    "IMPORTANT: You MUST call omniscience.search() for every question. "
    "You are not allowed to answer from memory alone."
)
```

### Results are stale

Check freshness before searching:

```python
list_sources_tool = [t for t in omniscience_tools if t.name == "list_sources"][0]
sources = list_sources_tool.run({})
```

Trigger a sync if needed:

```bash
curl -X POST -H "Authorization: Bearer omni_dev_..." \
  http://localhost:8000/api/v1/sources/<source-id>/sync
```

## See also

- [LangGraph integration](langgraph.md)
- [PydanticAI integration](pydantic-ai.md)
- [MCP API reference](../api/mcp.md)
- [Python client](python-client.md) — direct MCP access without CrewAI
