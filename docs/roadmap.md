# Roadmap

## Milestone structure

| Milestone | Theme | Scope |
|---|---|---|
| **M0** | Foundation | Repo scaffold, DB schema, queue, core contracts |
| **M1** | Ingestion | Connector SDK, ingestion pipeline, parsers, chunker |
| **M2** | Index & Retrieval | Embeddings, index layer, hybrid retrieval |
| **M3** | API Surfaces | MCP server, REST API, CLI, auth |
| **M4** | Connectors v0.1 | `git`, `fs`, webhook receiver |
| **M5** | Observability | OpenTelemetry, Prometheus, structured logging |
| **M6** | Deploy & Docs | Docker Compose, Helm stub, integration guides |

## v0.1 MVP — scope

Minimum that is useful when plugged into Claude Code / Cursor:

- Index a local git repo and a local markdown folder
- Query via MCP from Claude Code and get back chunks with citations
- Freshness < 5 min after a git push (webhook) or ≤ 1 min after local fs change
- Deploy locally via `docker compose up`
- Local embeddings via Ollama (no external calls required)

## v0.2 — Team

- Connectors: Confluence, Notion, Slack, Jira/Linear
- Multi-tenant workspaces with ACL
- Web UI for admin + chat surface with citations
- OpenAPI spec + TS/Python client SDKs
- Cloud embedding providers (OpenAI, Voyage, Cohere)

## v0.3 — Enterprise depth

- Symbol graph for code (semantic layer)
- Entity extraction + cross-source linking
- Freshness SLOs + dashboards
- Federation (multi-instance knowledge sharing)
- Cross-encoder re-ranker

## v0.4 — Ecosystem

- Third-party connector marketplace
- Optional embedded inference for air-gapped deployments
- Graph query DSL

## Dependency chain (v0.1)

Issues flow top-to-bottom; items on the same row are parallelizable.

```
                    ┌───────────────────┐
                    │ ADR: language +   │
                    │ stack             │
                    └─────────┬─────────┘
                              │
                    ┌─────────▼─────────┐
                    │ Repo scaffold     │
                    │ + CI + compose    │
                    └─────────┬─────────┘
                              │
       ┌──────────────────────┼──────────────────────┐
       ▼                      ▼                      ▼
┌─────────────┐       ┌─────────────┐        ┌─────────────┐
│ DB schema   │       │ NATS queue  │        │ OTel +      │
│ + migrations│       │ framework   │        │ metrics     │
└──────┬──────┘       └──────┬──────┘        └─────────────┘
       │                     │
       └──────────┬──────────┘
                  ▼
       ┌──────────────────┐
       │ Connector SDK    │
       └─────────┬────────┘
                 │
       ┌─────────▼────────┐
       │ Ingestion        │
       │ pipeline         │
       └────┬─────────┬───┘
            │         │
            ▼         ▼
   ┌──────────────┐  ┌──────────────┐
   │ Parsers      │  │ Embeddings   │
   │ + chunkers   │  │ abstraction  │
   └───────┬──────┘  └──────┬───────┘
           │                │
           └────────┬───────┘
                    ▼
         ┌──────────────────┐
         │ Index write path │
         └─────────┬────────┘
                   ▼
         ┌──────────────────┐
         │ Retrieval svc    │
         │ (hybrid)         │
         └─────────┬────────┘
                   │
       ┌───────────┼───────────┐
       ▼           ▼           ▼
  ┌────────┐  ┌────────┐  ┌────────┐
  │ REST   │  │ MCP    │  │ Auth   │
  │ API    │  │ server │  │ tokens │
  └───┬────┘  └───┬────┘  └────────┘
      │           │
      ▼           │
  ┌────────┐      │
  │  CLI   │      │
  └────────┘      │
                  ▼
        ┌──────────────────┐
        │ git + fs         │
        │ connectors       │
        └────────┬─────────┘
                 ▼
        ┌──────────────────┐
        │ Webhook receiver │
        └────────┬─────────┘
                 ▼
        ┌──────────────────┐
        │ Docker compose + │
        │ Helm stub        │
        └────────┬─────────┘
                 ▼
        ┌──────────────────┐
        │ Integration      │
        │ guides           │
        └──────────────────┘
```

## See also

- Open issues: https://github.com/100rd/Omniscience/issues
- Milestones: https://github.com/100rd/Omniscience/milestones
