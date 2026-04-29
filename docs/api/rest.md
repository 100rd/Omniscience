# REST API

Secondary interface. Exists for:

- Admin UI
- Webhook ingestion (git push events etc.)
- Non-MCP integrations
- Debugging from `curl`

MCP-first clients should prefer [MCP API](mcp.md).

## Base

```
https://<host>/api/v1
```

All endpoints require `Authorization: Bearer <token>` except `/health`.

## Endpoints

### `GET /health`

Unauthenticated. Returns `{ "status": "ok", "version": "..." }` when the service can serve traffic.

### `POST /search`

Body: same as MCP `search` input.
Response: same as MCP `search` output.

Optional query parameter `as_of` (ISO-8601 UTC datetime) anchors the result to graph state at that time per [ADR-0008](../decisions/0008-bitemporal-schema-for-neo4j.md) §5. May also be supplied inside the request body — the query-string value wins when both are present. See **Bitemporal `as_of` parameter** below. Naive or non-UTC values return 400 `INVALID_TIMEZONE`.

### `GET /entities/{name}`

Resolve a single entity by name within the caller's workspace. Workspace-scoped token required.

Optional query parameter `as_of` returns the entity as it was at that point in time (issue [#133](https://github.com/100rd/Omniscience/issues/133), ADR-0008 §5).

Response:

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

When the entity is unknown at the supplied `as_of`, the response is 200 with `entity: null` and `meta.degraded_response = "as_of_before_recorded_history"`. When the entity is unknown without `as_of`, the response is 404.

### `GET /entities/{name}/related`

Traverse the entity graph from a named entity. Workspace-scoped token required.

Query parameters: `max_depth` (int, default 1), `edge_types` (repeat for multi-value), `as_of` (ISO-8601 UTC datetime, optional).

Response carries the seed, related entities, edges, plus `effective_as_of` and an optional `meta` block. A pre-history `as_of` returns 200 with an empty payload + `meta.degraded_response = "as_of_before_recorded_history"`.

### `POST /incidents/{alert_id}/resolve`

Mirror of the MCP `resolve_incident` tool (issue [#153](https://github.com/100rd/Omniscience/issues/153)). Workspace-scoped token required.

`alert_id` is the path parameter (URL-encoded) and MUST be of the form `alert://{provider}/{provider_alert_id}`. Other forms return 400 `invalid_alert_id`.

Query parameters: `max_depth` (int, default 2; clamped to `[1, 5]`), `as_of` (ISO-8601 UTC datetime, optional).

Returns the same JSON shape as the MCP tool — see [MCP API → `resolve_incident`](mcp.md#resolve_incident) for the schema, the v0.1 confidence-score heuristic, and the error matrix.

**Workspace-mismatch behaviour**: a foreign workspace's `alert_id` returns 404 `alert_not_found` (not 403) to avoid leaking alert existence — same response a non-existent alert would produce.

### `GET /sources`

List sources. Query params: `type`, `status`.

### `POST /sources`

Create source. Body validated per-type (Pydantic discriminated union).

### `GET /sources/:id`

Read one source.

### `PATCH /sources/:id`

Update source (config, secrets_ref, status, freshness_sla_seconds).

### `DELETE /sources/:id`

Remove source. Associated documents and chunks are tombstoned then purged by janitor.

### `POST /sources/:id/sync`

Trigger a manual sync now. Returns `{ "run_id": "..." }`. Progress via `GET /ingestion-runs/:run_id`.

### `GET /sources/:id/stats`

Same as MCP `source_stats`.

### `GET /documents/:id`

Retrieve document with all chunks.

### `POST /ingest/webhook/:source_name`

Webhook receiver for sources that push events (GitHub, GitLab, Confluence). Payload validated + signature-checked per source type. Enqueues a sync task.

### `GET /ingestion-runs`

Recent ingestion runs. Query params: `source_id`, `status`, `limit`.

### `GET /ingestion-runs/:id`

Single run detail.

### `GET /tokens` / `POST /tokens` / `DELETE /tokens/:id`

Token management (admin scope).

## Bitemporal `as_of` parameter

The `as_of` parameter (issue [#133](https://github.com/100rd/Omniscience/issues/133)) anchors a read to the graph state that was valid at a point in time per [ADR-0008](../decisions/0008-bitemporal-schema-for-neo4j.md) §5.

**Format**: ISO-8601 timezone-aware UTC datetime (e.g. `2026-04-12T19:25:00Z` or `2026-04-12T19:25:00+00:00`). Naive datetimes and non-UTC offsets return 400 `INVALID_TIMEZONE`.

**Boundary semantics** (ADR-0008 §5):
- Open-closed interval: `valid_from <= as_of < valid_to`.
- Still-current rows have `valid_to = null`; they are returned for any `as_of >= valid_from`.
- A future `as_of` resolves to the still-current row (equivalent to omitting the parameter).
- A pre-history `as_of` returns 200 with an empty payload and `meta.degraded_response = "as_of_before_recorded_history"`.

**Response envelope**: every response carries `effective_as_of` — for `as_of=null` requests this is the response generation time; for an explicit `as_of` it is echoed back so callers can pin retries.

**ACL invariant**: `as_of` is a query-time predicate, NOT an authorisation surface. The token-derived `workspace_id` continues to gate every read. A token for workspace A cannot read workspace B's data at any `as_of`.

**Out of scope**:
- Qdrant `as_of` payload filter — issue [#134](https://github.com/100rd/Omniscience/issues/134).

## Error format

```json
{
  "error": {
    "code": "unauthorized",
    "message": "Token missing or invalid",
    "details": {}
  }
}
```

HTTP status codes map to error codes (401 → `unauthorized`, 403 → `forbidden`, 404 → `*_not_found`, 429 → `rate_limited`, 500 → `internal`, 400 → `invalid_timezone` for malformed `as_of`).

## Rate limiting

Per-token, token-bucket: 60 rpm default, configurable. Exceeded → 429 with `Retry-After` header.

## OpenAPI spec

`GET /api/v1/openapi.json` — machine-readable spec. Served from FastAPI automatic docs. UI at `/docs` (dev only; disabled in production).

## Versioning

`/api/v1` until v0.2 graduates to semver.
