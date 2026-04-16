# ADR 0003 — Agent framework: LangGraph primary, CrewAI + PydanticAI as adapters

- **Status**: Accepted
- **Date**: 2026-04-17
- **Supersedes**: none

## Context

Omniscience's retrieval path is deterministic — no agents, no LLMs in hot path. However, some connectors cannot be expressed declaratively and require **LLM-driven decisions during discovery**: what to index, how to interpret, when to stop. Examples:

- **k8s** — which resources? Deployments and ConfigMaps yes; transient events no; Secrets never. Heuristics leak; an LLM that understands the cluster decides better
- **Databases** — index schemas, comments, saved queries; skip transactional data. Which tables are reference data vs transactional?
- **Selective log ingestion** — ingest error/warn only from specific services; summarize rather than embed raw lines

We call these **AgenticConnectors**. They need an agent framework.

### Constraints

- Target stack: Gemini + Claude (mixed provider setup, no single-vendor alignment)
- No tight coupling to Google or Anthropic ecosystems
- Must support MCP tools natively (we already expose MCP tools internally — e.g., k8s API via MCP server)
- Must support streaming, structured output, max-iterations guards
- Must be reasonably mature (production adoption, not pre-alpha)

### Candidates evaluated

| Framework | Verdict |
|---|---|
| **LangGraph** | ✅ chosen primary |
| **CrewAI** | ✅ chosen as v0.2 adapter |
| **PydanticAI** | ✅ chosen as v0.2 adapter |
| Google ADK | ❌ Gemini-biased, not optimal for mixed stack |
| AutoGen | ❌ Microsoft-biased ecosystem; weaker tool interop |
| Self-rolled loop | ❌ reinvents the wheel; sufficient for a PoC but not for sustained development |

## Decision

### v0.1 (now)

**LangGraph** is the primary and only supported agent framework for AgenticConnector. Reasons:

- **Mature and battle-tested** — 2+ years, large community, extensive documentation
- **Vendor-neutral** — `ChatGoogleGenerativeAI`, `ChatAnthropic` work identically; Gemini + Claude use is first-class
- **Stateful graphs** — right abstraction for AgenticConnector (branching decisions, retry loops, tool use)
- **MCP tools supported** via `langchain-mcp-adapters`
- **Streaming + structured output** built in
- **Human-in-the-loop** primitives if needed later for approval gates

### v0.2

Introduce an `AgentRunner` abstraction with adapters for:

- **CrewAI** — for role-based multi-agent scenarios (e.g., Researcher + Curator + Verifier for entity extraction)
- **PydanticAI** — for simpler, typed, low-overhead loops where LangGraph is overkill

Adapters translate a unified `AgentConfig` + `MCPTool[]` into each framework's native form. Each adapter is ~150–250 LOC.

## Why staged

Building three framework adapters simultaneously in v0.1 would:

- Triple testing matrix before we understand what the common abstraction should be
- Delay MVP by ~1.5–2 weeks
- Lock us into an abstraction that real-world use might reveal as wrong

Staging means: get LangGraph working end-to-end, build 2–3 AgenticConnectors on it, **then** extract the abstraction from observed patterns. Classic "prefer concrete over abstract until you have three examples" rule.

## LLM provider abstraction

Independent of agent framework choice, we keep an `LLMProvider` abstraction:

```python
class LLMProvider(Protocol):
    name: str                      # "gemini", "anthropic", "ollama"
    default_model: str

    def langgraph_chat_model(self) -> BaseChatModel: ...
    def crewai_llm(self) -> LLM: ...              # when CrewAI adapter lands
    def pydantic_ai_model(self) -> Model: ...     # when PydanticAI adapter lands
```

This means an AgenticConnector can say "use Gemini Flash" or "use Claude Sonnet" and the LangGraph-powered implementation picks up the right model object. When adapters land, the same `LLMProvider` feeds CrewAI and PydanticAI too.

## Default models

For AgenticConnector LLM calls:

- **Default**: `claude-sonnet-4-5` (strong tool-use, good reasoning/cost balance)
- **Cost-optimized**: `gemini-2.5-flash`
- **Local / air-gapped**: `ollama:llama-3.1` (reduced capability, documented)

Configurable per-connector + per-workspace.

## Layer B: external framework use is free

This ADR covers **Layer A**: frameworks used *inside* Omniscience. For **Layer B** — users calling Omniscience *from* their LangGraph/CrewAI/PydanticAI code — nothing to build. MCP is the standard; all three frameworks support MCP tools natively. We only ship integration guides. See [docs/integrations/](../integrations/).

## Revisit triggers

- LangGraph has a sustained period (6+ months) of breaking changes or community decline
- v0.2 reveals that CrewAI/PydanticAI adapters don't cover actual user needs
- A compelling new agent framework emerges with substantially better fit (unlikely in a 1-year horizon)
- User feedback shows more demand for agent framework X than our adapter matrix predicts

## Dependencies added

```
langgraph             ~=0.2        # v0.1
langchain-google-genai ~=2.0       # v0.1 — Gemini chat model
langchain-anthropic   ~=0.2        # v0.1 — Claude chat model
langchain-mcp-adapters ~=0.1       # v0.1 — MCP tools into LangGraph
crewai                ~=0.100      # v0.2 — adapter
pydantic-ai           ~=0.0.20     # v0.2 — adapter
```

Exact versions pinned in `pyproject.toml` when the AgenticConnector issue lands.
