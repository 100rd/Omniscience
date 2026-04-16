# Architecture

## System overview

```
                           ┌──────────────────────────────────┐
                           │  AI clients                       │
                           │  Claude Code · Cursor · Gemini    │
                           │  multiqlti · custom agents        │
                           └──────────────┬───────────────────┘
                                          │ MCP (stdio / streamable-http)
                                          │ REST (secondary)
                           ┌──────────────▼───────────────────┐
                           │         API Gateway               │
                           │  MCP server  │  REST + Auth       │
                           └──────────────┬───────────────────┘
                                          │
                           ┌──────────────▼───────────────────┐
                           │      Retrieval Service            │
                           │  hybrid: vector + tsvector +      │
                           │  graph traversal + ACL filter +   │
                           │  freshness filter                 │
                           └──────────────┬───────────────────┘
                                          │
                ┌─────────────────────────▼─────────────────────┐
                │                 Index Layer                    │
                │  pgvector · tsvector · symbol graph ·          │
                │  tombstones · content-hash dedup               │
                └─────────────────────────┬─────────────────────┘
                                          │
                ┌─────────────────────────▼─────────────────────┐
                │            Ingestion Pipeline                  │
                │  queue(NATS) → parser → chunker → embedder →  │
                │  index-writer  +  DLQ + retry                  │
                └─────────────────────────┬─────────────────────┘
                                          │
                ┌─────────────────────────▼─────────────────────┐
                │          Source Connector Framework            │
                │  git · fs · Confluence · Notion · Slack ·     │
                │  Jira/Linear · Grafana · ArgoCD · k8s · tf-state│
                │  (push via webhooks · pull via polling)        │
                └────────────────────────────────────────────────┘
```

## Components

### 1. Source connectors (`packages/connectors/`)

Pluggable adapters implementing a small interface. Each connector:

- Discovers documents in its source
- Emits change events (full list on first run, incremental after)
- Provides per-document content + metadata
- Handles source-specific auth (token, OAuth, service account)

Built-in for v0.1: `git`, `fs`. Next: Confluence, Notion, Slack, Jira, Grafana, k8s, Terraform state.

### 2. Ingestion pipeline (`apps/server/`, `packages/parsers/`)

Event-driven via NATS JetStream. Stages:

1. **Change detector** — connector emits `document.changed` with source_id + external_id
2. **Fetcher** — pulls content from source
3. **Parser** — source-type-aware (tree-sitter for code, markdown parsers for docs)
4. **Chunker** — strategy-per-source (function/class for code, section/heading for docs)
5. **Embedder** — provider-pluggable (Ollama default, OpenAI/Voyage optional)
6. **Indexer** — writes chunks + embeddings + metadata

Failures flow to DLQ. Retries are bounded with exponential backoff. Freshness SLO per source defines when staleness alerts fire.

### 3. Index layer (`packages/index/`)

PostgreSQL-backed:

- `documents` — one row per source doc, with `content_hash` and `tombstoned_at`
- `chunks` — one row per chunk, with `embedding vector`, `text_tsv`, `metadata jsonb`
- HNSW index on embeddings (cosine)
- GIN index on tsvector (full-text)

Single source of truth. No separate vector DB.

### 4. Retrieval service (`packages/retrieval/`)

Hybrid search:

1. **Vector** — pgvector HNSW top-K
2. **BM25-like** — tsvector ranking
3. **Merge** — reciprocal rank fusion
4. **Filter** — ACL, source subset, freshness cap
5. **Re-rank** (v0.3) — cross-encoder optional

Returns chunks with citations and provenance.

### 5. API surfaces (`apps/server/`)

Two transports:

- **MCP server** (primary) — stdio + streamable-http. Tools: `search`, `get_document`, `list_sources`, `source_stats`.
- **REST** (secondary) — `/search`, `/sources`, `/documents/:id`, `/ingest/webhook/:source`, `/health`.

Both hit the same retrieval service.

### 6. CLI (`apps/cli/`)

`omniscience` command for operators: source management, manual reindex, ad-hoc search, status.

## Deployment

Single `docker-compose.yml` brings up:

- `app` — Omniscience server (FastAPI + FastMCP + ingestion workers)
- `postgres` with pgvector
- `nats` JetStream
- `ollama` (optional — if using local embeddings)
- `caddy` — TLS termination

Helm chart available for Kubernetes.

### Managed Postgres

Nothing in Omniscience requires the built-in Postgres. Any Postgres 14+ with pgvector works:

- **AWS RDS for PostgreSQL** — pgvector available as a managed extension
- **Google Cloud SQL** — pgvector extension supported
- **Supabase / Neon / Crunchy Bridge** — pgvector first-class
- **Aurora PostgreSQL** — pgvector supported

Set `DATABASE_URL` to the external instance; drop the `postgres` service from Compose. Daily `pg_dump` backup sidecar can be similarly disabled in favor of the managed provider's backup mechanism.

## Agent layer (for AgenticConnector only)

Most of Omniscience is deterministic. The exception is **AgenticConnector** — a connector variant whose `discover()` phase uses an LLM to decide what to index (see [ADR 0003](decisions/0003-agent-framework-langgraph-primary.md)).

- **v0.1**: LangGraph, with pluggable LLM provider (Gemini, Claude, Ollama)
- **v0.2**: CrewAI and PydanticAI adapters added
- **Layer B** (external users calling Omniscience from their agent code): uses MCP directly, no Omniscience-side abstraction — see [integrations/](integrations/)

## Data flow: a source update

```
GitHub push webhook
       │
       ▼
┌──────────────┐     ┌──────────────┐     ┌─────────────┐
│ REST webhook │────▶│  NATS stream │────▶│  Ingestion  │
│  receiver    │     │  git.events  │     │   worker    │
└──────────────┘     └──────────────┘     └──────┬──────┘
                                                  │
                     ┌────────────────────────────┴──────────┐
                     │                                        │
                     ▼                                        ▼
              ┌─────────────┐                         ┌─────────────┐
              │  Fetch diff │                         │ Determine    │
              │  vs last    │                         │ affected     │
              │  indexed    │                         │ docs         │
              └──────┬──────┘                         └──────┬──────┘
                     │                                        │
                     └────────────────────┬───────────────────┘
                                          ▼
                               ┌──────────────────┐
                               │  Parse → chunk → │
                               │  embed → index   │
                               └────────┬─────────┘
                                        │
                                        ▼
                               ┌──────────────────┐
                               │  Content-hash    │
                               │  dedup           │
                               └────────┬─────────┘
                                        │
                                        ▼
                               ┌──────────────────┐
                               │  Upsert + emit   │
                               │  `doc.indexed`   │
                               └──────────────────┘
```

## Data flow: a search query

```
MCP client (Claude Code)
         │  search(query="how does auth work", topK=10, max_age=3600)
         ▼
┌──────────────────┐
│  MCP server      │ authenticate token, derive ACL
└────────┬─────────┘
         ▼
┌──────────────────┐
│ Retrieval service│
│  1. embed(query) │
│  2. vector top-K │──┐
│  3. tsvector rank│──┤
│  4. RRF merge    │◀─┘
│  5. ACL filter   │
│  6. freshness    │
│     filter       │
└────────┬─────────┘
         ▼
┌──────────────────┐
│  Response        │
│  [chunk, ...]    │  each with: text, score, source, uri,
│                  │             indexed_at, doc_version
└──────────────────┘
```

## Multi-tenancy (v0.2+)

Workspaces (tenants) separate at the query and ingestion level. Each source belongs to a workspace; each API token is scoped. Single-tenant MVP hides this behind a default workspace.

## See also

- [Schema](schema.md)
- [MCP API](api/mcp.md)
- [Connector SDK](api/connector-sdk.md)
- [ADR 0001: Language & stack](decisions/0001-language-and-stack.md)
