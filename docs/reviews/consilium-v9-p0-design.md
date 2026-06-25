# Consilium v9 — P0 Remediation Design (AP3 pin + AP1 CAS + AP6 evidence gates)

> Architect's approved design. Branch `feat/consilium-v9-p0`. Authoritative for AP1/AP3/AP6.
> Product decisions: **AP3 = consistent-stale PIN** (not 409); **AP4 = suppress the decimal** when
> `calibrated=false` (qualitative band + flag). Scope this round: AP1–AP6 + AP7.

## AP3 — Read-path PIN to min-watermark (consistent-stale)
Approach: **post-compose filter on `applied_version > min_watermark`; GLOBAL min; returned from `check_convergence`.**
- `reconciler.py`: `check_convergence` → return `(bool, degraded, max_lag, min_watermark)` where `min_watermark = min(all pg/neo4j/qdrant versions)` from the SAME snapshot; `wait_for_convergence` → `(degraded, staleness, min_watermark)` on both paths.
- `models.py`: add `SearchResult.pinned_watermark: int | None`.
- `graph_rag.py`: unpack 3-tuple; add pure helper `_apply_watermark_filter(hits, min_watermark)` dropping hits with `applied_version > min_watermark` (keep `applied_version is None`); call it in `_run_merge_stage`; stamp `pinned_watermark` in `_stamp_envelope`. Invariant: **no composed hit with version > min_watermark**.
- Edge cases: cold store (watermark 0 → empty, correct); caller `as_of` composes independently (effective = valid-at-as_of ∩ version≤watermark); no reconciler → `None` (no filter).

## AP1 — Per-entity conditional apply (CAS)
Approach: **CAS in Cypher `WHERE` (`$incoming_version > coalesce(n.version,-1)`); Neo4j-only; source checkpoint stays as coarse fast-path. Qdrant has NO per-entity gap** (chunk points per-document + content-hash idempotency + source checkpoint are correct).
- `neo4j/_cypher.py`: add `_UPSERT_ENTITY_CAS_CYPHER` + `_UPSERT_ENTITY_BITEMPORAL_CAS_CYPHER` — MERGE on `{workspace_id,id}`, `WITH ... CASE WHEN $forced THEN TRUE ELSE $incoming_version > coalesce(n.version,-1) END AS should_write`, `FOREACH(... should_write ... | SET ..., n.version=$incoming_version, ...)`, `RETURN should_write AS applied`. Run them through `_ensure_workspace_predicate`.
- `neo4j/store.py` `upsert_entity._run`: keep source-checkpoint fast-path skip + advance; then run CAS Cypher (bitemporal variant when enabled); `version=None` path stays unconditional (legacy/tests).
- The node `version` written by CAS is what `get_entity_versions` reads (feeds AP3 watermark + AP2 drift). Re-delivery of same/lower version = no-op (idempotent).

## AP6 — Evidence bundle: PR-blocking CI fail-gates (test-first)
Approach: **new `.github/workflows/conformance.yml` with 5 named, independently-failing jobs (one per P0)**; mock-only (no containers) for AP1–AP4; Postgres-only smoke for AP5; integration probes added to `ci.yml` `test` job (containers already up). Commit conformance tests RED/`xfail` first; each P0 closure = red→green of its named check.
- `tests/conformance/{__init__.py, test_ap1_per_entity_cas.py, test_ap2_bounded_reemit.py, test_ap3_no_mixed_epoch.py, test_ap4_confidence_suppression.py, test_ap5_dr_hash_assert.py}` (+ `test_ap7_arch_invariants.py`).
- Jobs: `ap1-per-entity-cas`, `ap2-reconcile-convergence`, `ap3-no-mixed-epoch`, `ap4-confidence-suppression`, `ap5-dr-smoke` (postgres svc), `ap7-arch-invariants`. All `pull_request`→main, blocking (no continue-on-error).
- Mapping table: AP1 out-of-order v3→v2 rejected + monotonicity; AP2 per-tick cap; AP3 no hit>watermark; AP4 no uncalibrated decimal; AP5 3-doc hash-assert recompute-from-text (PR smoke) + nightly full-volume `dr-drill.yml`.

## Specs for AP2 / AP4 / AP5 / AP7 (implementer-facing)
- **AP2**: export `REEMIT_TICK_CAP: int` from `reconcile_worker.py`; cap/cursor `_reemit_entities_for_missing_sources` and `_check_entity_drift` to `min(N, cap)` per tick; ACTIVE Neo4j tombstone heal (end-date orphans, not detect-only; turn `ENABLE_NEO4J_ORPHAN_REEMIT` on by default or replace with real re-projection); EDGE drift detect + re-emit (`get_edge_versions`/`get_edge_content_hashes` on both stores); causal order entities-before-edges in backfill.
- **AP4 (suppress)**: when `calibrated=false`, do NOT surface the decimal — return a qualitative band (low/medium/high) + `calibrated: bool` IN the Pydantic schema (`ResolveIncidentResponse`/relevant model), not injected post-`model_dump`. Keep the calibrated numeric path for when a fitted artifact exists.
- **AP5 (rebuild-from-SoT)**: add `--recompute-embeddings` to `rebuild_all_projections.py` that IGNORES `chunk.embedding` and recomputes from `chunk.text` via the live provider; remove the zero-vector bypass in `seed_dr_drill.py`; add hash-equivalence assert to `dr_verify.py` (vector/content digest, not just counts); PR-smoke (3 docs) + nightly volume.
- **AP7**: `tests/conformance/test_ap7_arch_invariants.py` — `ast`-walk `apps/`+`packages/`, assert `neo4j.AsyncDriver`/`AsyncQdrantClient` imported only under `omniscience_index/stores/`; assert MCP registered tool set is retrieval-only (or `generate_postmortem` explicitly categorized as synthesis with documented governance).

## Sequence
1. AP6 scaffold (conformance.yml + stub tests red/xfail) + AP3 pin → ap3 green.
2. AP1 CAS → ap1 green.
3. AP2 bounded re-emit + tombstone + edges → ap2 green.
4. AP4 suppress + AP5 rebuild-from-SoT + AP7 arch-tests → remaining jobs green.
5. Full suite green + coverage≥80 + mypy 0 + ruff → push → CI green → merge.
