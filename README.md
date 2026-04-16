# Omniscience

Self-hosted knowledge retrieval service with an **MCP-first API**. Indexes your sources (code, docs, infra configs, tickets, wikis) and exposes them as retrieval tools to any MCP-compatible AI client — Claude Code, Cursor, Gemini, custom agents, or AI pipelines.

Retrieval-only: Omniscience returns chunks with citations, and the calling LLM synthesizes the answer. No opinionated chat, no embedded LLM, no vendor lock-in.

## Status

Pre-v0.1 — scaffolding. See [docs/roadmap.md](docs/roadmap.md).

## Quick links

- [Vision](docs/vision.md) — what Omniscience is and isn't
- [Architecture](docs/architecture.md) — system overview
- [Roadmap](docs/roadmap.md) — milestones M0 → M6
- [MCP API](docs/api/mcp.md) — tool contracts (primary interface)
- [REST API](docs/api/rest.md) — secondary interface
- [Connector framework](docs/api/connector-sdk.md) — how to add a source
- [Database schema](docs/schema.md)
- [Freshness & lineage](docs/freshness-and-lineage.md) — trust model for AI clients
- [Retrieval strategy (ADR 0004)](docs/decisions/0004-retrieval-strategy-staged.md) — hybrid → structural → GraphRAG-if-needed
- [Architecture decisions](docs/decisions/)
- Integrations: [Claude Code](docs/integrations/claude-code.md) · [Cursor](docs/integrations/cursor.md) · [multiqlti](docs/integrations/multiqlti.md) · [LangGraph](docs/integrations/langgraph.md) · [CrewAI](docs/integrations/crewai.md) · [PydanticAI](docs/integrations/pydantic-ai.md)

## License

Apache 2.0. See [LICENSE](LICENSE).
