# Freshness and lineage

Two cross-cutting concerns that determine how much **trust** a caller can place in Omniscience's responses:

- **Freshness** — how recently the source was synced, and how recently each chunk was re-indexed
- **Lineage** — where a chunk came from, by what process, with what model/parser, in which ingestion run

Together these let an AI client decide: *"is this chunk trustworthy enough for my task, and can I explain the answer to my user?"*

## MCP v1 evidence fitness

The stable MCP `1.0.0` envelope evaluates freshness over **only sources actually used** to form the
answer. An unrelated stale source in the same workspace does not poison the response. The four states
are:

```text
fresh | stale | unknown | degraded
```

- `fresh`: complete used-source lineage exists and every used source is within its freshness bound.
- `stale`: at least one used source exceeds its bound; only those ids appear in `stale_source_ids`.
- `unknown`: freshness cannot be proved, including never-synced evidence or missing lineage.
- `degraded`: a relevant source/store failure makes the freshness result partial or unreliable.

`oldest_source_age_seconds` is the greatest evaluated age among used sources, conservatively rounded
up to an integer. It is `null` when no source age can be proved. An explicit historical `as_of` remains
historical evidence: age is measured at that boundary, and a current source snapshot recorded after it
cannot prove historical lineage, so the result is `unknown` rather than fresh.

Consistency is separate from age. Postgres is authoritative; Neo4j and Qdrant are projections. Their
distance is a version delta named `projection_lag_versions`, never seconds. The legacy
`staleness_seconds` field may remain during compatibility migration but does not define v1 projection
semantics and is never copied into the v1 field. Without explicit integer version evidence, consistency
remains honestly unknown or degraded and selects fallback.

Stale used evidence, missing lineage, never-synced required evidence, relevant store divergence, and
unusable unknown/degraded states set `fallback.required=true` with a stable reason. Consumers then use
direct authoritative sources. Omniscience output remains evidence rather than the sole oracle for merge,
apply, incident action, or terminal workflow decisions.

## Freshness

### Per-source SLO

Each `source` carries `freshness_sla_seconds` (see [schema.md](schema.md)). When `now() - last_sync_at > sla`, the source is flagged stale:

- `GET /sources` surfaces it in status
- MCP `list_sources` returns `is_stale: true`
- Prometheus alert fires
- UI badges it

### Per-query filter

The MCP `search` tool accepts `max_age_seconds`. Chunks whose `indexed_at` is older than this are filtered out before ranking. Caller controls the trade-off between recall and freshness.

### Source update mechanisms

| Source type | Mechanism | Target freshness |
|---|---|---|
| `git` (GitHub/GitLab) | Push webhook → incremental fetch | < 5 min after push |
| `fs` (local filesystem) | `watchfiles`/`fsnotify` → immediate reparse | < 30 sec after save |
| `confluence` | Webhook (Cloud) / polling (Server); daily full sync as safety net | < 5 min (Cloud), 15 min (Server) |
| `notion` | Webhook via Notion integration | < 5 min |
| `slack` | Events API subscription | Seconds |
| `jira` / `linear` | Webhook | < 2 min |
| `grafana` | Polling (15 min default) | < 15 min |
| `argocd` | Webhook on sync events | < 1 min |
| `k8s` (state) | Watch API | Real-time |
| `terraform` | On-apply hook + periodic state-file pull | < 5 min after apply |
| `databases` (schemas) | Polling with change detection | < 1 hour |

### Safety net: periodic full sync

Every source runs a scheduled full rediscover regardless of push/pull mode — default daily at low-traffic hour. Catches:

- Missed webhooks (networking, provider downtime)
- Source-side edits that didn't emit events (manual DB writes)
- Permission changes affecting which documents are visible

### Retention of stale data

A document whose source goes silent for longer than `freshness_sla_seconds * N` (configurable, default 7× SLA) is tombstoned. Retention window keeps it queryable-with-warning for 30 days before hard delete.

## Lineage

### Data model

Lineage fields on every chunk (see [schema.md](schema.md) — updated in the same PR as this doc):

| Field | Meaning |
|---|---|
| `document_id` | Parent doc |
| `source_id` | Source that produced the doc |
| `ingestion_run_id` | The specific ingestion run that produced this chunk |
| `embedding_model` | e.g., `bge-large-en-v1.5` |
| `embedding_provider` | e.g., `ollama`, `google-ai`, `openai` |
| `parser_version` | Version of the parser used (tree-sitter grammar hash + parser code hash) |
| `chunker_strategy` | e.g., `code_symbol`, `markdown_section` |
| `indexed_at` | Timestamp of index write |
| `doc_version` | Monotonic; changes when source content changes |

### Response format

MCP `search` response includes a `lineage` sub-object on every hit (see [api/mcp.md](api/mcp.md) — same PR):

```json
{
  "hits": [{
    "chunk_id": "...",
    "text": "...",
    "score": 0.87,
    "source": {"id": "...", "name": "main-gitlab", "type": "git"},
    "citation": {
      "uri": "https://github.com/org/repo/blob/abc123/auth.py#L42-L60",
      "title": "auth.py",
      "indexed_at": "2026-04-17T10:32:15Z",
      "doc_version": 7
    },
    "lineage": {
      "ingestion_run_id": "ir_01HXYZ...",
      "embedding_model": "text-embedding-004",
      "embedding_provider": "google-ai",
      "parser_version": "treesitter-python-0.21+oms-0.4.2",
      "chunker_strategy": "code_symbol"
    },
    "metadata": {...}
  }]
}
```

### Why these fields specifically

Every field answers a real operational question:

- **`ingestion_run_id`** → *"which run produced this? Did that run complete cleanly?"*
- **`embedding_model` + `embedding_provider`** → *"is this chunk embedded with the current model, or does it need re-embed?"* Critical when model changes or a workspace switches providers
- **`parser_version`** → *"a parser bug was fixed on 2026-04-01. Which chunks predate the fix?"* Enables targeted reindex instead of blast-radius full reindex
- **`chunker_strategy`** → *"this answer is oddly specific — is it a whole-function chunk or a sliding window?"* Affects how the caller interprets context

### Lineage as a first-class query

For operators, not AI clients:

- `GET /documents/:id` returns the full lineage trail
- `GET /chunks/:id/lineage` returns ingestion history for a single chunk
- CLI: `omniscience chunks inspect <chunk_id>` prints the trail

### Privacy note

Lineage contains no source content itself (no prompts, no secrets). It is safe to log and export. Source content (text of chunks, document bodies) is governed by regular ACL — see query-time filters.

## How this enables downstream use

- **AI clients** can decide trust per-chunk ("only accept chunks under 10 min old from sources tagged `prod-docs`")
- **Operators** can diagnose "why is this answer wrong?" by tracing back to ingestion run + parser version
- **Re-embed / reindex jobs** can target only affected chunks (same model? same parser? same ingestion run?) rather than nuking everything

## See also

- [schema.md](schema.md) — database fields
- [api/mcp.md](api/mcp.md) — response format
- [architecture.md](architecture.md) — ingestion pipeline stages where these fields are populated
