# Runbook: pgvector → Neo4j + Qdrant migration (issue #108)

> Phase 4 of Epic #96. This is the last technical phase before the
> Phase 5 cutover (#105). The tool is one-shot, idempotent, and safe
> to re-run.

## Overview

| Item | Value |
|------|-------|
| CLI | `scripts/migrate_to_hybrid.py` |
| Module | `omniscience_index.migration` |
| Graph target | Neo4j 5.x via `Neo4jGraphStore` (ADR-0005) |
| Vector target | Qdrant via `QdrantVectorStore` (ADR-0006) |
| Read source | pgvector (`entities`, `edges`, `documents`, `chunks`) |
| Write semantics | Idempotent (`MERGE` on Neo4j; UUID point IDs + content-hash decision on Qdrant) |
| Failure mode | Per-source granularity; `--resume` picks up where it left off |

Every write carries a non-null `workspace_id`. The migration-layer
ACL gate refuses to run when a pgvector `Source` has no
`tenant_id` and no `--default-workspace-id` is supplied.

## Pre-flight checklist

Run in order; each item is blocking.

1. **Baseline tests green on main.** `uv run pytest -q` ≥ 1662 passed.
2. **Neo4j reachable.** The configured `neo4j_uri` responds to
   `verify_connectivity`. Credentials are provided via environment
   (never committed).
3. **Qdrant reachable.** Port 6333 (HTTP) or 6334 (gRPC) answers a
   `GET /` on the host from `qdrant_host`.
4. **pgvector backup taken.** `pg_dump` with `--schema=public --data-only`
   of at least the `entities`, `edges`, `documents`, `chunks`,
   `sources` tables. The migration does not modify pgvector, but a
   safety net is mandatory for production runs.
5. **Storage headroom.** Qdrant disk ≥ 3× pgvector vector-index size
   (HNSW + payload indexes); Neo4j heap ≥ 4 GiB for a realistic
   symbol graph.
6. **Dry-run completed.** `python scripts/migrate_to_hybrid.py --dry-run`
   prints the per-source counts matching what you expect. No writes
   occur in this mode.
7. **Legacy sources mapped.** If any pgvector `Source` has
   `tenant_id IS NULL` you must decide whether to backfill
   `Source.tenant_id` first OR pass `--default-workspace-id`. Without
   one of these the migration aborts.

## Commands

### Dry-run (read-only inventory)

```bash
python scripts/migrate_to_hybrid.py --dry-run
```

Prints per-source `(entities, edges, chunks, documents)` counts. Zero
writes hit Neo4j or Qdrant.

### Full migration, all sources, all workspaces

```bash
python scripts/migrate_to_hybrid.py
```

Streams every source in the pgvector `sources` table into Neo4j +
Qdrant, batching at `--batch-size` (default 500). Progress is
checkpointed to `.migration_progress.json`.

### Single workspace / single source

```bash
# Migrate only one workspace
python scripts/migrate_to_hybrid.py \
  --workspace-id 00000000-0000-0000-0000-000000000000

# Migrate one source, larger batch
python scripts/migrate_to_hybrid.py \
  --source-id 11111111-1111-1111-1111-111111111111 \
  --batch-size 1000
```

### Legacy-source fallback

```bash
python scripts/migrate_to_hybrid.py \
  --default-workspace-id 22222222-2222-2222-2222-222222222222
```

Binds all sources whose `Source.tenant_id` is `NULL` to the
supplied workspace. Without this flag, such sources abort the run.

### Resume a crashed / interrupted run

```bash
python scripts/migrate_to_hybrid.py --resume
```

Reads `.migration_progress.json` and skips every source in
`completed_source_ids`. Safe to re-run — the adapters are idempotent,
so a not-yet-checkpointed source that was partially migrated will be
re-migrated from the start without creating duplicates.

### Verification pass

Run after the write phase, or standalone on an already-migrated set:

```bash
# After migration
python scripts/migrate_to_hybrid.py --verify

# Standalone — no writes, only checks
python scripts/migrate_to_hybrid.py --verify-only
```

Verification gates:

1. **Counts.** Per source: pgvector `entities` = Neo4j entity nodes,
   pgvector `edges` = Neo4j relationships, pgvector `chunks` = Qdrant
   points. Any mismatch is reported.
2. **Entity round-trip.** `ROUND_TRIP_SAMPLE_SIZE = 20` random entities
   per source. `PgVectorGraphStore.get_entity` vs
   `Neo4jGraphStore.get_entity` must return equivalent `(name, kind,
   source)` fields.
3. **Cross-workspace isolation.** When ≥ 2 workspaces are present, every
   Qdrant payload must carry only its own `workspace_id`. Zero breaches
   is the only acceptable result.
4. **Retrieval overlap.** The top-k overlap helper
   (`compute_top_k_overlap`) is shared with
   `tests/test_graphrag_regression.py` so the migration gate and the
   GraphRAG regression suite use one threshold
   (`TOP_K_OVERLAP_THRESHOLD = 0.8`).

Exit codes:
* `0` — run (or verify) succeeded.
* `2` — verification failed (details printed). Non-zero so CI halts.

## Expected wall-clock

| Dataset size | Machine | Approx. wall-clock |
|--------------|---------|---------------------|
| 10 k entities / 50 k edges / 100 k chunks (768-dim) | 8-core, NVMe, loopback Neo4j + Qdrant | 4–6 minutes |
| 100 k entities / 500 k edges / 1 M chunks | 16-core, NVMe, dedicated Neo4j + Qdrant | 45–70 minutes |
| 1 M entities / 5 M edges / 10 M chunks | 32-core cluster | 8–12 hours |

Numbers are indicative. The dominant cost is per-chunk gRPC upsert
into Qdrant plus per-entity MERGE in Neo4j. Increase `--batch-size`
(default 500) if network RTT dominates; decrease it if transactions
exceed Neo4j's `max_transaction_retry_time`.

## Rollback strategy

The migration is strictly **additive**:

* pgvector rows are never deleted or mutated.
* Neo4j + Qdrant start empty (or contain prior migration output that
  is re-idempotently replaced per-source).

To roll back:

1. **Switch reads back to pgvector.** In `apps/server/.../app.py`,
   set `STORAGE_GRAPH_BACKEND=pgvector` and
   `STORAGE_VECTOR_BACKEND=pgvector` (or leave them at their current
   default before #105 flips them to neo4j/qdrant). Restart the
   server. Immediate; no data work required.
2. **Drain the new backends (optional).** If you want a clean slate
   before a retry:
   * Neo4j: `MATCH (n:Entity) DETACH DELETE n` scoped to the workspace
     (`MATCH (n:Entity {workspace_id: $ws}) DETACH DELETE n`).
   * Qdrant: `DELETE /collections/{collection_name}` or
     `client.delete_collection(collection_name)`.
3. **Delete the progress file.** `rm .migration_progress.json` so the
   next run starts from zero.

Because pgvector remains the source of truth up through the #105
cutover, no rollback touches production data.

## Troubleshooting

### `MissingWorkspaceError: Source … has no tenant_id`

A pgvector source row has `tenant_id = NULL`. Two options:

* Backfill `Source.tenant_id` for that row (preferred for any source
  that logically belongs to a specific tenant).
* Pass `--default-workspace-id UUID` to bind all such sources to a
  single workspace.

### Neo4j `Transaction.run timeout`

The per-source batch exceeded Neo4j's `max_transaction_retry_time`.
Drop `--batch-size` (try 100 or 250). The adapter retries internally
but cannot extend the transaction budget.

### Qdrant `UnexpectedResponse: 4xx on create_payload_index`

Collection already has the indexes — the adapter tolerates this on
idempotent re-runs. If the error persists, delete the collection and
re-run. Never seen on a first migration.

### Verification: count mismatch on `chunks`

Most common cause: a tombstoned document in pgvector. The migration
intentionally skips `documents.tombstoned_at IS NOT NULL` — the
hybrid stack does not need them.  Re-run verification with
`--include-tombstoned` (TODO: not yet surfaced — track in the issue
below) or accept the skew for tombstoned rows.

### Verification: isolation breach detected

This is a P0. Stop. The migrated data has cross-workspace
contamination. Do NOT flip any read traffic. File a ticket tagged
`acl` + `p0` and post the verification output.

## Post-migration

After a clean verification pass:

1. Retain `.migration_progress.json` as a run receipt.
2. Coordinate with the #105 owner to flip `STORAGE_GRAPH_BACKEND` /
   `STORAGE_VECTOR_BACKEND` from `pgvector` to `neo4j` / `qdrant`.
3. Run the full test suite (`make test`) against the new stack once
   more before declaring cutover done.

## References

* ADR-0005 — Neo4j as graph store.
* ADR-0006 — Qdrant as vector store.
* Issue #108 — this runbook's scope.
* `packages/index/src/omniscience_index/migration/` — the tool's source.
* `tests/test_migration_to_hybrid.py` — the test battery; run this
  before every release of the migration tool itself.
