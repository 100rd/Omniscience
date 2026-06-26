# Consilium v12 (Gemini Pro chair) — post-loop cleanup

> Verdict: Partial Success — no P0/criticals (loop has converged). Audited against actual code.
> The architecture-debate loop was CONCLUDED at v11; v12 is handled as ordinary engineering cleanup,
> not a reopened round.

| # | Claim | Prio | Verdict |
|---|-------|------|---------|
| 1 | empty-map bypass of fail-closed | P1 | ✅ Real — `_apply_watermark_filter` `if not per_source_watermark: return hits` lets a cold-start empty `{}` pass all versioned hits despite `strict_epoch=True`. FIX. |
| 2 | UUID-vs-str key blackout | P1 | ❌ REFUTED — reconciler stringifies all keys (`reconciler.py:261,278,298`); `graph_rag` lookup uses `str(h.source.id)`; conformance-live (real reconciler, multi-source) passed with "healthy survives" — impossible under UUID-key blackout. No change. |
| 3 | undirected BFS in `_recompute_reachability` | P2 | ⚠️ Real-minor — adjacency built both directions; may keep nodes reachable only against edge direction. FIX (directed BFS). |
| 4 | live AP3 test bypasses NATS consumer | P2 | ⚠️ Test hygiene — `test_ap3_real_reconciler_live` uses direct upsert; consumer→NATS→store IS already covered by `test_ap2_backfill_heal`. FIX (exercise consumer path in the AP3 live test too). |

Decision (user): fix AP1 + AP3 + AP4 in one PR; AP2 closed as refuted.
