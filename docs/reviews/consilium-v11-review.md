# Consilium v11 (Opus chair) — GROUNDED diff of the v10 changes — FINAL debate round

> Read-only forensic audit of `main` (consilium-v10 P0 merged, squash `7a0f1a7`) against the v11
> Action Points, which audit the v10 fixes. All 6 confirmed against actual code. **This is the last
> architecture-debate round**: v11 closes the 2 P0s AND the recurring root causes (mock gates not
> exercising the real component; control-flow-by-exception; unproven concurrency), then the project
> moves to real-usage-driven iteration.
>
> **Date**: 2026-06-25.

## Verdict Matrix

| # | v11 claim | Prio | Verdict | One-line |
|---|-----------|------|---------|----------|
| 1 | mixed-epoch leak: cold (pg==0) not in map + not-in-map is fail-OPEN + a test cements it | P0 | ✅ Confirmed | `graph_rag.py` not-in-map → pass-through; `test_ap3_no_mixed_epoch.py:458-471` asserts `applied_version==100` PASSES for an unmapped source |
| 2 | graph-ahead seed: `raise ValueError` caught by a too-WIDE `except` that also masks real `traverse()` errors | P0 | ✅ Confirmed | `graph_rag.py:546-611` `try: traverse(); _apply_graph_watermark_filter() except ValueError: anchor_hit=False` — control-flow-by-exception + masks genuine traverse errors on a lagging source |
| 3 | conformance-live MOCKS GlobalReconciler → the per-source watermark core (the v11-AP1 bug class) is not live-exercised | P1 | ✅ Confirmed | live AP3 test uses a mock reconciler; the real `check_convergence` multi-source/cold path is never run in the gate → self-deceiving (recursion of v10-AP6) |
| 4 | per-entity CAS atomicity unproven under backfill↔live concurrency; live tests single-threaded | P1 | ✅ Partial | CAS is a single-statement MERGE (atomic per-node via MERGE lock), but no concurrency/property test proves no lost-update; "heal closed" doesn't cover races |
| 5 | graph-pin doesn't recompute reachability after cutting ahead-nodes → phantom paths in scoring | P1 | ✅ Confirmed | `_apply_graph_watermark_filter` removes ahead nodes/edges but leaves nodes reachable only through them with stale depth in `related` → residual graph mixed-epoch |
| 6 | undocumented guarantee change (per-source vs cross-source epochs) + `version is None` silently bypasses both pins + no leak metric | P2 | ✅ Confirmed | no ADR/CHANGELOG for the per-source semantics; `applied_version is None` and node `version is None` always pass; no None/leak-rate observability |

Legend: ✅ Confirmed / Partial.

---

## Fix design (final round)

### v11-AP1 (P0) — close the mixed-epoch leak
- **(a) map population:** in `reconciler.py check_convergence`, ensure EVERY source known to ANY store (pg, neo4j, qdrant) appears in `per_source_watermark` — a cold source (pg==0 / no pg docs but present in a projection) gets an explicit `0`, not omission. Iterate the union of source ids, not just `pg_watermarks`.
- **(b) not-in-map policy = FAIL-CLOSED:** in `graph_rag.py _apply_watermark_filter`, a hit whose source has no map entry must be DROPPED (or only pass when `applied_version is None`), not passed through. Gate behind a `strict_epoch` knob (default fail-closed) + emit a `omniscience_graphrag_epoch_leak_total` / `*_dropped_unmapped_total` metric so the leak-rate is observable. Document the cold-start availability trade-off.
- **(c)** invert the test at `test_ap3_no_mixed_epoch.py:458-471`: an unmapped-source hit at v=100 must now be DROPPED (fail-closed) — assert the safe behavior, not the bug.

### v11-AP2 (P0) — explicit anchor-miss, no control-flow-by-exception
- `_apply_graph_watermark_filter` must NOT `raise ValueError` for a graph-ahead seed. Return an explicit signal (e.g. `(filtered_view, seed_excluded: bool)` or a sentinel), and have `_run_anchor_stage` map `seed_excluded` → `anchor_hit=False` deterministically.
- Narrow the `except` around `traverse()` so genuine traverse errors are NOT masked as anchor-miss (let them propagate or handle explicitly with logging/metric). Add a test that a real `traverse()` error is not silently swallowed as a miss.

### v11-AP5 (P1) — recompute reachability after pruning ahead-nodes
- After excluding ahead nodes/edges, re-run BFS from the seed over the surviving edges and drop nodes no longer reachable (or recompute their depth). Prefer this BFS cleanup (cheap); a Cypher `version`-ceiling in `traverse` is the more correct root fix if feasible. Add a test: a node reachable only via an excluded ahead-node is removed (no phantom path / no stale depth in scoring).

### v11-AP3 (P1, root-cause) — real reconciler in the live gate
- The `conformance-live` job must run the REAL `GlobalReconciler.check_convergence` against live Postgres+Neo4j+Qdrant in a MULTI-SOURCE scenario incl. a cold pg==0 source — asserting per-source watermark is computed correctly and the leak (AP1) is closed end-to-end. Add a Postgres service to the job. Make `conformance-live` a REQUIRED check.

### v11-AP4 (P1, root-cause) — prove CAS atomicity under concurrency
- Document that the per-entity CAS is a single-statement `MERGE ... WITH CASE ... FOREACH(SET)` (MERGE acquires a per-node lock → atomic CAS). Add a property/chaos test that runs concurrent backfill and live upserts for the SAME entity (interleaved/parallel) and asserts the final node version is the max applied and no write is lost.

### v11-AP6 (P2) — document the guarantee + observability
- ADR + CHANGELOG: the composed answer now legitimately mixes epochs ACROSS sources (per-source pin, not a single cross-source epoch) — note the implication for causal-temporal queries and the `strict_epoch` knob. Document the `version is None` pass-through policy (vector hits and graph nodes) and surface the None-rate + leak-rate metrics.

## Sequence
- Batch A: v11-AP1 + AP2 + AP5 + AP6 (retrieval-layer code/policy/docs/metric).
- Batch B: v11-AP3 + AP4 (real-reconciler live gate + concurrency property test).
- Then full CI green + `conformance-live` required → merge → declare stable baseline.
