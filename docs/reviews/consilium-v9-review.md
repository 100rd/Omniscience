# Consilium v9 — Architectural Review of `main`

> Read-only audit of `main` (after consilium-v8 P0 merged, squash `b215bae`) against the 4 Action
> Points of consilium v9 (Gemini Pro chair). Verdicts from actual code; evidence as `path:line`.
> v9 deliberately targets the **compromises consilium v8 knowingly made** — so each verdict is framed
> as the delta still owed beyond v8.
>
> **Date**: 2026-06-24 · **Method**: 2 parallel backend auditors.

## Verdict Matrix

| # | Action Point | Prio | Verdict | One-line |
|---|--------------|------|---------|----------|
| 1 | Strict Ordering via JetStream Partitioning | P0 | 🟡 Partial | v8 shipped `entity_id` column + version-guard backstop and **explicitly deferred** real partitioning; transport still flat subjects + global `created_at` poll, no per-entity ordering guarantee under concurrent consumers |
| 2 | Causal Reconciliation (re-emit from SoT) | P0 | 🟡 Partial | Entity-level closed-loop works, but **edges are never re-emitted on drift**, there is **no causal/topological ordering**, and `neo4j_orphan` re-emit is flag-off + diagnostic-only |
| 3 | Remove Fake Confidence | P1 | 🟡 Partial | The "fake" concern is real: default path returns the **v0.1 ladder constants**; no committed calibration artifact so the isotonic path is never reached; `support_size` always 30; uncalibrated decimal shown at full precision |
| 4 | Bulk-Load for DR Pipeline | P2 | 🔴 Missing | Rebuild is per-document Qdrant upsert + per-entity/per-edge `tx.run`; **no `UNWIND`/bulk path**; ~750s Neo4j alone at 100k chunks → **breaches the 900s RTO** |

Legend: 🟢 Done · 🟡 Partial · 🔴 Missing

---

## AP1 (P0) — Strict Ordering via JetStream Partitioning — 🟡 Partial
**Present (v8 backstop):** `entity_id` column + composite index (`alembic/versions/0013_outbox_entity_id.py:34-48`, `db/models.py:571-604`); populated on emit (`ingestion/operator_graph.py:334,358`); idempotent version-guard in store adapters.
**Missing for v9 strict transport ordering:**
- Worker polls/publishes by **global `created_at`** with no per-entity grouping — a 50-event batch interleaves N entities (`outbox_worker.py:79-81`).
- OUTBOX stream uses flat subjects `outbox.*` → `outbox.entity.upsert` / `outbox.edge.upsert` / `outbox.entity.merge` (`queue/streams.py:51-58`); **no per-entity subject** like `outbox.entity.upsert.{entity_id}`.
- Consumers: 3 `QueueConsumer` on flat subjects, **no `FilterSubjects`, no `max_ack_pending=1`, no ordered consumers** (`outbox_consumer.py:100-126`, `queue/consumer.py:104-113`).
- Net: two events for the same entity under concurrent consumers rely **solely on the version-guard discarding the lower version** — not transport-level ordering.
**Delta owed:** per-entity subject partitioning (stream wildcard `outbox.>`) + per-partition single-flight consumers (`FilterSubjects` + `max_ack_pending=1`) OR sequential per-entity publish; keep version-guard as safety net.

## AP2 (P0) — Causal Reconciliation (re-emit from SoT) — 🟡 Partial
**Present:** `_check_entity_drift` re-emits `entity.upsert` on version drift **and content_hash mismatch** (`reconcile_worker.py:288-359`, hashes at 306-309); `_check_pg_missing_qdrant` → `_reemit_entities_for_missing_sources` is closed-loop (`:365-395`, `:598-654`).
**Missing for v9 causal reconciliation:**
- **Edges are never re-emitted.** `_reemit_entities_for_missing_sources` selects only `Entity` rows; `_check_entity_drift` only entities; `Edge` is not imported in `reconcile_worker.py`. No `get_edge_versions()`/`get_edge_content_hashes()` on either store → edge drift is structurally undetectable.
- **No causal/topological ordering.** Re-emit iterates entities in Postgres scan order. The consumer has an `_auto_unpark_edges` causal guard (`outbox_consumer.py:365-407`) but the reconcile path never emits dependent edges, so it's never exercised from reconciliation.
- `_check_neo4j_orphans`: `ENABLE_NEO4J_ORPHAN_REEMIT` **defaults false** (`:108-110`); when on, emits a `source.orphan_detected` **diagnostic** marker (`:521-525`), not a re-projection.
**Delta owed:** detect + re-emit drifted **edges** (new store API), **topologically order** entities-before-edges in the backfill, make orphan re-emit on-by-default and actually re-project.

## AP3 (P1) — Remove Fake Confidence — 🟡 Partial
**The "fake" concern is confirmed on the default live path:**
- `score_incident(config=None)` returns `_v01_ladder(...)` → one of `CONFIDENCE_PR_TEMPORAL_MATCH=0.9 / _NO_TEMPORAL_MATCH=0.6 / _RESOURCE_ONLY=0.4 / _ALERT_ONLY=0.1` (`resolution.py:424-428`, constants `:53-57`). No `calibrate_isotonic` on this path.
- `_load_scoring_config` returns `None` when no per-workspace `incident_scoring` config (or no db session) → ladder is the default (`incidents.py:258-269`). Even WITH weights, `score_incident` calls `apply_weights` (linear), **never `calibrate_isotonic`** (`:429-445`).
- **No committed `calibration_artifact.json`** → `load_calibration_artifact` returns None permanently → `is_fitted_map_loaded()` = False on every fresh deploy (`calibration_store.py:194`, `probabilistic_scoring.py:157-166`, `incidents.py:220`).
- `support_size` is always `DEFAULT_SUPPORT_SIZE=30` (no caller supplies a real count) (`probabilistic_scoring.py:44`, `incidents.py:170`).
- `calibrated` is injected post-`model_dump` (`incidents.py:245-248`); not in `ResolveIncidentResponse` (`resolution.py:163-202`).
- When `calibrated=False`, the decimal is **shown at full precision** — e.g. `0.6` for a PR with no temporal match — a lookup constant dressed as a probability.
**Delta owed (pick one, per v9):** either make the surfaced number genuinely calibrated by default (commit/load a fitted artifact, route `score_incident` through `calibrate_isotonic`, real `support_size`) **OR** suppress/round the decimal and surface a qualitative band when `calibrated=False`. v9's framing ("remove fake confidence") favors the latter unless calibration is made real end-to-end.

## AP4 (P2) — Bulk-Load for DR Pipeline — 🔴 Missing
- Qdrant: one `upsert(points=...)` **per document** (`rebuild_all_projections.py:381,417`; `qdrant_store.py:651-652`); no `upload_collection`/`upload_points` cross-document bulk.
- Neo4j: per-entity and per-edge `tx.run` loops (`neo4j/store.py:377-386`, `:393-420`); no `UNWIND $rows`/`apoc.periodic.iterate`.
- Embeddings are reused from `chunk.embedding` (`rebuild_all_projections.py:395-399`) — not the bottleneck.
- **Quantified:** ~100k chunks / ~10k docs → ~150k Neo4j Cypher executions ≈ **~750s for Neo4j alone** at 5ms RTT, before Qdrant/Postgres/checkpoints → **breaches 900s RTO**. `UNWIND` batching could cut Neo4j round-trips ~100×.
**Delta owed:** `UNWIND`-batched entity/edge upsert in a rebuild-specific path; cross-document Qdrant batch upload.

---

## Prioritized remediation (v9)
1. **AP2 (P0)** — edge re-emit + topological (entities→edges) ordering + orphan re-project on-by-default. (Builds directly on v8's outbox/reconcile; highest correctness value.)
2. **AP1 (P0)** — per-entity subject partitioning + single-flight per-partition consumers; keep version-guard as backstop. (Transport change to JetStream config — the riskiest; needs care.)
3. **AP3 (P1)** — decide calibrate-for-real vs suppress-the-decimal; v9 leans suppress unless the isotonic path is wired end-to-end with a committed artifact + real support counts + `calibrated` in the schema.
4. **AP4 (P2)** — `UNWIND` batch Neo4j + Qdrant bulk upload in the rebuild path to hold the 900s RTO at scale.
