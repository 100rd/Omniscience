# Consilium v10 (Opus chair) — GROUNDED diff of the v9 changes

> Read-only forensic audit of `main` (consilium-v9 P0 merged, squash `b798b3d`) against the v10
> Action Points, which specifically audit the regressions/holes introduced BY the v9 fixes.
> Verdicts confirmed against actual code; evidence as `path:line`.
>
> **Date**: 2026-06-25 · **Bottom line:** v10 is accurate. The v9 fixes introduced **3 real P0
> correctness holes** (one of which — AP2 backfill heal — is *dead code in practice*), and several
> were invisible because the v9 conformance gates are **mock-only** — recursively proving the chair's
> own thesis ("mock gate green ≠ closed").

## Verdict Matrix

| # | v10 claim | Prio | Verdict | One-line |
|---|-----------|------|---------|----------|
| 1 | AP3 global-min watermark blacks out whole workspace when any source is cold/lagging | P0 | ✅ Confirmed | `min_watermark` is a single GLOBAL min; one cold source (v0) → all hits dropped workspace-wide |
| 2 | AP3 pin is one-directional; graph-ahead mixed-epoch still served | P0 | ✅ Confirmed | Only `vector_result.hits` is filtered; the Neo4j anchor traversal is NOT pinned → v10-graph view + v7-evidence |
| 3 | Per-entity CAS gated behind source-checkpoint skip → AP2 backfill cannot heal node drift | P0 | ✅ Confirmed (**heal is dead**) | `is_backfill` does NOT set `forced_replay`; source-checkpoint skip no-ops the backfill for checkpoint-ahead-of-stale-node |
| 4 | Cross-workspace re-emit cursor starves late-ordered workspaces | P1 | ✅ Confirmed | `_reemit_cursor` is global-per-tick across workspaces → one noisy tenant blocks all others |
| 5 | Unconditional Neo4j orphan end-date can false-split live data | P1 | ✅ Confirmed | `end_date_source_orphans` fires unconditionally, no grace/confirmation window |
| 6 | Conformance gates mock-heavy; live assertions not in the gate | P1 | ✅ Confirmed | ap1–ap4 jobs run mock tests with NO containers; live Cypher/embedding assertions absent from `conformance.yml` |
| 7 | DR hash-equivalence assumes bit-deterministic embeddings; untested at PR + bulk-load missing | P1 | ✅ Confirmed | `compute_vector_digest` = strict SHA-256 of float32 bytes; only the deterministic fake passes; no volume RTO; no bulk-load |
| 8 | Confidence band collapses evidence tiers + nullable is a breaking change | P2 | ✅ Confirmed | `BAND_HIGH≥0.55` merges the 0.9 and 0.6 ladder rungs into "high"; nullable confidence undocumented as a break |
| 9 | Carried v9 P1 gaps (AP8/9/10) tracked, not in scope | P2 | ✅ Noted | identity review-queue, cross-store as_of/tx-time, DLQ persistence still open |

---

## v10-AP1 (P0) — Global watermark blackout — ✅ Confirmed
- `reconciler.py`: `min_watermark` is documented and computed as "the minimum version seen across all three stores" (`reconciler.py:43-45`) — a SINGLE global scalar, no per-source scoping.
- `graph_rag.py:793-816` `_apply_watermark_filter` drops every hit with `applied_version > min_watermark`; applied to `vector_result.hits` at `:627`.
- **Blast radius:** in a multi-source workspace, if ONE source is cold (checkpoint v0) or lagging, `min_watermark` collapses to that low value and hits from ALL other healthy/higher-version sources are dropped → empty/decimated retrieval for the entire workspace. The v9 fix turned a "silent mixed-epoch" bug into a "silent availability cliff" — strictly worse for SRE retrieval. All AP3 tests are single-source, so they miss it.
- **Fix delta:** per-source watermark (drop a hit only if it exceeds the watermark of ITS OWN source), not a single global min.

## v10-AP2 (P0) — One-directional pin (graph-ahead still mixed-epoch) — ✅ Confirmed
- `_apply_watermark_filter` is applied ONLY to `vector_result.hits` (`graph_rag.py:627`). The anchor stage `_run_anchor_stage` (`:412,488`) receives NO `min_watermark` and traverses Neo4j at its current version.
- So with Neo4j@v10, Qdrant@v7, `min_watermark=7`: vector hits filtered to ≤7, but the graph anchor/traversal view is v10 → the composed answer mixes a v10 graph view with v7 evidence. The claimed invariant "no composed hit > min_watermark" holds for the hit LIST but the "no mixed-epoch" guarantee does NOT.
- **Fix delta:** pin the graph traversal too (version-aware ceiling, more than valid-time `as_of`), OR expose a documented hard-degrade product knob (availability vs correctness).

## v10-AP3 (P0) — CAS behind source-checkpoint skip → backfill heal is DEAD — ✅ Confirmed (highest value)
- `neo4j/store.py` `upsert_entity._run` (`:455-524`): step 1 reads the source `StoreCheckpoint`; `should_skip = existing_version >= entity_version` (`:478`); `if should_skip: return` (`:488-489`) — BEFORE the per-entity CAS (`:513-523`). Only `forced_replay` (`:485-486`) or an epoch bump bypasses the skip.
- Drift scenario (the most common): source checkpoint already at v5, but a specific entity node is stale at v3. AP2 re-emits the entity at v5 with `is_backfill=True` (`reconcile_worker.py:440,532`).
- **The consumer does NOT map `is_backfill`→`forced_replay`.** `outbox_consumer.py:261-264`: `if event.is_backfill:` only clears the parked-entity set. The `EntityUpsert` built at `:282-291` sets `version=event.version` but leaves `forced_replay=False` (default) and no `epoch`. So `upsert_entity` sees `existing_version(5) >= incoming(5)` → `should_skip=True` → returns → **the stale node is never healed.**
- Net: AP2's "closed loop" silently no-ops for checkpoint-ahead-of-stale-node — the dominant drift class. The v9 AP2 conformance gate passed because it is mock-based and never exercised this consumer→store path.
- **Fix delta:** map `is_backfill=True` → `forced_replay=True` (or an epoch bump) on the backfill `EntityUpsert` so it bypasses the source-checkpoint skip and reaches the per-entity CAS (which still guards true monotonicity). Bounded by `REEMIT_TICK_CAP`, so write-volume impact is acceptable. Add a LIVE conformance test that drifts a node, re-emits, and asserts the node version healed.

## v10-AP4 (P1) — Cross-workspace cursor starvation — ✅ Confirmed
- `reconcile_worker.py`: `_reemit_cursor` reset once per `run_once()` (`:274`), shared "across workspaces within a single run" (`:188-193`); workspaces iterated in order (`:279-280`).
- With `cap=1000` and workspace A holding 5000 drifted entities, A consumes the entire per-tick budget every tick; workspace B (also drifted, later in iteration order) gets ZERO budget and never reconverges → tenant-isolation violation.
- **Fix delta:** per-workspace cap with a global ceiling (e.g. `min(per_ws_cap, remaining_global)`); rotate/fair-share workspace order.

## v10-AP5 (P1) — Unconditional orphan end-date false-split — ✅ Confirmed
- `reconcile_worker.py:685-720` `_check_neo4j_orphans` calls `end_date_source_orphans` whenever a source looks orphaned; `ENABLE_NEO4J_ORPHAN_REEMIT` defaults `true` (`:136-137`). No grace window, no K-consecutive-detection requirement, no confirmation.
- Scenario: a transient read (in-flight source, tombstone race) makes a LIVE source briefly appear orphaned → its Neo4j entities are end-dated (`valid_to=now`) → data "disappears" + flaps when the source reappears next tick — in exactly the partial-failure scenarios this axis targets.
- **Fix delta:** require K consecutive orphan detections / a grace window before end-dating; or a two-phase mark-then-sweep.

## v10-AP6 (P1) — Mock-heavy conformance gates — ✅ Confirmed
- `conformance.yml`: jobs `ap1`/`ap2`/`ap3`/`ap4` run only `pytest tests/conformance/test_apN_*.py` with NO `services:` block (no containers) (`:39,57,74,92`). Only `ap5` has a postgres service (`:101`).
- The conformance tests are mock-based (mock `tx.run`, mock stores, mock reconciler). The authoritative LIVE assertions (`test_live_*`, gated by `OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS`) run only in the main `ci.yml` `test` job — not in the named conformance gate.
- This is the recursion the chair warns about: the AP2 mock gate is GREEN while the real consumer→store heal is dead (v10-AP3). A mock gate that passes on a semantic Cypher/consumer bug re-creates "claimed not closed".
- **Fix delta:** a shared container job in `conformance.yml` running the live behavioral P0 assertions (real Neo4j/Qdrant/NATS) — the gate must exercise the real path, not a mock.

## v10-AP7 (P1) — Bit-deterministic embedding hash + no volume/bulk — ✅ Confirmed
- `scripts/dr_verify.py:216-226` `compute_vector_digest` = `hashlib.sha256(<float32 packed bytes>)` — a STRICT bit-hash. Real embedding inference (Ollama/transformers) is not bit-deterministic across hardware/threads/runtime → this assert is flaky on real hardware; only the deterministic fake fixture passes.
- The PR-smoke uses a deterministic fake provider, so it never exercises real embedding non-determinism. Volume/RTO still validated only on a 9-chunk fixture. Bulk-load (`UNWIND`/batch upsert) still absent (carried from v9-gemini AP4).
- **Fix delta:** tolerance-based vector equivalence (cosine ≥ 1−ε or L2 ≤ ε) instead of bit-hash; pin the embedding runtime for reproducibility; a nightly volume drill with a real RTO budget; add bulk-load.

## v10-AP8 (P2) — Band collapse + breaking change — ✅ Confirmed
- `resolution.py:75-94`: `BAND_HIGH_THRESHOLD=0.55` covers BOTH the 0.9 (PR temporal match) and 0.6 (PR no-temporal-match) ladder rungs → both map to "high", erasing the strong/weak distinction the heuristic still computes (`CONFIDENCE_*` at `:66-69`). 4 tiers → 3 bands.
- The nullable `resolution_confidence` is a breaking API change; documented in the module docstring but not in a changelog/ADR.
- **Fix delta:** add a 4th band (or map 1:1 to the ladder tiers) so 0.9 vs 0.6 stays distinguishable; document the nullable break in CHANGELOG/an ADR.

## v10-AP9 (P2) — Carried gaps — ✅ Noted
AP8 identity (conservative threshold + review queue), AP9 cross-store as_of conformance + tx-time, AP10 DLQ persistence remain open (tracked in [[omniscience-consilium-loop]]).

---

## Prioritized remediation (v10)
1. **v10-AP3 (P0)** — map `is_backfill`→`forced_replay` in `outbox_consumer.py` so the AP2 heal actually heals; + a LIVE conformance test (drift node → re-emit → assert healed). *(Dead-code fix; highest value.)*
2. **v10-AP1 (P0)** — per-source watermark (no whole-workspace blackout from one cold source).
3. **v10-AP2 (P0)** — pin the graph anchor traversal to the (per-source) watermark too, OR a documented hard-degrade knob; close the mixed-epoch gap for real.
4. **v10-AP6 (P1)** — a live-container conformance job so the P0 gates exercise real Cypher/embedding (this is what would have caught AP3).
5. **v10-AP4 (P1)** — per-workspace re-emit cap + global ceiling.
6. **v10-AP5 (P1)** — grace window / K-consecutive before orphan end-dating.
7. **v10-AP7 (P1)** — tolerance-based vector check + pinned runtime + volume RTO + bulk-load.
8. **v10-AP8 (P2)** — finer bands + document the API break.
