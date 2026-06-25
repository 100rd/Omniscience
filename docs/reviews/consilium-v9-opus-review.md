# Consilium v9 (Opus chair) — Forensic Review of `main`

> Adversarial read-only audit of `main` (consilium-v8 P0 merged, squash `b215bae`) against the
> 10 Action Points of the Opus v9 loop. The chair explicitly distrusts the "Close P0 blockers"
> commit and demands an **evidence bundle**: every closed P0 must have a reproducible CI fail-gate,
> else status = "claimed", not "closed". Verdicts from actual code/CI; evidence as `path:line`.
>
> **Date**: 2026-06-24 · **Method**: 2 parallel forensic auditors. **Bottom line:** the chair is
> right on the highest-value axis — **AP3 read-path consistency was closed DECLARATIVELY** (a label,
> not a pin), and the **evidence bundle (AP6) is largely absent**.

## Verdict Matrix

| # | Action Point | Prio | Verdict | One-line |
|---|--------------|------|---------|----------|
| 1 | Version-guarded conditional writes in relay | P0 | 🟡 Partial | Guard is **per-source checkpoint**, not per-entity CAS; the exact v3-then-v2 same-entity reorder IS blocked, but via a coarser source-level lock, not a per-entity `applied_version` |
| 2 | Anti-entropy: content-hash + tombstone + bounded re-emit | P0 | 🟡 Partial | Qdrant orphan cleanup bounded; **Neo4j orphan detect-only**; **entity re-emit is fully UNBOUNDED** (storm risk); edges still not re-emitted |
| 3 | Read-path real pin to min-watermark + no mixed-epoch | P0 | 🔴 **Missing** | **"Closed declaratively" confirmed.** It's a LABEL not a PIN — GraphRAG composes **fresh Neo4j + lagged Qdrant** on every convergence timeout, annotating `degraded_subsystems` but serving mixed-epoch anyway. No pin, no reject |
| 4 | Confidence: Brier/ECE CI fail-gate OR remove the number | P0 | 🔴 Missing | Binary axis not closed: live path returns **v0.1 ladder constants**; `calibration-gate.yml` tests the pipeline in isolation, **path-triggered**, and does **not** protect the number users see |
| 5 | DR-drill volumetric + hash-assert + embeddings-from-SoT | P0 | 🟡 Partial | **Rebuild reads stored `chunk.embedding` → restore-from-backup, NOT rebuild-from-SoT** (seed even pre-loads zero-vectors to bypass embedding); no hash-equivalence assert; 9-chunk toy fixture; nightly-only |
| 6 | Evidence bundle: a CI fail-gate per P0 (meta-gate) | P0 | 🔴 Missing | AP1 soft (in pytest), AP2 soft only, **AP3 no gate**, **AP4 gate doesn't cover live path**, **AP5 nightly+toy**. Most P0s are "claimed", not "gated" |
| 7 | Arch-test: single-writer + MCP retrieval-only invariant | P1 | 🔴 Missing | No import-linter/AST arch-test; `lint_stale_architecture.py` only checks comments/ADRs; `generate_postmortem` is a **synthesis tool** on the MCP surface |
| 8 | Cross-domain identity: reversible merge/split + provenance | P1 | 🟡 Partial | Provenance + unmerge exist; **no conservative auto-merge threshold, no review queue** for probabilistic matches |
| 9 | as_of conformance across 3 stores | P1 | 🟡 Partial | Per-store as_of tests in CI; **cross-store consistency is simulator-only**; **tx-time (`recorded_at`) filtering absent** from read paths |
| 10 | DLQ / poison-message policy for relay | P2 | 🟢 Done* | Real DLQ + parking; per-entity park avoids HOL for others. Caveats: in-memory parking lost on restart, park↔unpark cycle for permanently-poisoned, contract undocumented |

Legend: 🟢 Done · 🟡 Partial · 🔴 Missing · * = with caveats

---

## AP1 (P0) — Version-guarded conditional writes — 🟡 Partial
- Guard reads a **`StoreCheckpoint` keyed on `{workspace_id, source_id}`** and skips if `existing_version >= incoming` (`neo4j/store.py:302-342,454-496`; `qdrant_store.py:528` one checkpoint point per `source_id`).
- Consumer applies via `upsert_entity` which checks the **source-level** checkpoint, not the entity node's own `version` (`outbox_consumer.py:281-296`).
- The exact reorder case (v3 then v2 for the same entity) IS correctly blocked because the source checkpoint advances monotonically — but this is a **coarser per-source lock**, not a per-entity conditional apply. No CAS on the individual Entity node/point version.
- **Delta owed:** read `e.version` from the entity itself, compare, skip if stale — true per-entity `applied_version`.

## AP2 (P0) — content-hash + tombstone + bounded re-emit — 🟡 Partial
- **Qdrant orphan (tombstone) cleanup is present + bounded** by `RECONCILE_ORPHAN_DELETE_LIMIT` (`reconcile_worker.py:401-443`).
- **Neo4j orphan cleanup is detect-only** — logs + optional `source.orphan_detected` diagnostic behind `ENABLE_NEO4J_ORPHAN_REEMIT` (default off); actual end-dating deferred to the retention worker (`:494-526`).
- **Entity re-emit is fully unbounded** — `_reemit_entities_for_missing_sources` and `_check_entity_drift` fetch ALL drifted entities and emit one OutboxEvent each in a single transaction, no cap/cursor/throttle (`:288-331,598-654`) → reconcile-storm risk. (Orphan *delete* is capped; re-emit is not.)
- Edges still not re-emitted on drift (carried from the Gemini-v9 finding).
- **Delta owed:** active Neo4j tombstone heal; per-tick cap/cursor on re-emit; edge-level drift + re-emit.

## AP3 (P0) — Read-path pin / no mixed-epoch — 🔴 Missing (highest-value)
- `GraphRAGComposer.search` calls `wait_for_convergence`, which on timeout **returns `(degraded, staleness)` and does NOT raise/pin** (`reconciler.py:58-91`).
- Both stages then run unconditionally: anchor stage traverses Neo4j at its current version, vector stage queries Qdrant at its current (possibly lagged) version — **no shared watermark passed to either** (`graph_rag.py:400-416`).
- `convergence_degraded`/`staleness` are attached to `SearchResult` **after** the mixed-epoch result is already composed (`graph_rag.py:467-473`). It is a LABEL.
- **Demonstrated skew:** Neo4j@v10 + Qdrant@v7 → result blends v10 entities with v7 chunks (chunks for entities Neo4j has superseded/removed), returned with `degraded_subsystems=["qdrant"]`. Exactly the "silent epoch-skew where a ban was declared."
- `staleness_seconds` is misleadingly named — it's a **version-delta**, not wall-clock (`reconciler.py:136`).
- **Delta owed (product decision):** either (a) compute `min_watermark = min(neo4j, qdrant, pg)` and pin both stages to it (consistent-stale), or (b) hard-degrade (409/503) on divergence beyond threshold. Silent mixing is the one thing the chair says is inadmissible.

## AP4 (P0) — Confidence: real gate or remove — 🔴 Missing
- Live `score_incident(config=None)` → `_v01_ladder` constants `0.9/0.6/0.4/0.1` (`resolution.py:53-57,424-428`); no committed calibration artifact, so the isotonic path is never reached; `support_size` always `30`; `calibrated` flag injected post-`model_dump`, not in the Pydantic schema.
- `calibration-gate.yml` runs the `CalibrationPipeline` on a synthetic 100-row fixture (Brier≤0.20/ECE≤0.10) but is **path-triggered** and tests the pipeline object — a PR editing `resolution.py` (the live path) does NOT trigger it, and it never asserts the number users actually receive.
- **Delta owed (product decision):** either gate Brier/ECE on the LIVE path with a committed fitted artifact + real support counts + `calibrated` in the schema, OR suppress the decimal when `calibrated=false` (qualitative band). v9 frames this as binary — current state closes neither.

## AP5 (P0) — DR-drill volumetric + hash-assert + embeddings-from-SoT — 🟡 Partial
- **Rebuild reuses stored `chunk.embedding`** and only recomputes when empty (`rebuild_all_projections.py:395-399`); the drill seed pre-loads zero-vectors (`seed_dr_drill.py:44-49`) so the embedding-from-text path is never exercised → **restore-from-backup, not rebuild-from-SoT** by the chair's definition.
- Verification checks counts + checkpoint versions only — **no vector/content hash-equivalence** (`dr_verify.py` `verify_projections`).
- Drill dataset = **9 chunks**, RTO budget 120s for the toy set; the real 900s RTO is never validated at representative scale; `dr-drill.yml` is **nightly-only**.
- **Delta owed:** rebuild recomputes embeddings from SoT text (or define `chunk.embedding` as SoT and justify); hash-assert equivalence; representative-volume nightly drill with a fixed RTO budget.

## AP6 (P0 meta-gate) — Evidence bundle — 🔴 Missing
CI inventory (`.github/workflows/`): `ci.yml` (lint/typecheck/test+cov on every PR), `calibration-gate.yml` (path-triggered), `dr-drill.yml` (nightly), `benchmark.yml` (not a correctness gate).
P0 → gate mapping:
- AP1 — soft (in the whole-suite `pytest`, per-PR via `tests/test_ap1_single_writer.py`); no static conformance gate.
- AP2 — soft only; no chaos/property/convergence gate; edge & orphan paths untested.
- AP3 — **no gate**.
- AP4 — gate exists but **doesn't cover the live path** + path-triggered.
- AP5 — gate exists but **nightly-only + 9-chunk toy**.
- **Delta owed:** a dedicated, PR-blocking fail-gate per P0 (conformance for AP1/AP3, chaos/property for AP2, live-path Brier/ECE for AP4, volumetric drill for AP5).

## AP7 (P1) — Arch-tests — 🔴 Missing
- `lint_stale_architecture.py` checks banned strings/ADR/test-id/docstring drift only — **no import topology** rules (`ci.yml:27`).
- No import-linter/AST test forbidding `neo4j.AsyncDriver`/`QdrantClient` imports outside the relay/consumer.
- No structural MCP retrieval-only guarantee; `generate_postmortem` synthesises a templated report + extracts FollowUp entities (`mcp/server.py:1002-1054`) — a synthesis tool on the surface; no test asserts the tool set is retrieval-only.

## AP8 (P1) — Identity reversible merge/split — 🟡 Partial
- Provenance + unmerge present (`neo4j/store.py:1202,1231,1252,1282-1387`, `MERGED_INTO` + `original_node_id`).
- **No conservative auto-merge threshold** (only `RESOURCE_MATCH_THRESHOLD=0.5`), **no review queue / pending_review state** for probabilistic matches (`linker.py:87-119`, `matchers.py`). Auto-merge fires on any cleared threshold.

## AP9 (P1) — as_of conformance — 🟡 Partial
- Per-store as_of contract tests run in CI (gated flags set) (`test_neo4j_store_as_of.py`, `test_qdrant_store_as_of.py`).
- **Cross-store consistency is simulator-only** (`property/test_bitemporal_contract.py:314` against `GraphSimulator`); no single test issues the same as_of through real Neo4j+Qdrant+Postgres asserting identical result sets consistent with a watermark.
- **tx-time (`recorded_at`) filtering absent** from read paths — only valid-time predicate applied.

## AP10 (P2) — DLQ / poison policy — 🟢 Done (caveats)
- Broker DLQ (`max_deliver=5` → `term()` + DLQ subject, `consumer.py:104-151`) + app-layer parking (`MAX_RETRIES_BEFORE_PARK=3`, `outbox_consumer.py:409-436`); parked entity's later messages skipped to DLQ so other entities advance.
- Caveats: `_parked_entities` is in-memory (lost on restart); `_unpark_loop` can cycle a permanently-poisoned entity park↔unpark; per-entity-vs-global order contract under parking is undocumented.

---

## The meta-conclusion (AP6) the chair is driving at
v8 closed the P0 axes **in code** but largely **not behind reproducible CI fail-gates**, and **AP3 was closed declaratively** (label, not pin) — so by the chair's standard the round's status is "claimed", not "closed". The two genuinely false closures are **AP3 (mixed-epoch served silently)** and **AP5 (restore-from-backup masquerading as rebuild-from-SoT)**; **AP4** remains an uncalibrated number; and **AP6** (the gates that would *prove* any of it) is the missing backbone.

## Prioritized remediation (v9 Opus)
1. **AP3 (P0)** — pin both stages to `min_watermark` OR hard-degrade; ban mixed-epoch composition. *(Product decision: consistent-stale vs 409.)*
2. **AP6 (P0)** — add PR-blocking fail-gates: single-writer conformance, reconcile convergence/chaos, read-path no-mixed-epoch, live-path calibration, volumetric DR drill. This is the backbone that makes every other closure provable.
3. **AP5 (P0)** — rebuild from SoT text (recompute embeddings) + hash-assert equivalence + representative-volume drill.
4. **AP4 (P0)** — decide calibrate-for-real-on-live-path vs suppress-the-decimal.
5. **AP1 (P0)** — per-entity conditional apply (read entity version, CAS) atop the source checkpoint.
6. **AP2 (P0)** — bounded re-emit (cap/cursor) + active Neo4j tombstone heal + edge re-emit.
7. **AP7 (P1)** — import-linter arch-test (single-writer) + MCP retrieval-only conformance test.
8. **AP8/AP9 (P1)** — conservative merge threshold + review queue; unified live cross-store as_of conformance + tx-time.
9. **AP10 (P2)** — persist parking; permanent-poison terminal state; document the ordering contract.
