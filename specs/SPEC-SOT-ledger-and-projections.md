# SPEC-SOT: Authoritative Ledger & Query Projections
Status: ready · Depends on: none
Readiness: human-approved by @100rd on 2026-07-11 under accepted ADR-0019

## Governing ADRs

- ADR-0005/0006/0008 - graph, vector, and temporal stores
- ADR-0017 - per-source convergence semantics
- ADR-0018 - sanctioned empty-store DR exception
- ADR-0019 (accepted) - authoritative-ledger model

## Goal

Keep one recoverable authority for ingested facts while allowing Neo4j and Qdrant to be independently
rebuildable query projections with observable consistency.

## Requirements

[REQ-SOT-1] Postgres owns source/document/chunk content and lineage, versions, entity/outbox records,
workspaces, and governance metadata required to rebuild projections. **Fallback:** data absent from the
ledger cannot be certified recoverable.

[REQ-SOT-2] Application writes to Neo4j/Qdrant flow through the transactional outbox and one projection
consumer with per-entity ordering/idempotency. **Fallback:** detected direct writes fail architecture tests.

[REQ-SOT-3] Outbox insertion is atomic with the authoritative mutation. Publish/consume is at-least-once
and projection apply uses version/CAS guards. **Fallback:** transaction or claim failure leaves no partial
authority change exposed as projected.

[REQ-SOT-4] Reconciliation compares per-source/per-entity ledger and projection checkpoints, boundedly
re-emits drift, heals tombstones/edges, and parks poison entities. **Fallback:** non-convergence alerts and
suppresses affected evidence.

[REQ-SOT-5] Read composition applies the complete per-source watermark map and fails closed on unknown
versioned sources in strict mode. **Fallback:** empty/unavailable map is an observable blackout, not pass-through.

[REQ-SOT-6] ADR-0018 direct writes are permitted only after target projections are wiped, all normal
writers stopped, human DR invocation recorded, and post-rebuild verification passes. **Fallback:** unmet
precondition aborts before destructive action.

[REQ-SOT-7] DR rebuild is deterministic for the same ledger revision and checks counts, hashes,
checkpoints, idempotent verify-only rerun, and RTO. **Fallback:** mismatch or RTO breach fails the drill.

[REQ-SOT-8] Backup/restore preserves every field needed for projection rebuild and tenant/audit history.
**Fallback:** a deployment without a passing restore drill cannot claim production recoverability.

[REQ-SOT-9] Architecture/product docs name Postgres authority and Neo4j/Qdrant projections consistently.
**Fallback:** documentation drift fails contract conformance.

## Interfaces

```text
Ledger.commit(change, outbox_events) -> revision
ProjectionConsumer.apply(event) -> applied|duplicate|stale|parked
Reconciler.check(workspace, source) -> ConvergenceReport
Rebuild.from_ledger(revision, empty_targets) -> VerificationReport
```

## Verification

- P-SOT-1 rebuilds both projections using only a restored Postgres ledger.
- P-SOT-2 AST/import gate rejects application direct-store writes outside ADR-0018 allowlist.
- P-SOT-3 crashes before/after commit/publish/apply and proves atomic authority plus idempotent projection.
- P-SOT-4 injects missing/stale/tombstoned nodes and reaches bounded convergence or parked alert.
- P-SOT-5 proves cold/unmapped sources cannot leak future-epoch evidence.
- P-SOT-6 attempts live/non-empty rebuild and aborts before wipe/write.
- P-SOT-7 runs nightly rebuild, verify-only, hash/count/checkpoint and RTO assertions.
- P-SOT-8 restores a backup into an empty environment and passes P-SOT-1.
- P-SOT-9 lints README/architecture/operations language against the ledger/projection contract.
