# Changelog

All notable changes to Omniscience are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — v0.3 line

### Added

- **Operator: `LimitRange` watcher (issue #202).** The operator now watches
  `core/v1 LimitRange` cluster-wide and emits a deterministic-JSON
  representation of `spec.limits[]` (defaults / min / max /
  maxLimitRequestRatio for Container, Pod, PersistentVolumeClaim, and
  similar object types). Closes one of the v0.4 default-flip parity gaps
  tracked under epic #199. RBAC adds `get`/`list`/`watch` on
  `limitranges`; no other verbs.

- **Operator: `ResourceQuota` watcher (issue #204).** The operator now
  watches `core/v1 ResourceQuota` cluster-wide and emits a deterministic-
  JSON representation of `spec.hard`, `spec.scopes`, and
  `spec.scopeSelector`. Closes one of the v0.4 default-flip parity gaps
  tracked under epic #199. RBAC adds `get`/`list`/`watch` on
  `resourcequotas`; no other verbs.

- **Operator: `ServiceAccount` watcher (issue #206).** The operator now
  watches `core/v1 ServiceAccount` cluster-wide and emits Secret
  reference NAMES (no values), image pull Secret names, and the
  automount setting. Closes one of the v0.4 default-flip parity gaps
  tracked under epic #199. RBAC adds `get`/`list`/`watch` on
  `serviceaccounts`; no other verbs.

### Deprecated

- **`k8s-agentic` connector deprecation announced (issue #168, ADR-0011).**
  The legacy LLM-driven Kubernetes connector at
  `packages/connectors/src/omniscience_connectors/agentic/k8s.py` is
  superseded by the in-cluster `omniscience-operator` Go controller at
  `operator/`.  Importing
  `omniscience_connectors.agentic.k8s` now emits a `DeprecationWarning`
  carrying the removal version (`v0.5.0`), the migration target name
  (`omniscience-operator`), and a URL to the migration guide.

  - Migration guide: [`docs/connectors/k8s-agentic-deprecation.md`](docs/connectors/k8s-agentic-deprecation.md)
  - Parity matrix: [`docs/connectors/k8s-agentic-vs-operator-parity.md`](docs/connectors/k8s-agentic-vs-operator-parity.md)
  - Schedule (ADR-0011): v0.3 announce → v0.4 default-disabled →
    v0.5 remove.
  - During the cutover window, server-side dedup
    ([ADR-0010](docs/decisions/0010-server-side-emitter-dedup.md))
    silently drops agentic events for any
    `(workspace_id, external_id)` the operator is authoritative on.
    Watch
    `omniscience_ingestion_dedup_drop_total{authority_emitter="k8s-operator"}`
    rise as the operator takes over.

  The connector remains **fully functional** in v0.3.  No customer
  code change is required to upgrade to v0.3; the deprecation warning
  is informational.

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
