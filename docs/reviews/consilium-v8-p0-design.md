# Consilium v8 — P0 Implementation Design (AP1 + AP2 core)

> Architect's approved design for the single-writer invariant (AP1) and closed-loop reconciliation
> + per-entity anti-entropy (AP2). Branch `feat/consilium-v8-p0`. Baseline: 45 mypy errors in 13 files.
> This document is authoritative for the AP1/AP2 implementation. AP3/AP4/AP5 are tracked separately.

## AP1 — Single-writer invariant

**Approach**: application-level partition key (`entity_id` column on `outbox_events`), NOT per-entity
NATS subjects. Keep the existing single durable consumer per event type; the idempotent version guard
(`neo4j/store.py:310`, `qdrant_store.py`) is the ordering backstop. Route 3 of 4 bypass writers
through the outbox; sanction the rebuild script as a documented DR exception (ADR-0015).

### Migration 0013 — `packages/core/alembic/versions/0013_outbox_entity_id.py` (NEW)
- Add nullable `entity_id` UUID column to `outbox_events`.
- Composite index `ix_outbox_events_entity_created` on `(entity_id, created_at)` for per-entity ordered polling.
- Edge events use `source_entity_id` as the partition key; merge/unmerge use the acted-on entity id; NULL allowed (version guard backstop applies).
- Model: add `entity_id: Mapped[uuid.UUID | None]` + index in `__table_args__`.

### Protocol corrections — `packages/core/src/omniscience_core/storage/graph.py` (fixes ~14 mypy errors)
1. Rename `GraphStore.delete_tombstoned` → `delete_tombstoned_graph() -> int` (resolves contract.py:10 base-class conflict with `VectorStore.delete_tombstoned`).
2. Add `workspace_id: uuid.UUID` (keyword-only) to `upsert_graph`.
3. `upsert_edge_by_name`: rename params to `source_name`, `properties` (match Neo4j impl).
4. `list_entities`: protocol is truth (`kind, cluster, name, as_of`) — fix `FullStore.list_entities` to match (drop `limit/offset`).
5. Add `get_entity_versions(*, workspace_id) -> dict[UUID,int]` to the protocol (impl below).

### `full_store.py` — align all delegations to corrected protocol
- `upsert_chunks`: add `epoch`, `forced_replay`.
- `delete_tombstoned` → delegate vector-only; graph method renamed to `delete_tombstoned_graph`.
- `upsert_graph`: add `workspace_id`. `upsert_edge_by_name`: `source_name`/`properties`. `list_entities`: protocol kwargs.
- Remove `merge_nodes`/`unmerge_node` from StoreContract delegation (routed via outbox now).

### New message types — `packages/core/src/omniscience_core/queue/messages.py` (or equivalent)
- `MergeNodesEvent {source_id, target_id, workspace_id}`
- `UnmergeNodeEvent {merged_node_id, workspace_id}`

### Bypass routing
- **REST** (`rest/entities.py:386,413`): replace direct `merge_nodes`/`unmerge_node` with `OutboxEvent(event_type="entity.merge"/"entity.unmerge", entity_id=..., payload=...)`. Inject `session_factory`. Response stays `200 {"success": true}` (queued semantics; up to 1 outbox tick before visible). Keep the existing AP6 admin-scope check on merge; ADD the same check to unmerge.
- **Linker** (`linker.py:70-82`): inject `session_factory`; emit `OutboxEvent(event_type="edge.upsert", entity_id=ent_a.id, payload=EdgeUpsertEvent(...))` instead of `graph_store.upsert_edge()`.
- **Operator-graph** (`ingestion/operator_graph.py:301`, wired from `worker.py:376`): add optional `session_factory` param; when present, emit one `entity.upsert`/`edge.upsert` OutboxEvent per entity/edge; else legacy direct write with deprecation log.
- **Rebuild** (`scripts/rebuild_all_projections.py`): SANCTIONED exception. Add a structured warning header + new `docs/adrs/0015-rebuild-direct-write-exception.md` (precondition: empty/wiped stores; postcondition: run reconcile scan; rationale: NATS may be down during DR).

### Worker/consumer handlers + mypy fixes
- `outbox_worker.py`: `OutboxEvent.processed == False` (not `not ...`); per-branch typed vars (line 93); add `entity.merge`/`entity.unmerge` routing to NATS subjects.
- `outbox_consumer.py`: annotate `msg: Any` (129); `Source.workspace_id` → `Source.tenant_id` (163,181,303 — runtime bug); per-branch typed vars (181,184); `embed_query` → `embed([text])[0]` (257); `cast(ChunkPayload, chunk)` (284); `min(d, key=lambda k: d[k])` (335,383,430); add `entity.merge`/`entity.unmerge` → `graph_store.merge_nodes()`/`unmerge_node()`.
- `Neo4jGraphStore.get_entity_versions` (neo4j/store.py): implement `MATCH (e:Entity {workspace_id}) RETURN e.id, e.version`. Add to protocol. Resolves reconcile_worker.py:282.

### AP1 tests
- Unit: outbox worker sets `entity_id`; linker emits OutboxEvent not direct write; REST merge/unmerge insert OutboxEvent + return success; consumer routes entity.merge/unmerge; corrected `Source.tenant_id` join.
- Integration (docker UP): `tests/integration/test_single_writer_invariant.py` — linker path emits no direct store call, OutboxEvent row has entity_id, worker tick projects to Neo4j+Qdrant, version-guard keeps higher version. Extend `test_outbox_fault_injection.py` for merge-via-outbox; exercise the real `_unpark_loop` (don't clear `_parked_entities` manually).

## AP2 — Closed-loop reconciliation + per-entity anti-entropy

**Approach**: store `content_hash` on `Entity` (Postgres SoT), project to Neo4j node prop + Qdrant
payload, compare per-entity hashes in `_check_entity_drift`, re-emit corrective OutboxEvents for the
two detect-only drift classes. Reuse `compute_state_fingerprint` (BLAKE2b-256, canonical JSON, sort_keys).

### Migration 0014 — `0014_entity_content_hash.py` (NEW)
- Add nullable `content_hash: Text` to `entities` + index. Backfill existing rows offline; NOT NULL in a follow-up (0014b).
- Model: `Entity.content_hash: Mapped[str | None]`.

### Hash util — `audit/fingerprint.py`
- `entity_content_hash(entity_type, name, display_name, metadata) -> str` = `compute_state_fingerprint({...})`. Exclude id/source_id/chunk_id/version/created_at (provenance, not content).

### Projection
- Ingestion pipeline: set `entity.content_hash` on every Entity write.
- Neo4j: add `content_hash` node prop in upsert Cypher; add `content_hash` to `EntityUpsert` dataclass.
- Qdrant/consumer: use `entity_content_hash(...)` (NOT the inline SHA-256 of embedding text) so all three stores compare the same value; add `content_hash` to chunk metadata + `EntityUpsertEvent` (optional field, forwarded from Postgres).

### Reconcile anti-entropy — `reconcile_worker.py`
- Add `get_entity_content_hashes(*, workspace_id) -> dict[UUID,str]` to both stores + protocols.
- `_check_entity_drift`: drift if `version_drift OR (pg_hash is not None AND store_hash != pg_hash)`.
- `_check_pg_missing_qdrant`: re-emit `entity.upsert` (is_backfill=True) for all entities in missing sources (was detect-only).
- `_check_neo4j_orphans`: emit `source.orphan_detected` durable event for the retention worker (behind `ENABLE_NEO4J_ORPHAN_REEMIT`), keep deferral to retention for actual end-dating.

### AP2 tests
- Unit: hash stable + changes on field change; `_check_entity_drift` detects same-version hash mismatch; `_check_pg_missing_qdrant` re-emits.
- Integration (docker UP): `test_anti_entropy_content_hash.py` (corrupt Neo4j content_hash → reconcile detects → heals); `test_pg_missing_qdrant_heal.py` (delete Qdrant points → reconcile re-emits → consumer restores). Round-trip hash determinism (write→read→recompute→equal).

## Implementation sequence (must follow)
1. Migration 0013 + model. 2. Protocol fixes (graph.py, full_store.py). 3. outbox_consumer mypy (incl. `Source.tenant_id`). 4. `Neo4jGraphStore.get_entity_versions`. 5. New event types in worker+consumer. 6. REST bypass (flag `MERGE_VIA_OUTBOX`). 7. Linker bypass. 8. Operator-graph bypass. 9. ADR-0015. Then AP2: 10. Migration 0014 + backfill. 11. content_hash on writes. 12. project to Neo4j/Qdrant. 13. reconcile hash compare + close loops.

## Scope guards
- NO per-entity NATS subjects, NO JetStream stream/consumer reconfiguration this iteration.
- Bundle the ~19 unrelated mypy one-liners (pb_events, db/models annotations, neo4j inner-fn annotations, writer/postgres_only Literal action, qdrant_store union-attr, app.py:430 missing await, queue/consumer.py:248 datetime import, compile_proto, federation) into the cleanup batch for a 0-error mypy run.
