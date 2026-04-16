# ADR 0001 — Language and core stack

- **Status**: Accepted
- **Date**: 2026-04-16
- **Supersedes**: none

## Context

Omniscience is a retrieval service whose core work is **parsing**, **chunking**, **embedding**, and **indexing** text from heterogeneous sources. It exposes results via an **MCP-first API** to AI clients.

Three viable stacks were considered:

1. **Python 3.12** — `fastapi`, `FastMCP`, `sqlalchemy`, `pgvector`, `tree-sitter`, `langchain-text-splitters`, `nats-py`
2. **TypeScript / Node** — `@modelcontextprotocol/sdk`, `drizzle-orm`, `tree-sitter` bindings
3. **Go core + Python workers** — Go for API, Python over gRPC for parsing/embedding

## Decision

**Python 3.12** is the primary language.

## Rationale

- **Ingestion ecosystem** — `tree-sitter-languages`, `unstructured`, `langchain-text-splitters`, `sentence-transformers`, `fastembed` — are all Python-native. This is the bulk of the work.
- **MCP SDK parity** — the official `mcp` Python package with `FastMCP` is on par with the TypeScript SDK; both are first-class.
- **Single runtime** — no Python sidecar needed, simpler deploy.
- **Modern Python UX** — `uv` (Astral) gives fast, reproducible installs; `ruff` gives format + lint at rust speed; `mypy --strict` gives type safety. None of the historic Python pain points apply.
- **Postgres + SQLAlchemy 2 + pgvector** — proven, typed, asynchronous capable.

## Components

| Concern | Choice |
|---|---|
| Language | Python 3.12+ |
| Package / task manager | `uv` (Astral) |
| Web framework | `fastapi` |
| MCP server | `mcp` SDK (`FastMCP`) |
| ORM + migrations | SQLAlchemy 2 + Alembic |
| Storage | PostgreSQL 16 + pgvector |
| Queue | NATS JetStream (client: `nats-py`) |
| Code parsing | `tree-sitter` via `tree-sitter-languages` |
| Markdown / docs parsing | `markdown-it-py`, `python-frontmatter` |
| Generic fallback parser | `unstructured` (optional, heavy, pulled in when needed) |
| Chunking | `langchain-text-splitters` (pure strategy lib) |
| Embeddings | Ollama (default), OpenAI, Voyage, Cohere (pluggable) |
| Lint / format | `ruff` |
| Type checking | `mypy --strict` |
| Tests | `pytest`, `pytest-asyncio`, `httpx` |
| Logging | `structlog` (JSON) |
| Metrics | `prometheus-client` |
| Tracing | OpenTelemetry (`opentelemetry-*`) |
| Container build | Multi-stage Dockerfile (slim base) |
| Orchestration | Docker Compose (dev/single-node) + Helm (k8s) |

## Alternatives rejected

### TypeScript / Node

Rejected as primary because:

- `tree-sitter` native bindings in Node are fragile (node-gyp pain, prebuilt binaries often lag)
- No equivalent to `unstructured.io` for generic document parsing
- No equivalent to `fastembed` / `sentence-transformers` for local embeddings
- Embedding provider libraries are thinner

Still a reasonable choice if the team's skill is exclusively TS. For Omniscience, Python removes more friction.

### Go core + Python workers

Rejected because:

- Two runtimes double ops overhead for no clear MVP benefit
- gRPC boundary adds serialization cost and schema maintenance
- Language context-switch for contributors

Reconsider at v0.3+ if API layer becomes a bottleneck.

## Consequences

- Contributors must be comfortable with typed Python (mypy --strict) + async (asyncio/anyio)
- Docker images built from `python:3.12-slim` base; system deps for tree-sitter (`build-essential`) added in builder stage only
- No JavaScript anywhere in `apps/server` or `packages/*` (frontend, when added in v0.2, is a separate app)

## Revisit

Re-evaluate at v0.3 against actual performance data. If API layer latency or throughput becomes a bottleneck, consider a Go rewrite of `apps/server` while keeping workers in Python.
