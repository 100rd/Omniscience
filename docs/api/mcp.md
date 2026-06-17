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
| `as_of` | ISO-8601 datetime (optional) | Anchor results to graph state at this time. See **Bitemporal `as_of` parameter** below. |

### Retrieval strategies

The `retrieval_strategy` parameter lets the caller choose how retrieval works. In v0.1, only `"hybrid"` is implemented; other values are part of the v0.2 plan and documented here for contract stability. See [ADR 0004](../decisions/0004-retrieval-strategy-staged.md) for rationale.

| Value | Behavior | Status |
|---|---|---|
| `"hybrid"` (default) | Vector (Qdrant HNSW) + BM25 (tsvector), merged via reciprocal rank fusion. Good for ~70–80% of typical queries | v0.1 |
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

### `get_entity`

Resolve a single entity by name within the caller's workspace. Workspace-scoped token required.

**Input**:

| Param | Type | Description |
|---|---|---|
| `entity_name` | string | Fully-qualified entity name |
| `as_of` | ISO-8601 datetime (optional) | Return the entity's state at this time. See **Bitemporal `as_of` parameter** below. |

**Output**:

```json
{
  "entity": {
    "name": "svc.ratings",
    "kind": "deployed",
    "source": "uuid",
    "chunk_text": "...",
    "valid_from": "2026-04-12T08:00:00Z",
    "valid_to": null,
    "recorded_at": "2026-04-12T08:00:00Z"
  },
  "effective_as_of": "2026-04-12T19:25:00Z",
  "meta": null
}
```

When the entity cannot be resolved at the supplied `as_of` (pre-history), `entity` is `null` and `meta.degraded_response = "as_of_before_recorded_history"`.

### `get_related_entities`

Traverse the entity graph from a named entity. Workspace-scoped token required.

**Input**:

| Param | Type | Description |
|---|---|---|
| `entity_name` | string | Seed entity name |
| `max_depth` | int (default 1) | Maximum BFS hops |
| `edge_types` | string[] (optional) | Allowlist of edge types to follow |
| `as_of` | ISO-8601 datetime (optional) | Anchor traversal to graph state at this time. See **Bitemporal `as_of` parameter** below. |

**Output** mirrors the REST `/entities/{name}/related` shape and adds `effective_as_of` plus the optional `meta` block.

### `resolve_incident`

Compose a recommendation bundle for a single alert: the alert itself, the affected target resource (pod / ARN / service), the responsible PR (when one is reachable in the graph), the matching Slack discussions, and a v0.1 confidence score. Workspace-scoped token required.

**Input**:

| Param | Type | Description |
|---|---|---|
| `alert_id` | string | Canonical alert URI of the form `alert://{provider}/{provider_alert_id}`. Other forms return `invalid_alert_id`. |
| `max_depth` | int (default 2) | BFS depth from the alert seed; clamped to `[1, 5]`. |
| `as_of` | ISO-8601 datetime (optional) | Anchor traversal to graph state at this time. See **Bitemporal `as_of` parameter** below. |

**Output**:

```json
{
  "alert": {
    "name": "alert://pagerduty/INC-123",
    "kind": "alert",
    "source": "src-alerts",
    "chunk_text": "...",
    "valid_from": "2026-04-12T19:30:00Z"
  },
  "target_resource": {
    "name": "pod/api-7f9b",
    "kind": "cross_ref_target",
    "source": "src-alerts",
    "edge_type": "FIRES_AGAINST"
  },
  "responsible_pr": {
    "name": "https://github.com/acme/api/pull/42",
    "kind": "pull_request",
    "source": "src-github",
    "edge_type": "DEPLOYED_BY",
    "merged_at": "2026-04-12T17:30:00Z"
  },
  "slack_threads": [
    {
      "name": "slack://channel/C0001/thread/1700000000.000100",
      "kind": "slack_thread",
      "source": "src-slack",
      "chunk_text": "...thread excerpt...",
      "edge_type": "DISCUSSED_IN"
    }
  ],
  "confidence_score": 0.9,
  "effective_as_of": "2026-04-12T19:30:00Z",
  "meta": null
}
```

**Confidence score** (v0.1 placeholder; calibrated model lands in [#155](https://github.com/100rd/Omniscience/issues/155)):

| Score | Condition |
|---|---|
| 0.9 | Responsible PR found AND merged within 24h before the alert fired |
| 0.6 | Responsible PR found but no temporal correlation |
| 0.4 | Target resource resolved but no PR |
| 0.1 | Only the alert entity itself was resolvable |
| 0.0 | Documented for completeness; alert-resolution failure 404s upstream |

**Errors**:

| Code | Meaning |
|---|---|
| `invalid_alert_id` | `alert_id` is not of the form `alert://{provider}/{id}` |
| `alert_not_found` | The alert is unknown in the caller's workspace (also returned for cross-workspace lookups — see ACL invariant) |
| `forbidden` | Token is not workspace-scoped |
| `invalid_timezone` | `as_of` is naive or non-UTC |

**ACL invariant**: `workspace_id` is taken from the caller's bearer token, never from input. A foreign workspace's `alert_id` returns `alert_not_found` — the same response a non-existent alert would produce. Existence is never leaked.

## Bitemporal `as_of` parameter

The `as_of` parameter (issue [#133](https://github.com/100rd/Omniscience/issues/133)) anchors a read to the graph state that was valid at a point in time per [ADR-0008](../decisions/0008-bitemporal-schema-for-neo4j.md) §5.

**Format**: ISO-8601 timezone-aware UTC datetime.
- Accepted: `2026-04-12T19:25:00Z`, `2026-04-12T19:25:00+00:00`.
- Rejected: naive datetimes (no timezone) and non-UTC offsets — error code `invalid_timezone`.

**Boundary semantics** (ADR-0008 §5):
- Open-closed interval: `valid_from <= as_of < valid_to`.
- Still-current rows have `valid_to = null`; they are returned for any `as_of >= valid_from`.
- A future `as_of` is permitted and resolves to the still-current row (equivalent to `as_of=null`).
- A pre-history `as_of` (before any record exists for the entity in the workspace) returns an empty result with `meta.degraded_response = "as_of_before_recorded_history"`.

**Response envelope**: every response carries `effective_as_of` — the timestamp the response reflects. For `as_of=null` requests this is the response generation time. For an explicit `as_of` it is echoed back so callers can pin retries.

**ACL invariant**: `as_of` is a query-time predicate, NOT an authorisation surface. The token-derived `workspace_id` continues to gate which `(workspace_id, as_of)` slice is queryable. A token for workspace A can never read workspace B's data at any `as_of`.

**Out of scope (separate sub-issues)**:
- Qdrant `as_of` payload filter — issue [#134](https://github.com/100rd/Omniscience/issues/134). The vector step in `search` is unfiltered on time and scoped only by chunk ids the graph traversal returned.

## Errors

All tools return standard MCP error objects. Notable codes:

| Code | Meaning |
|---|---|
| `unauthorized` | Token missing / invalid / expired |
| `forbidden` | Token lacks required scope |
| `rate_limited` | Too many requests (429-equivalent) |
| `source_not_found` | Requested source id doesn't exist |
| `embedding_provider_unavailable` | Can't embed query — retry later |
| `invalid_timezone` | `as_of` is naive or non-UTC (issue #133) |
| `invalid_alert_id` | `resolve_incident.alert_id` is not `alert://{provider}/{id}` (issue #153) |
| `alert_not_found` | `resolve_incident` cannot resolve the alert in the caller's workspace (issue #153) |
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
