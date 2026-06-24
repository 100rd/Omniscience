# Consilium v8 — Architectural Review of `main`

> **Scope**: Read-only audit of the Omniscience codebase (`main`) against the 12 Action Points of
> the Opus vs Gemini Pro consilium **v8** (Opus chair). Verdicts are based on **actual code**, not
> commit messages. Evidence cited as `path:line`. No code modified; nothing applied/deployed.
>
> **Method**: dev-team Lead + parallel specialist auditors (backend ×2, security/QA); targeted
> `pytest` runs used to confirm bug fixes and regressions.
>
> **Date**: 2026-06-23 · **HEAD**: `319a620` · **Prior review**: [`consilium-v7-review.md`](consilium-v7-review.md)

## Verdict (echoing the Opus chair)

**NOT production-ready until the P0 class is closed — but the iteration loop is working.** The v8
audit confirms the chair's framing: the foundation is strong and rare for its class (Postgres-SoT +
bitemporal graph + semantics + MCP-first + retrieval-only). The blockers remain the three defect
classes the chair named — (1) three-store convergence under partial failure, (2) read-path
consistency under epoch-skew, (3) trust in the numeric confidence label. None is cosmetic; all sit on
the core value proposition ("citable evidence **with** confidence").

**Score: 0 Done · 11 Partial · 1 Missing.** All four P0s are Partial — real machinery exists for each,
but none is fully closed. Critically, **the last iteration fixed 4 of the worst v7 defects** (see delta
below), so the trend is correct; what remains is finishing partial work and proving claims, not redesign.

## What the last iteration (v7 → v8) actually fixed ✅

| Area | v7 finding (broken) | v8 status |
|------|--------------------|-----------|
| AP3 read-path | Reconciler filtered `Document.source.tenant_id` (relationship) → **runtime crash** | **Fixed** — proper `.join(Source, Document.source_id == Source.id).where(Source.tenant_id == …)` (`reconciler.py:55`) |
| AP5 DR | Rebuild passed `version=None` → checkpoints never advanced → reconciler stalls post-DR | **Fixed** — `version=doc.doc_version` now passed (`rebuild_all_projections.py:171, 198`) |
| AP7 bitemporal | `QdrantFilterBuilder.build()` had `if as_of: pass` → **no-op, 4 failing tests** | **Fixed** — calls `_as_of_must_clauses` (`qdrant_filters.py:181-182`); **19 tests pass** |
| AP9 chaos | Zero fault-injection; the one chaos test broken (`client_name` TypeError) | **Improved** — `tests/test_outbox_fault_injection.py` added (fault→park→detect→re-emit→heal); `test_epoch_forced_replay.py` no longer broken |

## Verdict Matrix

| # | Action Point | Prio | Verdict | One-line reason |
|---|--------------|------|---------|-----------------|
| 1 | Single-writer invariant (all store writes via SoT outbox, idempotent, ordered per-entity) | P0 | 🟡 Partial | **4 bypass paths** write Neo4j/Qdrant directly (linker, operator-graph, REST merge/unmerge, rebuild); only **global** FIFO, no per-entity ordering; `OutboxEvent` has no `entity_id` column |
| 2 | Closed-loop reconciliation + per-entity anti-entropy | P0 | 🟡 Partial | Entity-version drift **does** re-emit & heal (tested); but `pg_missing_qdrant`/`neo4j_orphan` are detect-only; anti-entropy is version-int, **not content-hash**; doc/chunk-hash drift uncaught |
| 3 | Read-path exposes consistency (watermark pin / explicit staleness; no silent epoch-skew) | P0 | 🟡 Partial | v7 crash fixed, but **timeout still serves stale silently** (no `degraded_subsystems`); no atomic cross-store watermark; `doc_version` ≠ global LSN; `GlobalReconciler` has **0 tests** |
| 4 | Confidence: calibration with Brier/ECE **in CI**, or drop the numeric label | P0 | 🟡 Partial | Neither bar met — CI gate is **aspirational** (no assert step); `support_size=0` hardcoded disables isotonic; fitted pipeline never loaded into runtime; uncalibrated float still on the wire |
| 5 | DR rebuild-from-SoT with RTO budget + CI/staging drill | P0 | 🟡 Partial | v7 `version=None` fixed, but **no `ORDER BY`** (non-deterministic), no post-rebuild verification, **no RTO budget, no CI/staging DR drill** |
| 6 | Identity: reversibility + conservative threshold + review queue | P1 | 🟡 Partial | Unmerge provenance sound; **conservative auto-merge threshold MISSING** (any score>0 links); **review queue MISSING**; tag-poisoning + no domain guard unchanged; unmerge REST lacks admin scope |
| 7 | as_of conformance across 3 stores (esp. Qdrant valid/tx-time) | P1 | 🟡 Partial | Qdrant no-op **fixed**; but **no unified cross-store conformance test** (only in-proc simulator); **transaction-time dimension absent entirely** → uni-temporal, not bi |
| 8 | Prove GraphRAG > vector-only via benchmark | P1 | 🔴 Missing | Harness is **vendor-vs-vendor** with estimated competitor numbers; **no GraphRAG-vs-vector-only ablation**; CI gate runs in mock mode (zero scores, no assert). Claim unproven |
| 9 | Property/chaos convergence tests (inject failure, assert convergence + as_of invariant) | P1 | 🟡 Partial | New fault-injection test (Qdrant crash→heal) + simulator cross-store property; but **no JetStream-failure test, no split-brain reconvergence test, no as_of-invariant-under-fault**; property is simulator-only |
| 10 | MCP surface: structural retrieval-only guarantee + unified contracts | P1 | 🟡 Partial | **No structural guarantee** (convention only); `generate_postmortem` is a **synthesis** tool; budget on only **6 of 14** tools; confidence/citations/as_of/pagination scattered ad-hoc; **no conformance test**; no `next_cursor` anywhere |
| 11 | Consistency observability (per-store lag, outbox depth, DLQ depth, drift count) + alerts | P1 | 🟡 Partial | Drift counter + parked-entity gauges + alerts exist; but **per-store lag, outbox depth, DLQ depth all MISSING**; server-side reconcile-drift **unalerted** (only operator chart) |
| 12 | full/lite degradation contract + conformance test (lite must not over-claim) | P2 | 🟡 Partial | Lite silently **ignores `as_of`** (returns current state); `degraded` flag exists but **never activated** (defaults False, `app.py:178`); no completeness signal in results; no conformance test |

Legend: 🟢 Done · 🟡 Partial · 🔴 Missing

---

## AP1 (P0) — Single-writer invariant — 🟡 Partial
The outbox consumer is the canonical write path, but **four paths bypass it**:
- **Linker**: `packages/index/src/omniscience_index/linker.py:70-82` calls `graph_store.upsert_edge()` directly for every cross-source link (hit each ingestion run via `ingestion/pipeline.py:652`).
- **Operator-graph bridge**: `ingestion/worker.py:376` → `ingestion/operator_graph.py:301` calls `graph_store.upsert_graph()` directly; no outbox event.
- **REST merge/unmerge**: `rest/entities.py:386` (`merge_nodes`) and `:413` (`unmerge_node`) write the graph directly; no outbox event.
- **Rebuild script**: `scripts/rebuild_all_projections.py:163, 193` (intentional for DR, but still violates the stated invariant).

**Ordering & idempotency:**
- Single durable NATS consumer on `outbox.entity.upsert`, no per-entity subject/partition (`outbox_consumer.py:88-103`). `outbox_worker.py:73` orders by `created_at ASC` **globally** — FIFO across all entities, not per-entity FIFO.
- Version guard makes individual writes idempotent (`neo4j/store.py:310` `existing_version >= version → skip`; same in `qdrant_store.py`), so stale replays are dropped — but this does not enforce per-entity ordering across concurrent writers.
- **`OutboxEvent` has no `entity_id` column** (payload-only JSONB) → per-entity selective replay/ordering is impossible from the table.

**Gaps:** close the 4 bypasses (route through outbox or document them as exceptions); add a per-entity partition key / subject `outbox.entity.upsert.{entity_id}`; add an `entity_id` column.

## AP2 (P0) — Closed-loop reconciliation + anti-entropy — 🟡 Partial
**Closed loop works for entity-version drift:** `reconcile_worker.py:272-315` (`_check_entity_drift`) reads `Entity` versions from Postgres, compares to `vector_store.get_entity_versions` / `graph_store.get_entity_versions`, and **re-emits** `OutboxEvent(event_type="entity.upsert", payload={…,"is_backfill":True,"version":…})` for any lagging store. The worker republishes; the consumer heals. Tested: `tests/test_reconcile_worker.py:511-592`.

**Gaps:**
- `_check_pg_missing_qdrant` (`reconcile_worker.py:321-346`) and `_check_neo4j_orphans` (`:445-472`) are **detect-only** — mark run `partial` / emit metric, no corrective re-emit.
- Anti-entropy is a **per-entity version integer** compare, **not a content_hash**. Document/chunk drift (chunk exists with wrong content hash) is not caught.
- Parked-entity clearance across processes is acknowledged-fragile (`:308-315`): the backfill `is_backfill=True` must clear the consumer's in-memory park set before the re-check at `outbox_consumer.py:224-231` or the heal event lands in DLQ.

## AP3 (P0) — Read-path consistency — 🟡 Partial
v7 runtime crash **fixed** (proper join, `reconciler.py:55`). Remaining:
- **Silent stale on timeout**: `reconciler.py:43-46` logs `global_reconciler_timeout` and returns; `graph_rag.py:393-394` continues serving with **no staleness signal** — no `degraded_subsystems`, no staleness-age, no header.
- **No atomic cross-store watermark**: `check_convergence` reads `max(doc_version)` per source, Qdrant checkpoint payloads, and Neo4j `StoreCheckpoint` nodes as **separate** reads → epoch-skew between stores can make convergence flap non-monotonically.
- `doc_version` is **per-source max, not a global LSN** — no total order across stores.
- **`GlobalReconciler` has 0 tests** (`grep GlobalReconciler tests/` = 0). `test_graph_rag_degraded.py` covers a different signal (pre-history `as_of`), not convergence/timeout.

## AP4 (P0) — Confidence calibration in CI, or drop the label — 🟡 Partial
Neither v8 condition is met:
- **Calibration not enforced in CI**: `.github/workflows/benchmark.yml:59-78` `pr-regression-gate` comment claims it fails the PR on accuracy drop, but **there is no assertion step** — it uploads the artifact and runs unit tests. Aspirational documentation, not a gate.
- **Isotonic path is dead at inference**: `incidents.py:177` hardcodes `support_size=0` ("cold start assumed") → `probabilistic_scoring.py:120-123` blends to full Platt fallback every request; fitted `CalibrationPipeline.run()` output (`incidents/calibration.py:314-353`) is **never serialized/loaded** into `calibrate_isotonic` (hardcoded thresholds, `probabilistic_scoring.py:96-99`).
- **Uncalibrated float still surfaced**: `incidents.py:191` returns `resolution_confidence` to MCP/REST; an `uncalibrated: bool` flag (`:194`) is informational only — the number is not withheld.
- Offline Brier/ECE asserts exist (`tests/test_calibration_pipeline.py:143,157`) but run on synthetic data, not the live path.

## AP5 (P0) — DR rebuild + RTO + CI drill — 🟡 Partial
v7 `version=None` **fixed** (`rebuild_all_projections.py:171, 198`); `test_epoch_forced_replay.py` no longer broken. Remaining:
- **No `ORDER BY`** on documents (`:121-122`) → non-deterministic processing order (chunks within a doc are ordered, `:131`).
- **No post-rebuild verification** (no checkpoint-vs-Postgres count/hash assertion; just `print("Rebuild complete.")`).
- **No RTO budget** defined or measured. **No CI/staging DR drill** — `.github/workflows/ci.yml` runs only `pytest --cov`; `tests/integration/test_replay.py` tests bitemporal determinism (planted Cypher), **not** a rebuild-from-SoT drill.

## AP6 (P1) — Identity: threshold + review queue + reversibility — 🟡 Partial
- **Reversibility sound**: merge/unmerge with `MERGED_INTO` + `original_node_id` provenance (`neo4j/store.py:1114-1328`).
- **Conservative threshold MISSING**: `linker.py:113` creates an edge for **any pair scoring > 0.0**; `RESOURCE_MATCH_THRESHOLD=0.5` (`:35`) applies only to Tf↔K8s; other strategies are binary 1.0/0.0. No high-confidence-only auto-merge gate.
- **Review queue MISSING**: no `pending`/`review_queue`/`merge_candidate` table or model anywhere; probabilistic matches auto-link immediately.
- **Carried v7 risks**: `tags_match` returns 1.0 on any shared tag key (`matchers.py:207-210`) → poisoning; `merge_nodes` has no domain/kind guard (`store.py:1130-1136`); **`unmerge_entity` REST handler lacks the `admin/entities:write` runtime check** that `merge_entities` has — any `Scope.search` token can unmerge (`rest/entities.py:396-421`).

## AP7 (P1) — as_of conformance across 3 stores — 🟡 Partial
- Qdrant no-op **fixed** (`qdrant_filters.py:181-182, 201-241`); `tests/test_qdrant_filter_builder.py` **19 pass**.
- **No unified cross-store conformance test** issuing one `as_of` query to Postgres + live Neo4j + live Qdrant and asserting identical result sets. `tests/property/test_bitemporal_contract.py:320-354` (Property 8) is **simulator-only**; `tests/test_mcp_as_of.py` uses an in-memory fake store.
- **Transaction-time absent**: `recorded_at` exists on Neo4j (`_cypher.py:66-92`) but is used only for retention tiering (`:441-459`); no `recorded_at <= as_of` predicate in any read path → **valid-time only, not bitemporal**.

## AP8 (P1) — Prove GraphRAG > vector-only — 🔴 Missing
- Harness is real (`bench/runner.py`, `scorers.py`, `schema.py`, 50 incident fixtures) and there are published results (`bench/results/2026-Q2.md`: Omniscience top-1=0.840 vs estimated competitors) — but it is a **vendor-vs-vendor leaderboard**, not a mechanism ablation.
- **No `--mode vector-only` / `OmniscienceVectorOnlyAdapter` / ablation row**; the runner treats Omniscience as a black box and cannot tell whether GraphRAG or pure vector was used internally.
- CI gate (`.github/workflows/benchmark.yml:42-78`) runs the adapter in **mock mode** → all-zero scores, **no threshold assert**; the "fails the PR" comment is aspirational.
- Three of four competitor numbers are marked `[estimate]` (8/50 fixtures). **The "GraphRAG beats vector-only" claim has no empirical backing in any committed artifact.**

## AP9 (P1) — Property/chaos convergence — 🟡 Partial
- **New**: `tests/test_outbox_fault_injection.py` injects a Qdrant crash → 3 retries + park → reconciler detects → re-emits → replay heals → `ack`. Genuine fault→heal coverage.
- `tests/property/test_bitemporal_contract.py` Property 8 (120 Hypothesis examples) asserts cross-store visibility invariant — but against the **in-process simulator**, not live stores.
- **Gaps**: no JetStream-failure test (the `_unpark_loop` at `outbox_consumer.py:147-205` is bypassed via manual `_parked_entities.clear()`); no split-brain reconvergence test (Neo4j v5 / Qdrant v3 → converge); no "`as_of` returns identical result before/after a fault window" invariant.

## AP10 (P1) — MCP retrieval-only + unified contract — 🟡 Partial
- **14 tools** registered (`mcp/server.py`): search, get_document, get_entity, get_related_entities, list_entities, list_sources, source_stats, resolve_incident, incident_timeline, blast_radius, replay_context, suggest_runbook, find_similar_incidents, generate_postmortem.
- **No structural retrieval-only guarantee** — no decorator/registry/type enforces it; it's convention. No handler mutates state, **but `generate_postmortem` is a synthesis tool** (assembles a new narrative via `postmortem/generator.py`), which is not "retrieval-only" in spirit.
- **Budget (`_apply_mcp_budget`, 80k chars, `server.py:142-153`) on only 6 of 14 tools**; unbounded: get_document, get_entity, get_related_entities, list_entities, replay_context, suggest_runbook, find_similar_incidents, generate_postmortem.
- **Contract not unified**: `effective_as_of`, `confidence`, `citations` are each present on only a subset; **no `next_cursor` in any response** (search accepts a `cursor` input but nothing returns one); **no cross-tool conformance test**.

## AP11 (P1) — Consistency observability — 🟡 Partial
- **Present & emitted**: `omniscience_reconcile_drift_total{drift_type}` (`reconcile_worker.py`, `metrics.py:202`); parked-entities/edges gauges + oldest-age (`outbox_consumer.py:23-74`); source freshness/stale gauges (`metrics.py:43`). Alert rules in `monitoring/prometheus/alerts/{outbox,freshness,retention}.yaml`.
- **Missing**: per-store projection **lag** gauge (`{store=neo4j|qdrant}`); **outbox depth/backlog** gauge (only a debug log of pending count, `outbox_worker.py`); **DLQ depth** gauge (DLQ routing exists, no size metric).
- **Alert gap**: server-side `omniscience_reconcile_drift_total` has **no alert rule** — the only reconcile-drift alert is the operator chart's `OmniscienceOperatorReconcileDriftHigh` (different metric namespace). Sustained server-side drift goes unalerted.

## AP12 (P2) — full/lite degradation contract — 🟡 Partial
- Lite (`PostgresOnlyStore`, active when both backends = postgres, `app.py:175`) loses: native graph traversal (recursive CTE, no graph algos, `postgres_only_store.py:371-509`), hybrid/sparse vector search (`text_matches=0` hardcoded, `:877`), **bitemporal `as_of` (accepted then silently ignored — returns current state)**, and merge/unmerge.
- **Degraded signal not honest**: a `degraded: bool=False` flag exists (`:46`, applies a 0.8 score penalty at `:835`) but is **never activated** — `app.py:178` instantiates with the default `False`. No `store_mode`/`completeness` field in `SearchResult`; `as_of` skips filtering with **no signal to the caller**.
- **No conformance test** asserting lite results carry a completeness signal (`tests/test_postgres_only_store.py` tests CRUD/search only). **Reliability risk**: an LLM agent issuing an `as_of` query in lite mode silently receives current-state data and may draw wrong time-anchored conclusions.

---

## Prioritized Remaining Work

### P0 — must close before production (per the chair's verdict)
1. **AP1** — Route the 4 bypass writers (linker, operator-graph, REST merge/unmerge) through the outbox, or formally document them as exceptions; add an `entity_id` column + per-entity subject/partition for per-entity ordering. *(Highest-leverage: it is the precondition for AP2/AP3 to mean anything.)*
2. **AP3** — On reconciler timeout, **surface staleness** (`degraded_subsystems` + staleness-age) instead of silently serving stale; add tests for `GlobalReconciler` (currently 0); move toward a monotonic cross-store watermark.
3. **AP4** — Either (a) wire the fitted `CalibrationPipeline` output into the live isotonic path, stop hardcoding `support_size=0`, and add a **real CI assert** on Brier/ECE; or (b) drop the numeric decimal confidence from MCP/REST until calibrated. Pick one — the current middle state ships an uncalibrated number.
4. **AP2** — Add corrective re-emit for `pg_missing_qdrant` and `neo4j_orphan` drift classes; move anti-entropy to per-entity **content_hash**, not just version-int.
5. **AP5** — Add `ORDER BY` for deterministic rebuild; add post-rebuild verification (checkpoint vs SoT counts/hash); define an **RTO budget** and add a **CI/staging DR drill** job.

### P1
6. **AP6** — Add a conservative auto-merge threshold + a review queue for probabilistic matches; add the missing admin-scope check to `unmerge_entity`; add a domain/kind guard to `tags_match`/`merge_nodes`.
7. **AP8** — Add an `Omniscience-vector-only` ablation row to the harness and publish GraphRAG-vs-vector-only numbers on the causal-ops corpus; make the CI gate actually assert. (Until then, soften the "GraphRAG wins" claim.)
8. **AP9** — Add JetStream-failure and split-brain-reconvergence tests; add an "`as_of` invariant across a fault window" property.
9. **AP10** — Add a structural retrieval-only guarantee (registry/type + conformance test); apply the budget to all tools; unify the response contract (confidence/citations/as_of/pagination) and add `next_cursor`. Decide whether `generate_postmortem` belongs on a retrieval-only surface.
10. **AP7** — Add a unified live cross-store `as_of` conformance test; add the transaction-time (`recorded_at <= as_of`) predicate to reach true bitemporality.
11. **AP11** — Add per-store lag, outbox depth, and DLQ depth gauges; add an alert on server-side `omniscience_reconcile_drift_total`.

### P2
12. **AP12** — Activate the lite `degraded` flag in `app.py`; add a `store_mode`/`completeness` signal to results; either honor `as_of` in lite or return an explicit "bitemporal unsupported in lite" marker; add a conformance test that lite never over-claims evidence completeness.

---

## Appendix — Confirmed runtime evidence
- `pytest tests/test_qdrant_filter_builder.py` → **19 passed** (AP7 no-op fixed since v7).
- `pytest tests/test_reconcile_worker.py` → backfill re-emit asserted at `:511-592` (AP2 closed loop).
- `tests/test_outbox_fault_injection.py` exists (AP9 fault→heal).
- `grep -rn "GlobalReconciler" tests/` → 0 (AP3 untested).
- `grep -rn "review_queue\|merge_candidate\|pending.*merge" packages apps` → none (AP6 no review queue).
- `.github/workflows/benchmark.yml` `pr-regression-gate` → no assertion step (AP4/AP8 gates aspirational).
