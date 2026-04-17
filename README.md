# Omniscience

Self-hosted knowledge retrieval service with an **MCP-first API**. Indexes your sources (code, docs, infra configs, tickets, wikis) and exposes them as retrieval tools to any MCP-compatible AI client — Claude Code, Cursor, Gemini, custom agents, or AI pipelines.

Retrieval-only: Omniscience returns chunks with citations, and the calling LLM synthesizes the answer. No opinionated chat, no embedded LLM, no vendor lock-in.

## Status

Pre-v0.1 — scaffolding. See [docs/roadmap.md](docs/roadmap.md).

## Getting Started

Get Omniscience running and connected to your AI client in three steps.

**Step 1 — Start the stack**

```bash
cat > .env << 'EOF'
POSTGRES_PASSWORD=change-me-strong-password
OMNISCIENCE_SECRET_KEY=change-me-32-char-secret-key-here
EOF

docker compose up -d
```

Wait for all services to become healthy, then verify:

```bash
curl http://localhost:8000/health
# {"status":"ok","version":"0.1.0"}
```

**Step 2 — Create an API token**

```bash
docker compose exec app omniscience tokens create \
  --name my-client \
  --scopes search,sources:read
# Created token: sk_live_...  (save this — shown once)
```

**Step 3 — Connect your AI client**

Add this to your client's MCP config:

```json
{
  "mcpServers": {
    "omniscience": {
      "command": "omniscience",
      "args": ["mcp", "serve", "--transport", "stdio"],
      "env": {
        "OMNISCIENCE_URL": "http://localhost:8000",
        "OMNISCIENCE_TOKEN": "sk_live_..."
      }
    }
  }
}
```

Then ask your AI assistant a question — it will call `omniscience.search` and return grounded answers with citations.

## Integration guides

| Client | Guide |
|---|---|
| Claude Code | [docs/integrations/claude-code.md](docs/integrations/claude-code.md) |
| Cursor | [docs/integrations/cursor.md](docs/integrations/cursor.md) |
| Gemini CLI / SDK | [docs/integrations/gemini.md](docs/integrations/gemini.md) |
| multiqlti pipelines | [docs/integrations/multiqlti.md](docs/integrations/multiqlti.md) |
| Python (direct MCP client) | [docs/integrations/python-client.md](docs/integrations/python-client.md) |
| LangGraph agents | [docs/integrations/langgraph.md](docs/integrations/langgraph.md) |
| CrewAI agents | [docs/integrations/crewai.md](docs/integrations/crewai.md) |
| PydanticAI agents | [docs/integrations/pydantic-ai.md](docs/integrations/pydantic-ai.md) |

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

## License

Apache 2.0. See [LICENSE](LICENSE).
