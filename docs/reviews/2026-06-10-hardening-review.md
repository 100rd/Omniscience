# Hardening review — 2026-06-10

Multi-agent review of the retrieval / index / ingestion stack, followed by
implementation, contract verification, and a live local redeploy. This note
records what was found, what shipped, and the deploy caveats — so the numbers
and decisions are not lost.

## Scope

Read-only review across five dimensions (architecture, backend correctness,
security, QA/tests, devops), then three rounds of fixes shipped as six PRs.

## Changeset (PRs #304–#309, merged to `main`)

| Metric | Value |
|---|---|
| Files changed | 48 (`+7915 / −361`) |
| Breakdown | 21 code · 19 tests · 4 docs/ADR · 2 migrations · 2 scripts |
| New files | 14 (2 workers, 2 migrations, reindex script, Prometheus alert, 8 test files) |
| Substantive commits | 15 (+ 6 merge commits) |
| Test lines added | +4885 |
| New test functions | +134 |
| Contract tests (real containers) | 66 Qdrant + 10 bitemporal property + 5 GraphRAG-hybrid |

## Defects closed (9)

| Severity | Issue | Fix |
|---|---|---|
| VETO | Source/Document layer not workspace-scoped — cross-tenant read of sources (incl. `secrets_ref`) and documents | scope all handlers by `workspace_id`; cross-tenant → 404; legacy token → fail-closed 403 |
| CRITICAL | `purge_tombstones` left Qdrant points forever | orchestrate `delete_tombstoned` (Qdrant-first) |
| CRITICAL | k8s ingest staleness invisible (6-day launchd outage unnoticed) | k8s TTL + `freshness_sla_seconds` + Prometheus alert on `omniscience_source_stale_total` |
| CRITICAL | Qdrant path of `IndexWriter` had 0% unit coverage | `TestIndexWriterWithVectorStore` (success / Qdrant-failure rollback / purge) |
| HIGH | `as_of` gap: content updates deleted old vector points → historical search empty while graph returned correct past | end-date instead of delete; current-only filter on all read paths |
| HIGH | Triple write not atomic, no compensation | reconcile worker (PG↔Qdrant↔Neo4j drift), honest writer docstring |
| HIGH | BM25/RRF promised in ADR-0004 but absent (`text_matches` always 0) | sparse `sparse_bm25` vector + RRF (k=60) + strategy dispatch |
| HIGH | Enumeration queries ("how many X / all Y") recall < 100% | enumerate path: scroll + native exact count over payload indexes, dedup by `document_id` |
| MED | NULL-tenant rows visible to every workspace via `workspace_filter` | strict `== workspace_id`; alembic backfill to default workspace |

## Bugs that only surfaced at integration / deploy (4)

Unit tests on mocks passed; these were caught by contract tests and the live stack.

1. **Duplicate versions** — end-dating without a current-only filter leaked old
   versions as duplicates (the original problem, one level deeper). Caught by a
   Qdrant contract test.
2. **Migration 0010** — `uuid` column vs `VARCHAR` bind param failed on real
   Postgres (`DatatypeMismatch`). Tests ran on a backend that did not enforce it.
   → cast `CAST(:ws_id AS uuid)` (#306).
3. **reindex script TLS** — `api_key` forced a TLS handshake against plaintext
   gRPC. → default `https=False`, opt-in `--https` (#307).
4. **Process-salted hash (critical)** — `_tokenize_to_sparse` used builtin
   `hash()`, salted per-process (`PYTHONHASHSEED`). Tokens indexed in the seed
   process and hashed in the server process landed in different slots, so
   sparse/hybrid matched nothing in production despite valid `sparse_bm25`
   vectors. The contract test passed because indexing and search shared one
   interpreter. → `zlib.crc32` + golden-value and cross-process tests (#309).

## Retrieval quality — before → after (measured on the live stack)

| Metric | Before | After |
|---|---|---|
| keyword `text_matches` | 0 | 9 |
| hybrid `text_matches` | 0 | 18 (RRF mixes k8s + AWS) |
| enumeration recall ("all load balancers") | ~93% (43/46, top-50 semantic) | 100% (46/46, deterministic scroll) |
| keyword latency | — | ~23 ms |
| hybrid latency | — | ~65 ms |

## Deploy notes / follow-ups

- **Reindex on deploy**: collection schema changed (dense → dense+sparse). Run
  `scripts/reindex_qdrant_hybrid.py` (or wipe+reseed for fresh installs).
- **Migrations**: alembic `0010` (backfill) and `0011` (sync_mode) — additive.
- **Image rebuild**: a clean `--no-cache` rebuild re-pulls `python:3.12-slim`;
  pre-pull the base image if the registry is slow. A plain `docker compose build`
  can reuse the `COPY packages/` layer — verify the new code is actually in the
  image before recreating.
- **CI version skew**: contract tests run Qdrant `v1.12.4` vs prod `v1.17.1` —
  worth aligning.
- **Minor**: hybrid `QueryStats.vector_matches` can report a negative count
  (RRF accounting) — cosmetic, does not affect results.
- **Open (separate effort)**: transactional outbox (if exactly-once is needed
  instead of reconcile); a real-Postgres migration smoke test in CI (would have
  caught the 0010 uuid bug).
