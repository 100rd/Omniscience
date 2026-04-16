# MCP API

Primary interface to Omniscience. Designed for consumption by AI clients: Claude Code, Cursor, Gemini, multiqlti pipelines, custom agents.

## Transports

- **stdio** — for local CLI-style clients (Claude Code, Cursor)
- **streamable-http** — for hosted clients, behind Caddy TLS

## Authentication

All requests require an API token. Clients pass it as:

- **stdio**: environment variable `OMNISCIENCE_TOKEN`
- **http**: `Authorization: Bearer <token>` header

Tokens are scoped: `search`, `sources:read`, `sources:write`, `admin`. See [schema.md](../schema.md#api_tokens).

## Tools

### `search`

Primary retrieval. Hybrid vector + BM25 + filter in v0.1; additional strategies in v0.2+.

**Input**:

| Param | Type | Description |
|---|---|---|
| `query` | string | Natural-language or keyword query |
| `top_k` | int (default 10) | Max chunks to return |
| `sources` | string[] (optional) | Restrict to these source names |
| `types` | string[] (optional) | Restrict to source types (`git`, `fs`, ...) |
| `max_age_seconds` | int (optional) | Only return chunks whose `indexed_at` is within this age |
| `filters` | object (optional) | Metadata filters (`language=python`, `path_prefix=apps/server/`, ...) |
| `include_tombstoned` | bool (default false) | Include removed documents |
| `retrieval_strategy` | enum (default `"hybrid"`) | `"hybrid"` (v0.1), `"structural"`, `"keyword"`, `"auto"` — see below |

### Retrieval strategies

The `retrieval_strategy` parameter lets the caller choose how retrieval works. In v0.1, only `"hybrid"` is implemented; other values are part of the v0.2 plan and documented here for contract stability. See [ADR 0004](../decisions/0004-retrieval-strategy-staged.md) for rationale.

| Value | Behavior | Status |
|---|---|---|
| `"hybrid"` (default) | Vector (pgvector HNSW) + BM25 (tsvector), merged via reciprocal rank fusion. Good for ~70–80% of typical queries | v0.1 |
| `"structural"` | Graph-first. Interpret query as "find entities and traverse" using the structural graph (code imports, infra DEPENDS_ON, doc cross-refs). Falls back to hybrid if graph finds nothing | v0.2 |
| `"keyword"` | BM25-only. For exact-name lookup (function names, error strings, service names) | v0.1 (via `filters` today; explicit strategy in v0.2) |
| `"auto"` | A lightweight classifier picks the strategy for you. Use this when you don't want to reason about query shape | v0.2 |

The **caller is often best-placed to choose**: a code-aware agent asking *"what depends on X"* knows to pass `"structural"`. `"auto"` exists for callers that don't want to decide.

In v0.1, requests with `retrieval_strategy` other than `"hybrid"` are accepted with a warning and downgraded to `"hybrid"`. This preserves the API contract so clients written for v0.2 continue to work against v0.1 deployments.

**Output**:

```json
{
  "hits": [
    {
      "chunk_id": "uuid",
      "document_id": "uuid",
      "score": 0.87,
      "text": "...",
      "source": {
        "id": "uuid",
        "name": "main-gitlab",
        "type": "git"
      },
      "citation": {
        "uri": "https://github.com/org/repo/blob/abc123/apps/server/auth.py#L42-L60",
        "title": "auth.py",
        "indexed_at": "2026-04-16T10:32:15Z",
        "doc_version": 7
      },
      "lineage": {
        "ingestion_run_id": "ir_01HXYZ...",
        "embedding_model": "text-embedding-004",
        "embedding_provider": "google-ai",
        "parser_version": "treesitter-python-0.21+oms-0.4.2",
        "chunker_strategy": "code_symbol"
      },
      "metadata": {
        "language": "python",
        "symbol": "authenticate_token",
        "line_range": [42, 60]
      }
    }
  ],
  "query_stats": {
    "total_matches_before_filters": 142,
    "vector_matches": 85,
    "text_matches": 97,
    "duration_ms": 34
  }
}
```

### `get_document`

Retrieve a full document (all chunks) by id.

**Input**: `{ "document_id": "uuid" }`

**Output**: `{ document, chunks[] }` — same shape as individual hits, concatenated.

### `list_sources`

List configured sources with freshness.

**Output**:

```json
{
  "sources": [
    {
      "id": "uuid",
      "name": "main-gitlab",
      "type": "git",
      "status": "active",
      "last_sync_at": "2026-04-16T10:32:15Z",
      "freshness_sla_seconds": 300,
      "is_stale": false,
      "indexed_document_count": 2341
    }
  ]
}
```

### `source_stats`

Per-source details.

**Input**: `{ "source_id": "uuid" }`

**Output**: counts, freshness, recent errors, last ingestion run.

## Errors

All tools return standard MCP error objects. Notable codes:

| Code | Meaning |
|---|---|
| `unauthorized` | Token missing / invalid / expired |
| `forbidden` | Token lacks required scope |
| `rate_limited` | Too many requests (429-equivalent) |
| `source_not_found` | Requested source id doesn't exist |
| `embedding_provider_unavailable` | Can't embed query — retry later |
| `internal` | Unexpected failure (check logs) |

## Streaming

`search` supports streaming results over `streamable-http`. Hits arrive as they're computed. Useful for AI clients that want to start reasoning with the top-1 hit before full top-K is ready.

## Connecting from clients

- [Claude Code](../integrations/claude-code.md)
- [Cursor](../integrations/cursor.md)
- [multiqlti](../integrations/multiqlti.md)
- [Custom agent (Python)](../integrations/python-client.md) (v0.2)

## Versioning

MCP API is **v0** until v0.2. Breaking changes allowed. After v0.2, semver applies.
