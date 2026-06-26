# Consilium v13 (Opus chair) — grounded diff of v12 — observability + canonical signal

> Verified against actual code. 2 real P0 (a silent blackout introduced in v12 + unobservability),
> 1 P1, 2 P2. The debate loop remains "concluded"; these are real availability bugs fixed as
> ordinary engineering. The observability fix is the genuine exit (epoch drops become a metric).

| # | Claim | Prio | Verdict |
|---|-------|------|---------|
| 1 | metric only on unmapped branch; in-map cold_zero/lag drops unobservable | P0 | ✅ `graph_rag.py` increments `_EPOCH_DROPPED_UNMAPPED` only on not-in-map; in-map drops (wm==0, version>wm) have no counter → blackout invisible (ADR-0017 §3). FIX: reason-labeled counter {unmapped, lag, cold_zero, empty_map} in BOTH the hit filter AND the graph-node filter. |
| 2 | None-vs-{} ambiguity → dead None branch + {} blackout | P0 | ✅ `search():500-501` leaves `per_source_watermark={}` when no reconciler; filter bypass is `is None` only → None branch dead; `{}` (no reconciler OR cold workspace where check_convergence returns `{}`) → strict fail-closed blackout. FIX: canonicalize — pass `None` on the genuine no-reconciler path (so None→pass is live), and emit `epoch_blackout_total` on the `{} + strict` branch so an empty-map blackout is OBSERVABLE. |
| 3 | consumer-path test lacks positive src_a-survives assert (NATS_URL export OK) | P1 | ⚠️ `NATS_URL` IS exported (conformance.yml:327) — SKIP risk refuted. But no `assert any(h.source.id==src_a and h.applied_version==5 ...)` → vacuous positive side. FIX: add the positive assert; ensure the path returns a real src_a hit (not _FakeLegacy→[]). |
| 4 | AP3 undirected verdict rests on comment + tautological test | P2 | ⚠️ Reframe as NOT-PROVEN; replace inline-reimpl directed test with a parametrized test running the REAL traverse() + _recompute_reachability on a shared fixture graph (gated live-Neo4j) proving reachable-set equality. |
| 5 | update_stream branch behaviorally uncovered + fail-loud behavior change | P2 | ✅ `test_queue.py:47` mocks `update_stream` without asserting it's called; 10058→update_stream path untested; update_stream now propagates previously-swallowed errors. FIX: behavioral assert it's called on 10058; make fail-loud-on-incompatible-update an explicit, tested decision. |

Decision (user pattern): fix all (AP1+AP2+AP3+AP5; AP4 = honest NOT-PROVEN + real test), one PR, merge on green CI.
