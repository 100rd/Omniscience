# Database schema

Single Postgres database, single schema (`public` in MVP; namespaced in v0.2). All tables SQLAlchemy 2 declarative, migrations via Alembic.

## Tables

### `sources`

Configured ingestion sources.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid pk | |
| `type` | enum | `git`, `fs`, `confluence`, `notion`, ... |
| `name` | text | Unique per tenant |
| `config` | jsonb | Type-specific config; validated by Pydantic discriminated union at write |
| `secrets_ref` | text | Pointer to env var / vault; never the secret itself |
| `status` | enum | `active`, `paused`, `error` |
| `last_sync_at` | timestamptz | |
| `last_error` | text | Nullable |
| `last_error_at` | timestamptz | Nullable |
| `freshness_sla_seconds` | int | Alert if `now - last_sync_at > sla` |
| `tenant_id` | uuid | Nullable in single-tenant MVP |
| `created_at`, `updated_at` | timestamptz | |

Indexes:
- UNIQUE(`tenant_id`, `name`)
- `status`

### `documents`

One row per source-native document (file, wiki page, issue, ...).

| Column | Type | Notes |
|---|---|---|
| `id` | uuid pk | |
| `source_id` | uuid fk → sources | On delete cascade |
| `external_id` | text | Source-native id (git path@rev; wiki page id; ...) |
| `uri` | text | Origin URL for citation |
| `title` | text | Nullable |
| `content_hash` | text | sha256 of normalized content; change detection + dedup |
| `doc_version` | bigint | Monotonic per document |
| `metadata` | jsonb | mime, language, author, source-side updated_at, tags |
| `indexed_at` | timestamptz | |
| `tombstoned_at` | timestamptz | Nullable; excludes from default retrieval |

Indexes:
- UNIQUE(`source_id`, `external_id`)
- Partial index on `tombstoned_at IS NULL`
- `indexed_at` for freshness queries

### `chunks`

Chunked, embedded content used at retrieval.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid pk | |
| `document_id` | uuid fk → documents | On delete cascade |
| `ord` | int | Order within document |
| `text` | text | Raw chunk text |
| `text_tsv` | tsvector | Generated column, language-aware |
| `embedding` | vector(768) | Dim parameterized via migration |
| `symbol` | text | For code: function/class FQN; nullable |
| `metadata` | jsonb | Anything useful for filters (line_range, section_path, ...) |

Indexes:
- `(document_id, ord)`
- GIN on `text_tsv`
- HNSW on `embedding` (`vector_cosine_ops`, `m=16, ef_construction=64`)

### `ingestion_runs`

Audit of ingestion attempts.

| Column | Type |
|---|---|
| `id` | uuid pk |
| `source_id` | uuid fk |
| `started_at` | timestamptz |
| `finished_at` | timestamptz nullable |
| `status` | enum: `running`, `ok`, `partial`, `error` |
| `docs_new` | int |
| `docs_updated` | int |
| `docs_removed` | int |
| `errors` | jsonb |

### `api_tokens`

Minimal API token model (MVP single-user, OIDC added in v0.2).

| Column | Type | Notes |
|---|---|---|
| `id` | uuid pk | |
| `name` | text | User-readable label |
| `hashed_token` | text | argon2 hash of the secret part |
| `token_prefix` | text | First 8 chars for identification in logs |
| `scopes` | jsonb | `["search", "sources:read", "sources:write", "admin"]` |
| `created_at` | timestamptz | |
| `expires_at` | timestamptz | Nullable |
| `last_used_at` | timestamptz | |

## Key design decisions

### Content-hash dedup

`documents.content_hash` is the source of truth for "has anything changed". On ingestion:

1. Compute hash of normalized content
2. If exists and hash matches → skip (no-op, update `indexed_at` only if needed)
3. If exists and hash differs → update doc, delete old chunks, insert new chunks, increment `doc_version`
4. If new → insert everything

Normalization: trim trailing whitespace per line, collapse multiple blank lines to one, strip BOM. Aims to ignore cosmetic-only changes.

### Tombstones not deletes

Removed documents get `tombstoned_at` set, not DROP. Reasons:

- Queries default to `tombstoned_at IS NULL`, so they're invisible
- Enables "recently removed" queries for operators
- Survives source temporary outages (a connector going blind for an hour shouldn't obliterate the index)

A janitor job eventually hard-deletes tombstones older than the retention window.

### Single-dim embedding column

`embedding vector(768)` is fixed at table creation. Changing dim requires:

- ALTER migration (expensive on large tables)
- Re-embed everything

Parametrizing dim via additional columns is possible but adds query complexity. Preferring "pick your embedding model, commit to it" for simplicity.

### No separate keyword index

`text_tsv` is a generated column + GIN index. This gives BM25-like full-text search without a separate Elasticsearch/Meilisearch deployment. Sufficient for v0.1–v0.3; reconsider for larger scale.

### No separate graph DB

Symbol relationships (function A calls function B) can be stored as edges in a `symbol_edges` table (v0.3). For v0.1 they live in `chunks.metadata` and aren't queried structurally.

## Migrations

All managed by Alembic. Initial migration:

1. Enable `pgvector` extension (guarded — skip if already installed)
2. Create all tables above
3. Create all indexes (HNSW is expensive — ran after bulk loads ideally)
4. Seed default tenant / admin token (dev mode only)

## See also

- [Architecture](architecture.md)
- [Retrieval](api/mcp.md)
