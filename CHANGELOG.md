# Changelog

All notable changes to Omniscience are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-04-25

Cutover release for Epic #96. Vector and graph storage move out of Postgres
to Qdrant and Neo4j respectively. Postgres retains operational metadata only.

### Breaking changes

- **`PgVectorGraphStore` and `PgVectorVectorStore` are removed.** All graph
  reads/writes go through the Neo4j adapter; all vector reads/writes go
  through the Qdrant adapter.
- **Default backends flipped.** `STORAGE_GRAPH_BACKEND` defaults to `neo4j`,
  `STORAGE_VECTOR_BACKEND` defaults to `qdrant`. The string value `pgvector`
  is no longer accepted on either flag.
- **`docker-compose.yml`**: Neo4j and Qdrant services no longer require the
  `--profile graph` flag; they start as part of the default stack. The
  `ankane/pgvector` Postgres image is gone — Postgres now runs the standard
  upstream image with operational tables only.
- **Helm chart**: Neo4j and Qdrant sub-charts are required dependencies;
  pgvector-specific bootstrap removed.
- **Database schema**: the `vector` column on `chunks` and the `pgvector`
  extension are dropped in alembic migration `0004_drop_pgvector`.
  `ChunkData` no longer carries an `embedding` field — chunk embeddings live
  in Qdrant.
- **Retrieval**: the legacy hybrid retrieval pipeline (`search.py`,
  `strategies/`, `reranker.py`, `query_rewriter.py`, `ranking.py`,
  `filters.py`) is removed. All retrieval flows through `GraphRAGComposer`.
  `app.state.retrieval_service` is preserved for federation-peer
  compatibility but is unwired by default.

### Removed

- `PgVectorGraphStore`, `PgVectorVectorStore`
- `omniscience_retrieval.adapters.*` (entire sub-package)
- `omniscience_retrieval.search`, `.strategies.*`, `.reranker`,
  `.query_rewriter`, `.ranking`, `.filters`
- `tests/test_adaptive_retrieval.py`, `tests/test_reranker.py`,
  `tests/test_retrieval.py`, `tests/test_storage_protocols.py`
- pgvector configuration knobs from `Settings`

### Changed

- `Settings.app_version`: `0.1.0` → `0.2.0`
- ADR-0005 (Neo4j as graph store) status: `Proposed` → `Implemented`
- ADR-0006 (Qdrant as vector store) status: `Proposed` → `Implemented`
- `docs/architecture.md`, `docs/schema.md`, `docs/deploy.md` updated to
  reflect Neo4j + Qdrant as the only stores.

### Added

- alembic `0004_drop_pgvector` — drops the `vector` column, HNSW index,
  and `pgvector` extension.
- `tests/support/graph_store_proxy.py` — test-only `GraphStore`
  implementation backed by the existing `GraphQueryService` so MCP/REST
  wiring tests do not need a Neo4j testcontainer.

### Upgrade notes

This is a major version bump. There is no in-place upgrade path from a
pre-v0.2 install that has data — see
`docs/migrations/pgvector-to-hybrid.md` for the manual procedure using the
`scripts/migrate_to_hybrid.py` script (kept in-tree for that purpose).

For installs with no production data: drop the database, run
`alembic upgrade head`, redeploy. New writes land directly in Neo4j +
Qdrant.

### Known gaps

- The ingestion worker continues to drive `IndexWriter` for Postgres-side
  chunk metadata. Wiring ingestion to write entities into Neo4j and
  embeddings into Qdrant via the same path the migration script uses is a
  follow-up. Until then, fresh installs need the migration script even on
  empty pgvector-less data.

## [0.1.0] — 2026-03-xx

Initial pre-release. Single-store Postgres + pgvector retrieval service
with FastMCP exposure. See `docs/vision.md` for the original product
posture before the v0.2 pivot.
