# Project: Omniscience

## Overview

Self-hosted knowledge retrieval service with an MCP-first API. Indexes sources (code, docs, infra configs, tickets) and exposes them as retrieval tools to any MCP-compatible AI client.

## Repository

- **Remote**: `github.com/100rd/Omniscience`
- **Local**: `project/Omniscience/`
- **Issue tracking**: GitHub Issues + Milestones
- **Epic**: [#22 — v0.1 MVP](https://github.com/100rd/Omniscience/issues/22)

## Stack

- **Language**: Python 3.12+
- **Package manager**: `uv` (Astral)
- **Web framework**: FastAPI
- **MCP server**: `mcp` SDK (FastMCP)
- **ORM**: SQLAlchemy 2 + Alembic
- **Database**: PostgreSQL 16 + pgvector
- **Queue**: NATS JetStream
- **Parsing**: tree-sitter, markdown-it-py
- **Embeddings**: Ollama (default), OpenAI/Voyage (pluggable)
- **Observability**: OpenTelemetry + Prometheus + structlog
- **Lint/Format**: ruff
- **Type checking**: mypy --strict
- **Tests**: pytest + pytest-asyncio
- **Deploy**: Docker Compose (dev) + Helm (k8s)

## Milestones (v0.1 MVP)

| # | Milestone | Issues | Status |
|---|-----------|--------|--------|
| 1 | M0 – Foundation | #1, #3, #4 | Not started |
| 2 | M1 – Ingestion | #2, #5, #6, #7, #8, #9 | Not started |
| 3 | M2 – Index & Retrieval | #10, #11, #12, #13 | Not started |
| 4 | M3 – API Surfaces | #14, #15, #16 | Not started |
| 5 | M4 – Connectors v0.1 | #17, #18, #19 | Not started |
| 6 | M5 – Observability | #4 (shared with M0) | Not started |
| 7 | M6 – Deploy & Docs | #20, #21 | Not started |

## Dependency Waves

```
Wave 1: #1 (Scaffold) + #4 (Observability)
Wave 2: #2 (DB schema) + #3 (NATS) + #10 (Embeddings)
Wave 3: #5 (Connector SDK) + #12 (Auth)
Wave 4: #6 (Ingestion pipeline) + #11 (Index writer)
Wave 5: #7 (Parser framework) + #8 (Tree-sitter) + #9 (Chunker)
Wave 6: #13 (Hybrid retrieval)
Wave 7: #14 (MCP) + #15 (REST) + #16 (CLI)
Wave 8: #17 (git connector) + #18 (fs connector) + #19 (Webhooks)
Wave 9: #20 (Deploy) + #21 (Integration guides)
```

## Development

- **Local dev**: `docker compose up`
- **Tests**: `make test`
- **Lint**: `make lint`
- **Format**: `make fmt`

## Key Docs

- [Vision](docs/vision.md)
- [Architecture](docs/architecture.md)
- [Roadmap](docs/roadmap.md)
- [Schema](docs/schema.md)
- [MCP API](docs/api/mcp.md)
- [REST API](docs/api/rest.md)
- [ADRs](docs/decisions/)
