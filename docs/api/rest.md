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

HTTP status codes map to error codes (401 → `unauthorized`, 403 → `forbidden`, 404 → `*_not_found`, 429 → `rate_limited`, 500 → `internal`).

## Rate limiting

Per-token, token-bucket: 60 rpm default, configurable. Exceeded → 429 with `Retry-After` header.

## OpenAPI spec

`GET /api/v1/openapi.json` — machine-readable spec. Served from FastAPI automatic docs. UI at `/docs` (dev only; disabled in production).

## Versioning

`/api/v1` until v0.2 graduates to semver.
