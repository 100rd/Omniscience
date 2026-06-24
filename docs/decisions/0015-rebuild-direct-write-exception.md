# ADR-0015: Rebuild script as sanctioned DR exception to the single-writer invariant

**Status**: Accepted  
**Date**: 2026-06-24  
**Deciders**: Platform Engineering  
**Supersedes**: —  
**Related**: ADR-0012 (outbox pattern), consilium-v8 AP1

---

## Context

AP1 (consilium-v8) establishes the **single-writer invariant**: all Neo4j and
Qdrant writes must flow through the Postgres outbox table and be consumed by
`OutboxConsumerWorker` via NATS JetStream.  This invariant exists to provide
per-entity ordering, idempotency via version guards, and a replay point for
disaster recovery.

`scripts/rebuild_all_projections.py` was the fourth bypass identified in the
AP1 audit.  Unlike the three application bypasses (REST merge/unmerge, linker,
operator-graph bridge) — which are now routed through the outbox — the rebuild
script writes directly to Neo4j and Qdrant.

## Decision

The rebuild script is a **sanctioned exception** to the single-writer
invariant.  It is NOT routed through the outbox for the following reasons:

1. **Precondition: empty stores**.  The script wipes Neo4j and Qdrant before
   writing.  The outbox requires both a live NATS JetStream connection and an
   empty `outbox_events` table with sequential ordering.  During a DR scenario
   NATS may be unavailable or its stream state may be inconsistent.

2. **Ordering is irrelevant at wipe-then-rebuild**.  The version guard
   (`existing_version >= version → skip`) that enforces ordering invariants
   becomes a no-op when the target stores are freshly wiped.

3. **NATS availability during DR**.  The most common scenario requiring
   `rebuild_all_projections.py` is a catastrophic Neo4j or Qdrant failure
   where NATS may also be recovering.  Depending on NATS to write Neo4j
   would create a circular dependency.

## Consequences

### Preconditions (MUST be satisfied before running the script)

1. Neo4j and Qdrant have been **completely wiped** (the script does this
   automatically with `--yes`).
2. Postgres is the source of truth and is known-good.
3. No other writer process is running against Neo4j or Qdrant during the
   rebuild.

### Postconditions (MUST be verified after the script completes)

1. Run a full reconcile scan: `POST /admin/reconcile/trigger` or restart
   the `ReconcileWorker`.  The worker will compare Postgres entity versions
   against the freshly rebuilt Neo4j/Qdrant versions and re-emit any drifted
   events.
2. Verify checkpoint versions align: `StoreCheckpoint` nodes in Neo4j should
   match Postgres `doc_version` values for all active sources.

### Warning

Running this script on a **live, non-empty** Neo4j or Qdrant instance will
**destroy all graph and vector data**.  The `--yes` flag is a last-resort
guard, not a safety mechanism.

### Compliance

This exception is reviewed at each consilium iteration.  If NATS HA is
improved to survive the DR scenarios described above, routing the rebuild
through the outbox should be reconsidered.

---

## RTO (Recovery Time Objective) — AP5, consilium-v8

**Default budget: 900 seconds (15 minutes).**

The rebuild script enforces an RTO budget so that CI/staging DR drills can
assert recovery time objectively.  If the rebuild + verification completes
within the budget the script exits 0.  If it exceeds the budget the script
exits 2 and the CI job fails.

### Rationale for the 900 s default

| Factor | Estimate |
|--------|----------|
| Neo4j wipe + Qdrant wipe | ≤ 30 s |
| Rebuild 50 k chunks (no re-embedding; stored vectors) | ≤ 600 s |
| Post-rebuild verification (count queries) | ≤ 30 s |
| Safety margin | 240 s |
| **Total** | **≤ 900 s** |

The default is intentionally conservative for the first iteration.  As
empirical numbers accumulate from CI drills the budget should be tightened
to 2× p95 observed rebuild time.

### Override at runtime

```
python scripts/rebuild_all_projections.py --yes --rto-seconds 300
```

### CI/staging drill

`.github/workflows/dr-drill.yml` seeds a small fixture dataset, runs the
rebuild with `--rto-seconds 120` (small fixture budget), and fails the job
if the RTO is exceeded or verification reports any count mismatch.

### Deterministic rebuild order

The script selects Documents with `ORDER BY source_id, id` to guarantee
the same Postgres SoT produces byte-identical checkpoint epoch sequences
across two independent runs.  Chunks within each document are ordered by
`Chunk.ord` (pre-existing stable column).  This property is required for
the CI determinism assertion in `tests/test_dr_rebuild.py`.
