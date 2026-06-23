# Consilium v7 — Architectural Review of `main`

> **Scope**: Read-only audit of the Omniscience codebase (`main`) against the 8 Action Points
> of the Opus vs Gemini Pro consilium v7. Verdicts are based on **actual code**, not commit
> messages. Evidence is cited as `path:line`. No code was modified; nothing was applied or deployed.
>
> **Method**: 4 parallel specialist auditors (backend ×2, security, QA) traced each AP from its
> entry points through imports and tests; targeted `pytest` runs were used to confirm bugs.
>
> **Date**: 2026-06-23 · **Reviewed against**: HEAD `319a620` (`feat(omniscience): implement decision-grade architecture upgrades`)

## TL;DR

The recent commits (`v4 remediation roadmap AP 1-6`, `decision-grade architecture upgrades`) added
**real machinery** for most Action Points — calibration pipelines, an Outbox worker, a global
reconciler, deterministic matchers, merge/unmerge, bitemporal Cypher. But the audit found that
**several "done" claims do not hold under inspection**, including two runtime-breaking defects and
one confirmed no-op shipped behind a passing-looking feature flag:

- **AP7 (bitemporal) is effectively non-functional** for the vector path — `as_of` filtering is a
  literal `pass`. 4 tests already fail. `GRAPH_BITEMPORAL=enabled` by default makes this worse, not better.
- **AP1 (read consistency) crashes at runtime** — the reconciler's Postgres watermark query filters
  on a SQLAlchemy *relationship*, not a column, and raises on the first read.
- **AP2 (identity isolation) ships two cross-domain data-poisoning vectors and an authz hole** — a
  read-only token can permanently reroute the graph.

Net: **0 of 8 fully Done.** 5 Partial, 2 Missing, and AP7 is "implemented-but-broken." The architecture
is sound and the scaffolding is mostly present; the gap is correctness and test coverage, not design.

## Verdict Matrix

| # | Action Point | Priority | Verdict | One-line reason |
|---|--------------|----------|---------|-----------------|
| 1 | Global LSN + Watermarking (Read-Path) | P0 | 🟡 **Partial** | Reconciler wired into read path but **crashes** on invalid ORM filter; `doc_version` is per-doc, not a global LSN; timeout silently serves stale; no tests |
| 2 | Cross-domain Identity Isolation | P0 | 🟡 **Partial** | Merge/unmerge mechanically works, but **no domain guard**, `tags_match` poisoning vector, and merge endpoints need only `Scope.search` |
| 3 | Deterministic Disaster Recovery | P0 | 🟡 **Partial** | Rebuild covers both stores but passes `version=None` (checkpoints never updated → reconciler stalls post-DR); **non-deterministic row order**; no verification step |
| 4 | GraphRAG Fallback Ladder | P1 | 🟡 **Partial** | 2-rung degrade exists; catches only `ValueError` (real Neo4j errors hard-fail); no timeout/circuit-breaker; no keyword rung; no tests |
| 5 | Confidence Calibration | P1 | 🟡 **Partial** | Genuine Platt/isotonic + offline pipeline exist, but **old placeholder constants remain the default path** and pipeline output is never loaded into runtime |
| 6 | Token Budgeting in MCP | P1 | 🟡 **Partial** | Several tools clamped, but `get_document`/entity tools/`generate_postmortem`/`replay_context` unbounded; no pagination; no global budget |
| 7 | Bitemporal Post-filtering | P2 | 🔴 **Missing** | `QdrantFilterBuilder.build()` has `if as_of: pass` — **confirmed no-op, 4 failing tests**; transaction-time dimension entirely absent |
| 8 | Chaos Testing of Pipelines | P2 | 🔴 **Missing** | Outbox + happy-path tests exist; **zero fault injection**; split-brain heal untested; chaos test is broken (`client_name` TypeError) |

Legend: 🟢 Done · 🟡 Partial · 🔴 Missing

---

## AP1 (P0) — Global LSN + Watermarking for Read-Path — 🟡 Partial

**What exists**
- Per-source watermark via `Document.doc_version` (BigInteger, incremented per content update): `packages/core/src/omniscience_core/db/models.py:218`, `packages/index/src/omniscience_index/writer.py:332`.
- `StoreCheckpoint` versioning per `(workspace_id, source_id)` in both stores: `packages/index/src/omniscience_index/stores/neo4j/store.py:301-324`, `packages/index/src/omniscience_index/stores/qdrant_store.py:486-615`.
- `GlobalReconciler.wait_for_convergence()` is genuinely on the read path — `GraphRAGComposer.search()` calls it before querying: `packages/retrieval/src/omniscience_retrieval/reconciler.py:22-119`, `packages/retrieval/src/omniscience_retrieval/graph_rag.py:388-389`, `apps/server/src/omniscience_server/app.py:247-262`.
- Per-hit staleness annotation: `applied_version` + `staleness` on search hits, `SearchResult.min_applied_version`: `qdrant_store.py:1842-1843`, `models.py:183-185`.

**Gaps**
- **[P0 BUG] Reconciler raises at runtime.** `reconciler.py:55` filters `Document.source.tenant_id == workspace_id` — `Document.source` is a *relationship*, not a column. This generates an invalid ORM expression that raises on the first call. Unlike the Qdrant/Neo4j checkpoint reads (each wrapped in `except Exception: return {}` at `reconciler.py:99-100, 117-118`), the Postgres watermark query has **no** try/except — the exception bubbles through `check_convergence` → `wait_for_convergence`. Correct filter is `Source.tenant_id == workspace_id`.
- **`doc_version` is not a global LSN.** Per-document, starts at 1 — two docs from the same source at `doc_version=3` carry no mutual ordering. True read-your-writes needs a monotonic global sequence.
- **Timeout silently serves stale data.** On timeout `wait_for_convergence` logs a warning and returns normally (`reconciler.py:43-45`) — no exception, no `degraded_subsystems` flag, no staleness bound surfaced to the caller.
- **`freshness.py` is timestamp staleness only** (`freshness.py:62-110`) — measures `now - last_sync_at`, knows nothing about projection lag; used by monitoring/`list_sources`, not the read path.
- **No tests** — `grep "GlobalReconciler" tests/` → 0 results.

---

## AP2 (P0) — Strict Cross-domain Identity Isolation — 🟡 Partial

**What exists**
- Deterministic matchers (no LLM/random): `arn_match`, `otel_match`, `tags_match`, `exact_name_match` in `packages/index/src/omniscience_index/matchers.py:100-220`; `resource_name_match` (bounded Sorensen-Dice, 0.5 threshold) `linker.py:36`.
- Full merge/unmerge chain with provenance: protocol `graph.py:315-332`, Neo4j impl `store.py:1114-1328` (`MERGED_INTO` rel + `original_node_id` edge provenance), REST `entities.py:367-419`, tests `tests/test_merge_unmerge_store.py`.
- Workspace scoping correctly enforced on both merge and unmerge: `store.py:1122, 1129-1134, 1229`.

**Gaps**
- **[P0 SECURITY] Merge/unmerge endpoints require only `Scope.search`** (`entities.py:370, 397`) — the same read scope used for graph traversal. Any read-token holder can permanently reroute all edges between entities. An `admin`/`entities:write` scope exists conceptually (`auth/scopes.py:25`) but is not required.
- **[P0 SECURITY] `tags_match` data-poisoning vector** (`matchers.py:199-220`): returns `1.0` when any shared tag/label key agrees (e.g. both carry `{"env":"prod"}`), with no domain/kind guard. A low-trust connector (alerts webhook with attacker-controlled tags) can acquire `cross_ref` edges to every high-trust entity sharing a common tag.
- **[P1] Global prefix stripping enables cross-domain name collisions.** `normalize_entity_name` strips `aws_`/`k8s_`/`gcp_`/`azure_` (`matchers.py:79-97`), and `exact_name_match` is checked **before** the Tf↔K8s kind guard (`linker.py:116-120`). So `aws_nginx` ≡ `k8s_nginx` ≡ a Slack/PagerDuty entity named `nginx`.
- **[P1] `merge_nodes` has no domain/kind compatibility check** (`store.py:1114-1220`) — a PagerDuty incident can be merged into an AWS resource; only the workspace boundary blocks anything.
- **[P2 BUG] Stub resolution flattens edge type.** `_RESOLVE_STUBS_CYPHER` hardcodes `:calls` (`_cypher.py:869`), discarding the original `cross_ref`/`owns`/`deploys` type — semantic corruption that can also defeat edge-type-based filtering.
- **[P2] Test coverage gaps**: no test asserts cross-domain *non*-merge; merge/unmerge tests use a session-level mock and never execute real Cypher (provenance logic in `_unmerge_tx` untested against real Neo4j).

---

## AP3 (P0) — Deterministic Disaster Recovery — 🟡 Partial

**What exists**
- `scripts/rebuild_all_projections.py` rebuilds **both** stores: wipes Neo4j (`MATCH (n) DETACH DELETE n`, line 63), drops prefixed Qdrant collections (lines 77-80), re-ingests active non-tombstoned docs into both via `upsert_chunks`/`upsert_graph`.
- `scripts/reindex_qdrant_hybrid.py` — idempotent BM25 sparse-vector backfill (skips existing, `update_vectors`, `--dry-run`). Solid for its narrow purpose.
- `tests/integration/test_replay.py` — bitemporal replay determinism via fingerprint comparison at 3 anchors (`test_replay.py:305-365`), skips cleanly without Docker.

**Gaps**
- **[P0] `version=None` → checkpoints never updated by rebuild.** `rebuild_all_projections.py:163-171` and `:192-197` call `upsert_chunks`/`upsert_graph` without `version=`. When `version is None`, both stores skip the checkpoint path (`qdrant_store.py:486-487`, `neo4j/store.py:299`). Post-DR, all checkpoints retain pre-wipe/zero state, so the `GlobalReconciler` sees `pg_watermarks > checkpoints` and **spins to timeout on every read** (compounds with AP1).
- **[P0] Non-deterministic document order.** `rebuild_all_projections.py:121-122` has no `ORDER BY` — heap-scan order varies across vacuums, so the same SoT can yield different checkpoint epoch sequences. (Chunk order within a doc is stable: `order_by(Chunk.ord)` line 131.)
- **[P1] Coverage gap**: only `SourceStatus.active` sources rebuilt (line 106); `paused`/`error` skipped; `tenant_id IS NULL` sources `print`-skipped without raising (lines 112-115).
- **[P1] No verification step** — script ends on `print("Rebuild complete.")` with no Postgres-vs-projection count reconciliation.
- **[P2 BUG] `tests/test_epoch_forced_replay.py` is broken** — instantiates `Neo4jGraphStore(config=..., client_name="test")` but the constructor is `__init__(self, *, config)` (`neo4j/store.py:170`). Fails at collection with `TypeError`. The DR/replay regression guard does not run.
- `reindex_qdrant_hybrid.py` is **not** a DR path (Qdrant-only sparse backfill, no Postgres rebuild, no Neo4j).

---

## AP4 (P1) — GraphRAG Fallback Ladder — 🟡 Partial

**What exists**
- Graph-unavailable → unscoped-vector degradation: `_run_anchor_stage` catches `ValueError` from `traverse` and proceeds with unscoped vector (`graph_rag.py:524-537`).
- Anchor-absent path skips graph entirely (`graph_rag.py:466-480`); legacy branch for non-Neo4j+Qdrant stacks (`graph_rag.py:380-383`).
- **Truncation is now weight-based**, not blind: `_collect_candidates` sorts by degree centrality desc, depth asc before slicing `MAX_ANCHOR_CANDIDATES=32` (`graph_rag.py:726-787`). *(This directly addresses the earlier hard-truncation finding.)*

**Gaps**
- **Only `ValueError` is caught** (`graph_rag.py:524`) — `ServiceUnavailable`, connection-refused, driver errors propagate and hard-fail the request.
- **No timeout / circuit breaker** — a slow (non-erroring) Neo4j hangs the request; no `asyncio.wait_for`.
- **Two rungs only** (graph→unscoped-vector); no keyword-only rung as an independent fallback when Neo4j is absent.
- **No tests** for the degraded path under real driver error types.

---

## AP5 (P1) — Confidence Metric Calibration — 🟡 Partial

**What exists**
- Genuine probabilistic scoring: `p_source * p_time * p_topo` + Platt (`calibrate_platt`) and isotonic (`calibrate_isotonic`) calibrators — `packages/retrieval/src/omniscience_retrieval/probabilistic_scoring.py:78-99`. Not renamed constants.
- Real offline `CalibrationPipeline` (PAVA isotonic, weighted Brier, ECE, bootstrap CIs, out-of-fold): `incidents/calibration.py`; tested end-to-end in `tests/test_calibration_pipeline.py`.
- Per-tenant weight tuning with sum-to-1 validator + DB persistence: `incidents/scoring.py`.

**Gaps**
- **Old placeholder constants remain — and are the default.** `incidents/resolution.py:53-56` still defines `CONFIDENCE_PR_TEMPORAL_MATCH=0.9`, etc. (comment: "v0.1 placeholder; #155 replaces it"). `score_incident` falls back to the v0.1 ladder whenever `config is None or config.weights is None` (`resolution.py:372-374`) — the default for any workspace without an explicit override.
- **No feedback loop**: offline `CalibrationPipeline` output is never loaded into the live `calibrate_isotonic`, which uses hardcoded thresholds `[0,0.2,0.4,0.6,0.8,1.0]` / values `[0,0.15,0.35,0.6,0.85,1.0]` always (`probabilistic_scoring.py:96-99`).
- `calculate_probabilistic_incident_confidence` still uses fixed `base_p` constants `0.95/0.65/0.45/0.15` (`probabilistic_scoring.py:196-269`) — restructured, not calibrated.
- No test asserts a Brier-score bound on the **live** scoring path (only the offline pipeline).

---

## AP6 (P1) — Token Budgeting in MCP — 🟡 Partial

**What exists**
- `incident_timeline`: hard cap `MAX_EVENTS_RETURNED=500` with explicit `truncated: bool` signal (`apps/server/src/omniscience_server/incident_timeline.py:92, 258-269`).
- `search.top_k` validated `ge=1, le=500` (`packages/retrieval/src/omniscience_retrieval/models.py:29`); `max_depth` clamped to `[1,5]` for `resolve_incident`/`blast_radius` (`mcp/server.py:616, 740`); `suggest_runbook`/`find_similar_incidents` limits clamped (`runbook.py:101`, `similar_incidents.py:85-86`).

**Gaps**
- **`get_document` unbounded** (`mcp/tools.py:139-211`): fetches all chunks `order_by(Chunk.ord)`, no `LIMIT`, no size cap, no `truncated` flag.
- **Entity tools unbounded**: `get_related_entities`/`get_entity`/`list_entities` (`mcp/tools.py:413-570`) pass `chunk_text` verbatim per entity — 100 entities × 2 KB = 200 KB in one response.
- **`generate_postmortem`** (`mcp/server.py:986-1033`) and **`replay_context`** (`mcp/server.py:766-844`) echo full rendered output with no size gate.
- **No pagination/continuation token** anywhere — `incident_timeline` truncation is a silent drop with no cursor for events 501+.
- **No global per-call budget** at the MCP layer — each tool bounds its own input cardinality, nothing bounds total bytes to the LLM.
- Truncation test is thin: `tests/integration/test_incident_timeline.py:427-435` only asserts `truncated is False`; no test forces the flag to `True`.

---

## AP7 (P2) — Bitemporal Post-filtering — 🔴 Missing (confirmed by failing tests)

**What exists (and is correct)**
- `GRAPH_BITEMPORAL` defaults to `"enabled"` (`config.py:258-271`, per issue #317).
- **Neo4j valid-time filtering is correct**: `_TRAVERSE_AS_OF_CYPHER_TEMPLATE` applies `valid_from <= datetime($as_of) AND (datetime($as_of) < valid_to OR valid_to IS NULL)` on seed, relationships, and neighbours (`_cypher.py:772-824`).
- Composer threads `as_of` to both stages (`graph_rag.py:391-401`); `QdrantFilterBuilder` *has* a correct `_as_of_must_clauses` helper (`qdrant_filters.py:201-236`).

**The critical defect**
- **`QdrantFilterBuilder.build()` never applies the `as_of` clause.** `qdrant_filters.py:179-182`:
  ```python
  if self.current_only_flag:
      pass
  if self.as_of is not None:
      pass
  ```
  Both branches are `pass`. Every `as_of` vector query silently becomes an all-time scan. `build_as_of_filter` delegates to `build()`, so it is equally broken.
- **Confirmed**: `pytest tests/test_qdrant_filter_builder.py` → **4 failures** (`test_with_as_of_adds_valid_from_lte_clause`, `..._valid_to_gt_or_null_subfilter`, `..._preserves_other_narrowers`, `test_build_as_of_filter_is_workspace_scoped`).
- Because `GRAPH_BITEMPORAL=enabled` by default, every historical/as-of query now returns wrong (current-blind) vector results silently.

**Additional gap**
- **Transaction-time dimension entirely absent** — no `recorded_at <= as_of_tx` predicate in either Neo4j or Qdrant paths. Only valid-time is modelled, so this is "uni-temporal as-of," not bitemporal.

---

## AP8 (P2) — Chaos Testing of Distributed Pipelines — 🔴 Missing

**What exists**
- Outbox pattern implemented: `OutboxEvent` model, `OutboxWorker` (Postgres→NATS), `OutboxConsumerWorker` (NATS→Neo4j+Qdrant) — `apps/server/src/omniscience_server/outbox_worker.py`, `outbox_consumer.py`.
- `tests/test_outbox_flow.py` — 4 unit tests: same-tx OutboxEvent write, happy-path publish, DLQ+park after 3 retries, ack-on-success.
- Drift detection emits OutboxEvents (`tests/test_reconcile_worker.py:511`); NATS stream/consumer mock-level tests (`test_nats_streams.py`, `test_queue.py`); scheduler handles `nats_conn=None` (`test_scheduler.py:364`).

**Gaps**
- **No fault injection of any kind** — no toxiproxy, no socket faults, no Docker health teardown, no `tests/chaos/`.
- **Split-brain (issue #314) heal untested**: `OutboxConsumerWorker._consume_entities()` writes Neo4j then Qdrant with no outer transaction; if Neo4j succeeds and Qdrant raises, the msg is nak'd but Neo4j now holds an entity Qdrant lacks. No test verifies the reconcile worker detects/heals this.
- No test for NATS-unavailable mid-`_tick()`, Neo4j-down during consume, or worker SIGKILL between processing and `processed=True` commit.
- **The one chaos-adjacent test is broken** — `test_epoch_forced_replay.py` fails with the `client_name` `TypeError` (see AP3), so it guards nothing.

---

## Prioritized Remaining Work

Ordered by (priority × correctness impact). Items marked 🐞 are shipped bugs, not missing features.

### Must-fix before claiming P0 done
1. 🐞 **AP1** — Fix reconciler watermark filter `Document.source.tenant_id` → `Source.tenant_id` (join `Source`); wrap in try/except like the projection reads. *(One-line fix; currently crashes every GraphRAG read if reached.)* `reconciler.py:55`
2. 🐞 **AP7** — Replace the two `pass` branches in `QdrantFilterBuilder.build()` with calls to `_as_of_must_clauses` / current-only clause; make the 4 failing tests pass. `qdrant_filters.py:179-182`
3. 🐞 **AP3** — Pass a real monotonic `version` to `upsert_chunks`/`upsert_graph` in the rebuild so checkpoints advance (otherwise DR + reconciler deadlock-by-timeout). `rebuild_all_projections.py:163-197`
4. 🐞 **AP2 (security)** — Require `admin`/`entities:write` (not `Scope.search`) on `POST /entities/merge|unmerge`. `entities.py:370, 397`
5. 🐞 **AP2 (security)** — Add a domain/kind guard to `tags_match` (and ideally a domain-pair allowlist to the linker) to close the tag-poisoning vector. `matchers.py:199-220`, `linker.py:116-132`

### Should-fix to make P0/P1 robust
6. **AP1** — Promote `doc_version` to (or add) a global monotonic sequence for true read-your-writes; on reconciler timeout, surface staleness (`degraded_subsystems`) instead of silently serving stale.
7. **AP3** — Add `ORDER BY` for deterministic rebuild order; add a post-rebuild Postgres↔projection count verification; cover non-active sources or document the exclusion.
8. **AP5** — Wire the offline `CalibrationPipeline` output into runtime `calibrate_isotonic`; remove/retire the v0.1 placeholder ladder as the default fallback. `resolution.py:53-56, 372-374`, `probabilistic_scoring.py:96-99`
9. **AP4** — Broaden the catch beyond `ValueError` (handle `ServiceUnavailable`/driver errors); add an `asyncio.wait_for` timeout + circuit breaker; add a keyword-only rung. `graph_rag.py:524`
10. **AP2** — Scope prefix-stripping to the Tf↔K8s pair only (run after the kind guard), not globally. `linker.py:116-120`

### P2 / hardening
11. 🐞 **AP3/AP8** — Fix `test_epoch_forced_replay.py` constructor call (`client_name` removed) so the replay/chaos guard runs. `test_epoch_forced_replay.py:17`
12. 🐞 **AP2** — Preserve original edge type in `_RESOLVE_STUBS_CYPHER` instead of hardcoding `:calls`. `_cypher.py:869`
13. **AP6** — Add a char/token budget + `truncated` flag + pagination cursor to `get_document` and the entity tools; consider a global per-call MCP budget. `mcp/tools.py:139-211, 413-570`
14. **AP7** — Add the transaction-time predicate (`recorded_at <= as_of_tx`) to reach true bitemporality.
15. **AP8** — Introduce real fault-injection tests (NATS down mid-tick, Neo4j down during consume, worker crash before `processed=True`); add an explicit split-brain heal test for the reconcile worker.

---

## Appendix — Confirmed runtime evidence

- `pytest tests/test_qdrant_filter_builder.py` → 4 failures (AP7 no-op confirmed).
- `pytest tests/test_epoch_forced_replay.py` → collection `TypeError: Neo4jGraphStore.__init__() got an unexpected keyword argument 'client_name'` (AP3/AP8).
- `grep -rn "GlobalReconciler" tests/` → 0 matches (AP1 untested).
