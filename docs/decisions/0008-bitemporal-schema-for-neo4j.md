# ADR 0008 — Bitemporal schema for Neo4j entities and edges

- **Status**: Proposed
- **Date**: 2026-04-24
- **Amends**: [ADR-0005](0005-neo4j-as-graph-store.md) §Schema posture — the placeholder bullet that defers `valid_from`, `valid_to`, `recorded_at` enforcement "to a follow-up ADR tied to epic [#97](https://github.com/100rd/Omniscience/issues/97)". This is that ADR.
- **Numbering note**: issue [#128](https://github.com/100rd/Omniscience/issues/128) anticipated file `0007-bitemporal-schema-for-neo4j.md`. ADR-0007 was assigned to the K8s operator architecture (issue [#101](https://github.com/100rd/Omniscience/issues/101), PR [#142](https://github.com/100rd/Omniscience/pull/142)) which merged after #128 was filed. This ADR therefore lands at 0008. The retention sibling (issue [#129](https://github.com/100rd/Omniscience/issues/129), PR #144) lands at 0009.

## Implementation notes

Filled by the Wave 6 closer ([#139](https://github.com/100rd/Omniscience/issues/139)) once schema migration ([#130](https://github.com/100rd/Omniscience/issues/130)), writer changes ([#131](https://github.com/100rd/Omniscience/issues/131)), the read-API `as_of` parameter ([#132](https://github.com/100rd/Omniscience/issues/132)–[#134](https://github.com/100rd/Omniscience/issues/134)), tombstones ([#137](https://github.com/100rd/Omniscience/issues/137)), and the property-test invariants ([#138](https://github.com/100rd/Omniscience/issues/138)) all land. The status flips to `Implemented` only when [#138](https://github.com/100rd/Omniscience/issues/138) closes — mirrors how ADR-0005 and ADR-0006 flipped after [#105](https://github.com/100rd/Omniscience/issues/105).

## Context

Two clocks govern incident reasoning. **Valid time** is when a fact was true in the world: pod `ratings-7d6c…` was running between `2026-04-12T08:00Z` and `2026-04-12T19:30Z` and would still have been running at `19:25Z` whether or not Omniscience had ingested that fact yet. **Recorded time** is when Omniscience learned the fact: the same pod's existence may have been ingested in three batches over a week as different connectors (kubectl-watch, AWS EKS, Datadog) caught up. Single-clock systems collapse the two and answer one or the other but never both.

Vision §5.3 ([`docs/vision.md`](../vision.md) line 71-77) requires both. Every entity and every edge carries `valid_from`, `valid_to` (real-world validity window) plus `recorded_at` (ingestion time). [ADR-0005](0005-neo4j-as-graph-store.md) §Schema posture committed the *names* of those properties as placeholders on every node and every edge but explicitly punted enforcement — constraints, indexes, query rewriting, retention worker — to "a follow-up ADR tied to epic [#97](https://github.com/100rd/Omniscience/issues/97)". The Neo4j adapter at [`packages/index/src/omniscience_index/stores/neo4j_store.py`](../../packages/index/src/omniscience_index/stores/neo4j_store.py) ships today with `MERGE` paths that carry `created_at` and `updated_at` but do **not** carry the bitemporal triple, and its `_BOOTSTRAP_STATEMENTS` block indexes `(workspace_id, kind)` and `(workspace_id, name)` but no temporal columns. ADR-0008 fixes the design that turns those placeholders into a real enforcement path.

[ADR-0006](0006-qdrant-as-vector-store.md) §Schema posture already mirrors the placeholder properties on the Qdrant payload — `valid_from`, `valid_to`, `recorded_at` with `recorded_at` payload-indexed — and cites Vision §5.3 as the bitemporal-alignment target. ADR-0008 is the canonical contract that ADR-0006 was deferring to. Cross-store alignment is therefore a hard requirement of this ADR: units, interval convention, and the "still valid" sentinel must be identical on both stores so a vector search at `as_of=T` returns chunks consistent with the graph at T.

The architectural pressure is real but bounded. Vision §11 (line 177) flags that *"bitemporal semantics are powerful but expensive to query correctly. Initial implementation may restrict time-travel queries to a subset of entity types."* This ADR does not take that restriction — every entity kind is bitemporal — but it shapes the trade-offs around it: read-path performance for the dominant "current state" query must not regress, and the `as_of` predicate must be a single, mechanical Cypher rewrite that an engineer can apply by inspection rather than a per-kind hand-tuned query.

ADR-0008 is the operational and indexing contract that the rest of epic [#97](https://github.com/100rd/Omniscience/issues/97) implements. The retention tiering policy is a **parallel, sibling ADR** at issue [#129](https://github.com/100rd/Omniscience/issues/129) (file `0009-retention-tiering-policy.md`) and is explicitly out of this ADR's scope.

## Decision

**The Neo4j graph carries bitemporal semantics on every entity and every edge, encoded as a property-versioned identity-node with a side `[:HAD_STATE]` relationship to per-version `(:EntityState)` nodes, and as direct bitemporal properties on every relationship.** The interval convention is open-closed `[valid_from, valid_to)`, the "still valid" sentinel is `valid_to IS NULL`, and the unit is Cypher `datetime` (UTC, microsecond resolution).

The full decision is an answer to each numbered prompt in issue [#128](https://github.com/100rd/Omniscience/issues/128) §1–§9.

### 1. Property semantics

- **Type and unit**: Cypher `datetime` (the temporal type, not a string and not epoch ms). The `neo4j` Python driver round-trips `datetime` values to and from Python `datetime.datetime` objects with timezone preserved. UTC is the on-write canonical form; the writer rejects naive (timezone-less) datetimes. Microsecond resolution is the storage unit; the API surface may quantize to milliseconds without changing the schema.
- **`valid_to = NULL` for "still valid"**, not `+infinity`. Cypher's `datetime` type has no representable `+inf`; choosing a sentinel like `9999-12-31T23:59:59Z` introduces a class of "do we mean truly open-ended or the year 9999" footnotes that age poorly, and the index seeks degenerate when half the rows share the same maximum value. `IS NULL` is index-friendly under Neo4j 5.x (null-aware index seeks are first-class) and aligns with ADR-0006's Qdrant payload, where the `valid_to` field naturally maps to JSON `null`. The trade-off — every read predicate carries one extra `OR valid_to IS NULL` branch — is small and uniform; see §5.
- **`recorded_at` is monotonic non-decreasing per `(workspace_id, id)`.** Yes. Out-of-order ingestion (a connector catches up to old data) is a real failure mode but admitting it into the schema breaks the "what did Omniscience know at T" semantic, because a later read at T would see a fact ingested *after* T and thus impossible to have known. The writer enforces monotonicity at upsert time by clamping: `recorded_at := max(now_utc(), max_recorded_at_for_(workspace_id, id))`. This is the same idiom Postgres operational tables already use for `updated_at`. The trade-off is that very-late-arriving connector data appears at "now" rather than at its true ingestion time — acceptable, because the world-clock fields (`valid_from`, `valid_to`) carry the real-world reality and `recorded_at` is, by definition, an operator-clock concept where "when we learned it" includes "when we processed it".
- **`valid_from > valid_to` is corruption.** The writer rejects any upsert with `valid_from >= valid_to`; the equality case is also rejected (a zero-length interval has no semantics). Neo4j 5.x property existence constraints cannot express cross-property comparisons, so this is enforced in the adapter (the existing `_ensure_workspace_predicate` import-time guard pattern in `neo4j_store.py` is the model) plus a property test under [#138](https://github.com/100rd/Omniscience/issues/138). The corruption guard is symmetric on edges.

### 2. Identity vs. version shape

**Property-versioned identity-node with a side `[:HAD_STATE]` relationship and per-version `(:EntityState)` nodes.** Specifically:

- `(:Entity {workspace_id, id, …current state})` — one node per identity, carries the **current** state properties (mirror of the latest `EntityState` where `valid_to IS NULL`). The `(workspace_id, id)` UNIQUE constraint from ADR-0005 §Schema posture is unchanged; `id` is the source-level identity and is stable across versions per §9.
- `(:EntityState {workspace_id, id, valid_from, valid_to, recorded_at, …versioned properties})` — one node per `(identity, valid_from)` tuple. The full versioned snapshot lives here. `valid_to IS NULL` marks the still-current version.
- `(:Entity)-[:HAD_STATE]->(:EntityState)` — fan-out from identity to history. Every `:EntityState` is reachable from exactly one `:Entity` via `[:HAD_STATE]`.

**Current-state read** (the dominant query, no `as_of`) is one MATCH on `(:Entity {workspace_id, id})` against the existing composite index — same plan, same latency, same cost as the pre-bitemporal path. The mirror of current state onto the identity node is what makes this guarantee hold; without it, every read would have to traverse `[:HAD_STATE]` to find the `valid_to IS NULL` row. The duplication is intentional and bounded — current state is a single row.

**Point-in-time read** (`as_of=T`) is one MATCH on `(:Entity {workspace_id, id})-[:HAD_STATE]->(s:EntityState)` with the interval predicate on `s`. Index-backed by `entity_state_workspace_valid_window` (§4). One extra hop relative to current-state. This is the cost of bitemporal correctness and it is uniform across entity kinds.

**Edges remain identity-to-identity.** `(:Entity)-[:DEPLOYED_BY]->(:Entity)` does not bifurcate into per-version-of-each-endpoint relationships. Edges carry their own bitemporal triple as direct relationship properties; the edge has a validity window of its own (§3) but its endpoints are identity nodes. This is the architecturally important constraint: bitemporal-ness is contained to the *property* dimension of nodes (via `:HAD_STATE`) and to the *property* dimension of edges, never to the *topology* of the graph itself.

The writer contract (out of scope for this ADR; landed in [#131](https://github.com/100rd/Omniscience/issues/131)) is: an upsert that observes a state change creates a new `:EntityState` node, points a new `[:HAD_STATE]` relationship at it, closes the previous state's `valid_to` to the new state's `valid_from`, and updates the identity node's mirrored properties. An upsert that does not change state is a no-op on the version chain.

### 3. Edge bitemporal shape

Every relationship carries `valid_from`, `valid_to`, `recorded_at` directly as relationship properties. The shape is the same as the relationship-property design ADR-0005 §Schema posture already named idiomatic for Neo4j (`[:DEPLOYED_BY {valid_from, valid_to, recorded_at}]`).

**Tombstone semantics**: when an edge ends, `valid_to` is set to the end timestamp. The relationship is **not deleted**. The writer no longer issues `DELETE` against relationships once the bitemporal flag is on (§8). End-dating preserves the `as_of < end` queryability that hard deletion destroys.

This changes the writer contract from PR [#104](https://github.com/100rd/Omniscience/issues/104). Before bitemporal: upserts that no longer observe an edge produce a `DELETE`. After bitemporal: those same upserts produce a `SET r.valid_to = $now` on the still-current relationship. The previous `_DELETE_BY_SOURCE_CYPHER` and `_DELETE_TOMBSTONED_CYPHER` templates in `neo4j_store.py` are replaced by end-dating equivalents in [#137](https://github.com/100rd/Omniscience/issues/137); retention (the actual reaping of tombstoned edges) is the job of the retention worker in [#135](https://github.com/100rd/Omniscience/issues/135) and is governed by ADR-0009. Hard deletion of an edge happens only as a retention action on edges whose `valid_to` is older than the archive boundary, never as a writer action.

When a relationship's *target* identity is itself tombstoned (an end-dated entity, [#137](https://github.com/100rd/Omniscience/issues/137)), the edge's `valid_to` is closed to the same timestamp as the target's last `valid_to`. This is symmetric on the source side. The invariant is: `edge.valid_from >= max(source.valid_from, target.valid_from)` and `edge.valid_to <= min(source.valid_to, target.valid_to)` where `NULL` is treated as `+infinity` for the `min`. The corruption guard in §1 is extended to enforce this on edge writes.

A relationship that needs to "resurrect" after end-dating — the same `(source, target, edge_type)` re-observed after a gap — is a new relationship with a new `valid_from`. The graph then carries multiple parallel relationships of the same type between the same endpoints, distinguished by their disjoint validity windows. The composite uniqueness for relationships (§4) accommodates this.

### 4. Constraints and indexes

The list below is what lands in `_BOOTSTRAP_STATEMENTS` of the Neo4j adapter via [#130](https://github.com/100rd/Omniscience/issues/130). Every statement is composite on `workspace_id` (the carry-forward in §Consequences-security is non-negotiable). Statements that already exist in the bootstrap from ADR-0005 are listed here for completeness — they are unchanged.

```cypher
-- ADR-0005 carry-forward — unchanged.
CREATE CONSTRAINT entity_workspace_id_unique IF NOT EXISTS
FOR (n:Entity) REQUIRE (n.workspace_id, n.id) IS UNIQUE;

CREATE INDEX entity_workspace_kind IF NOT EXISTS
FOR (n:Entity) ON (n.workspace_id, n.kind);

CREATE INDEX entity_workspace_name IF NOT EXISTS
FOR (n:Entity) ON (n.workspace_id, n.name);

-- New in ADR-0008.
-- EntityState versions: one row per (workspace_id, id, valid_from). Same valid_from
-- twice for the same identity is corruption (a writer race); recorded_at is NOT in
-- the uniqueness key because monotonicity (§1) makes it redundant for distinguishing
-- rows.
CREATE CONSTRAINT entity_state_workspace_id_valid_from_unique IF NOT EXISTS
FOR (s:EntityState) REQUIRE (s.workspace_id, s.id, s.valid_from) IS UNIQUE;

-- Hot-path lookup of current state by identity, narrowed by ingestion freshness.
-- Used by the dominant "current state" read path and by the retention worker
-- to find candidates for hot -> warm eviction (ADR-0009 §2).
CREATE INDEX entity_workspace_recorded_at IF NOT EXISTS
FOR (n:Entity) ON (n.workspace_id, n.recorded_at);

-- Validity-window seek for as_of reads. The composite ordering matters: the
-- planner narrows by (workspace_id, id) first, then ranges over (valid_from,
-- valid_to), which is the shape the §5 predicate generates.
CREATE INDEX entity_state_workspace_valid_window IF NOT EXISTS
FOR (s:EntityState) ON (s.workspace_id, s.id, s.valid_from, s.valid_to);

-- Retention-side lookup on EntityState by ingestion freshness — used by
-- the retention worker (ADR-0009).
CREATE INDEX entity_state_workspace_recorded_at IF NOT EXISTS
FOR (s:EntityState) ON (s.workspace_id, s.recorded_at);

-- Edge bitemporal seek. Neo4j 5.x supports relationship property indexes;
-- this is the same surface the existing edge_workspace_id index uses.
CREATE INDEX edge_workspace_valid_window IF NOT EXISTS
FOR ()-[r]-() ON (r.workspace_id, r.valid_from, r.valid_to);

CREATE INDEX edge_workspace_recorded_at IF NOT EXISTS
FOR ()-[r]-() ON (r.workspace_id, r.recorded_at);
```

The pre-existing edge-side indexes from ADR-0005 (`edge_workspace_id`, `edge_source_id`) are unchanged; the bitemporal indexes are additive. There is no relationship-uniqueness constraint by `(workspace_id, valid_from, edge_type, source_endpoint, target_endpoint)` — Neo4j 5.x relationship property *constraints* are limited and the uniqueness invariant is enforced in the writer plus a property test in [#138](https://github.com/100rd/Omniscience/issues/138). The constraint table in this section is exhaustive for what the adapter MERGE/MATCH paths rely on; anything else is over-indexing.

### 5. Query rewriting strategy for `as_of`

The canonical predicate is **open-closed `[valid_from, valid_to)` with `valid_to IS NULL` as the still-valid sentinel**:

```cypher
WHERE n.valid_from <= $as_of AND ($as_of < n.valid_to OR n.valid_to IS NULL)
```

The same predicate is applied to every `:EntityState` and every relationship in a traversal. A 2-hop `as_of` read takes the form:

```cypher
MATCH (a:Entity {workspace_id: $ws, id: $id})-[:HAD_STATE]->(sa:EntityState)
WHERE sa.valid_from <= $as_of AND ($as_of < sa.valid_to OR sa.valid_to IS NULL)
MATCH (a)-[r]->(b:Entity)
WHERE r.workspace_id = $ws
  AND r.valid_from <= $as_of AND ($as_of < r.valid_to OR r.valid_to IS NULL)
MATCH (b)-[:HAD_STATE]->(sb:EntityState)
WHERE sb.workspace_id = $ws
  AND sb.valid_from <= $as_of AND ($as_of < sb.valid_to OR sb.valid_to IS NULL)
RETURN sa, r, sb
```

Open-closed is justified for three independent reasons. **First**, contiguous half-open intervals tile the time axis without gaps or overlaps, which makes the "exactly one row per `(workspace_id, id, T)`" property test in [#138](https://github.com/100rd/Omniscience/issues/138) trivial: `[t1, t2) ∪ [t2, t3) = [t1, t3)`. Closed-closed would require disambiguating `valid_to = t2` and `valid_from = t2` on adjacent versions; closed-open would invert the boundary equality and is non-canonical in SQL. **Second**, open-closed is the canonical SQL bitemporal idiom (Snodgrass; the SQL:2011 system-versioning specification chose it; every reference text covers it as the default). **Third**, the predicate composes mechanically — the query rewriter in [#132](https://github.com/100rd/Omniscience/issues/132) can transform any current-state Cypher into an `as_of` Cypher by injecting the predicate at every node and edge match, with no per-kind branching.

**The "current state" read (`as_of=NULL` in the API) does NOT carry the predicate.** It reads the identity `:Entity` node directly via the existing `(workspace_id, id)` constraint or the existing `(workspace_id, kind)` / `(workspace_id, name)` indexes. The mirror of current state on the identity node (§2) is what makes this work — no `[:HAD_STATE]` traversal is required, no interval predicate is evaluated, no extra index seek happens. Performance parity with the pre-bitemporal read path is the design contract. The query rewriter must therefore branch on `as_of IS NULL` at the application layer and emit two distinct Cypher templates, not a single template parameterized by an optional `as_of`. Issue [#132](https://github.com/100rd/Omniscience/issues/132) is the binding sub-issue and the property test in [#138](https://github.com/100rd/Omniscience/issues/138) measures the latency-parity invariant on a representative fixture.

### 6. Cross-store alignment with Qdrant (ADR-0006)

ADR-0008 is the canonical source of bitemporal semantics; ADR-0006 is the consumer. The contract:

- **Unit**: ISO-8601 datetime strings on the Qdrant payload (Qdrant payload datetime fields are ISO-8601; the round-trip from the writer is `value.isoformat()`). Cypher `datetime` ↔ ISO-8601 ↔ Python `datetime` is lossless at microsecond resolution, which is the storage precision both stores carry.
- **Interval convention**: open-closed `[valid_from, valid_to)`, identical to the graph.
- **"Still valid" sentinel**: `valid_to` field absent or `null` on the payload, identical to the graph's `valid_to IS NULL`.
- **Indexed fields**: `recorded_at` payload-indexed (already specified by ADR-0006 §Schema posture). [#134](https://github.com/100rd/Omniscience/issues/134) extends payload indexing to `valid_from` and `valid_to` and adds `as_of` to the filter on every read method.

ADR-0006 §Schema posture and §ACL carry-forward are not amended in this PR. The amendment lives in a follow-up sub-issue (the Wave 6 [#139](https://github.com/100rd/Omniscience/issues/139) closer is the right home; alternately a one-line cross-reference is added by [#134](https://github.com/100rd/Omniscience/issues/134) as part of payload-bitemporal landing). This is consistent with how ADR-0005 was amended after-the-fact by ADR-0008 itself (this ADR), and how ADR-0004 was amended by ADR-0005 and ADR-0006 in their own PRs.

### 7. Postgres operational metadata posture

Postgres tables — `sources`, `ingestion_runs`, `documents`, `chunks`, `tokens`, `workspaces` — are **not** bitemporal. Their lifecycle is the existing tombstone/janitor model in [`docs/schema.md`](../schema.md) §"Tombstones not deletes". `documents.tombstoned_at` remains the ingestion-side soft-delete signal and is unchanged by this ADR.

This is deliberate. None of those tables answers a "what was true in the world at T" question — sources are configured or not, ingestion runs happened or didn't, tokens are valid or revoked, documents are tombstoned or not. Adding bitemporal columns to operational metadata would multiply Postgres churn for a property none of those tables need, and it would create a duplicate retention policy surface (Postgres operational vs. graph bitemporal) that is gratuitous. The cross-store alignment from §6 applies to the Qdrant payload only because Qdrant is the *content* store; Postgres is the *metadata* store and operates on a different lifecycle.

A well-meaning future PR is likely to drag bitemporality into Alembic. This section exists so that PR has an explicit ADR to point at when it gets rejected.

### 8. Migration path

**Phase 0 — bootstrap DDL.** The new constraints and indexes from §4 are added to `_BOOTSTRAP_STATEMENTS` in `neo4j_store.py` via [#130](https://github.com/100rd/Omniscience/issues/130). Bootstrap is idempotent (`IF NOT EXISTS`) and runs at adapter `connect()` time on the existing FastAPI lifespan path. The DDL has no `IS NULL` blockers — the indexes are built on properties that are populated immediately by phase 1.

**Phase 1 — backfill.** A one-shot migration sets `valid_from = created_at`, `valid_to = NULL`, `recorded_at = updated_at` on every existing `:Entity` node and every existing relationship in Neo4j. The pre-bitemporal data has no `:EntityState` chain — backfill creates the initial `(:EntityState)` for each identity with the same triple, points a single `[:HAD_STATE]` relationship at it, and leaves the identity node's mirror in place. The migration is idempotent: every SET is guarded by `WHERE n.valid_from IS NULL`, so re-runs are no-ops on rows already migrated. The backfill runs as a Neo4j adapter method invoked from a one-shot CLI (e.g. `omniscience-admin migrate-bitemporal`); the FastAPI lifespan does **not** run the backfill (operator-driven, not request-path-driven). Issue [#130](https://github.com/100rd/Omniscience/issues/130) is the binding sub-issue.

**Phase 2 — feature-flag rollout.** The new write path is governed by environment flag **`GRAPH_BITEMPORAL=enabled|disabled`**, default `disabled` for the rollout window. With the flag disabled, the writer emits the pre-bitemporal MERGE shape and existing reads continue unchanged. With the flag enabled, the writer emits the new MERGE-plus-`:HAD_STATE`-plus-`SET-valid_to` shape and reads can carry the optional `as_of` predicate. The flag flips after the backfill migration is verified on a representative dataset and the property tests in [#138](https://github.com/100rd/Omniscience/issues/138) pass.

**Cutover criterion.** Same shape as the ADR-0005 / ADR-0006 cutover ([#105](https://github.com/100rd/Omniscience/issues/105)): the property-test suite passes against the bitemporal write path in CI on a representative fixture, the read-latency parity test in [#138](https://github.com/100rd/Omniscience/issues/138) shows no regression on the current-state path, and a rollback PR is staged that flips `GRAPH_BITEMPORAL=disabled`. The post-cutover follow-up drops the legacy MERGE templates in a subsequent PR.

**Rollback.** During the flag-disabled window, flipping back is a config change. Post-cutover (legacy templates removed), rollback requires re-hydration from a Neo4j backup taken pre-cutover; the retention of that backup is part of the [#130](https://github.com/100rd/Omniscience/issues/130) checklist. This is not symmetric with ADR-0006's "rebuild from chunks" rollback — the graph is a stateful artifact, not a deterministic function of upstream content, so backup-driven rollback is the only correct strategy.

The migration path interacts with the bootstrap DDL in one specific way: the new constraints in §4 require the corresponding properties to be present, but `IF NOT EXISTS` on the constraints does not block creation if the property is null. The constraints become enforceable for new writes the moment the bootstrap runs; the backfill then populates existing rows. The ordering is: (a) bootstrap DDL, (b) backfill, (c) flag flip, (d) cutover.

### 9. Identity stability invariant

`(workspace_id, id)` is **stable across versions**. A version change on an identity produces a new `:EntityState` and updates the `:Entity` node's mirrored properties, but the `:Entity` node's `id` does not change and the `(workspace_id, id)` UNIQUE constraint from ADR-0005 §Schema posture is unviolated.

This is a property-test target for Wave 6 ([#138](https://github.com/100rd/Omniscience/issues/138)). The invariants:

1. For any `(workspace_id, id)` pair, there is exactly one `:Entity` node.
2. For any `(workspace_id, id, T)` triple where `T` is in the recorded-time domain, there is exactly one `:EntityState` node `s` with `s.valid_from <= T AND (T < s.valid_to OR s.valid_to IS NULL)`.
3. The set of `:EntityState` nodes reachable from a given `:Entity` via `[:HAD_STATE]` has pairwise-disjoint validity windows (no overlap, no gap unless the entity was end-dated and resurrected).
4. The current state on the `:Entity` node mirrors exactly the `:EntityState` where `valid_to IS NULL` (or no rows where `valid_to IS NULL` if the entity is end-dated; in that case the `:Entity` node's `valid_to` is non-null and matches the latest `:EntityState.valid_to`).

These four invariants are the contract Wave 6 measures; a downstream engineer picking up [#130](https://github.com/100rd/Omniscience/issues/130) or [#138](https://github.com/100rd/Omniscience/issues/138) can implement directly from this list.

## Alternatives rejected

### Identity model — multi-node-versioned with `[:NEXT_VERSION_OF]`

Rejected. The shape is: one `:Entity` node per `(identity, version)`, linked into a chain via `[:NEXT_VERSION_OF]`, with a stable `entity_key` property that exposes identity to consumers. The appeal is conceptual symmetry — every node is a complete versioned snapshot, and `as_of` is a single MATCH with the interval predicate on the node directly, no relationship traversal.

The specific reasons it does not fit Omniscience:

- **It widens the uniqueness constraint across the entire codebase.** ADR-0005 §Schema posture committed `(workspace_id, id) IS UNIQUE` on `:Entity` and the regression guards in `neo4j_store.py` (`_ensure_workspace_predicate`, the import-time checks) and the workspace-isolation tests at `tests/test_graph_workspace_isolation.py` are written against that constraint shape. Multi-node versioning forces the constraint to widen to `(workspace_id, id, valid_from) IS UNIQUE` or to relax to non-unique with a composite index. Both change the surface that #117/#119 ACL carry-forward depends on. The cross-cutting churn cost is unbounded relative to the value.
- **Edges multiply.** A `(:Entity)-[:DEPENDS_ON]->(:Entity)` relationship in the multi-node model has to point at a specific version of each endpoint or at an identity-key indirection. The first re-introduces relationship rewrites on every version bump (write amp on every relationship that points to "current"); the second re-introduces the same `[:HAD_STATE]`-style indirection that we picked, but on the relationship side instead of the node side, and breaks the planner's ability to use direct relationship indexes.
- **The current-state plan regresses.** The dominant read is "current state of entity by id" with no `as_of`. In the multi-node model, there is no privileged "current" node — the read has to filter `WHERE valid_to IS NULL` and seek the index `(workspace_id, entity_key, valid_from, valid_to)` on every read, paying the interval-predicate cost even for the most common operational shape. The chosen design pays the predicate cost only when `as_of` is actually supplied.
- **Write amp is comparable but qualitatively worse.** Both designs produce ~3 ops per state change, but multi-node versioning adds nodes proportional to update rate (over months and years, this is the dominant graph-size driver), whereas property-versioning adds `:EntityState` nodes — same growth rate, but the topology of the graph (number of `:Entity` nodes and the relationships between them) is bounded by identity count, not by update rate. Retention (ADR-0009) is therefore simpler under property-versioning: aging out the `:EntityState` chain does not break edge integrity, because edges point at identity nodes.

### Identity model — edge-versioned-only (always-latest nodes)

Rejected. Nodes carry the latest state via `SET` updates; only relationships are bitemporal. The appeal is cost — no version chain, no `:EntityState`, no write amp on entity properties.

It violates the spec on its face. Vision §5.3 (line 71-77) says "every entity AND every edge" is bitemporal. Issue [#128](https://github.com/100rd/Omniscience/issues/128) §1 makes the property-semantics decision a per-entity decision. The architect memo at [#97](https://github.com/100rd/Omniscience/issues/97) explicitly lists this option as one of the three but flags it as "loses point-in-time entity reads". In the incident-reasoning use case that motivated this ADR ("what did the operator on call know about the pod's deployment state at 19:25Z?"), the entity's property state at T is the *answer* — losing it defeats the bitemporal model's reason to exist.

The design also breaks the identity-stability invariant in §9 in a subtle way. `(workspace_id, id)` is still stable as a node identifier, but there is no version concept on the node, so the property test in [#138](https://github.com/100rd/Omniscience/issues/138) collapses to "the current state of the node is whatever was last written" — which is the pre-bitemporal contract this ADR amends.

It is the right choice for a system where entities are pure references and only relationships carry meaningful state changes. That is not Omniscience.

### Interval convention — closed-closed `[valid_from, valid_to]`

Rejected. The appeal is symmetry — both endpoints inclusive, conceptually clean. The fatal problem is interval composition: when version v1 ends at `t2` and v2 begins at `t2`, both rows include `t2`, which means a query at `as_of=t2` returns both. Disambiguation requires "last-write-wins" tie-breaking on `recorded_at`, which is an implicit extra clause on every read predicate and a class of subtle bugs when the disambiguation is forgotten. SQL bitemporal references uniformly chose half-open intervals to avoid this, and the SQL:2011 system-versioning standard codified `[valid_from, valid_to)` as the canonical shape. Following the canon is the right choice.

Closed-open `(valid_from, valid_to]` was considered briefly. It has the same composition property as open-closed but inverts the boundary equality, which is non-canonical and would have to be re-explained on every read site forever. No upside.

### Migration strategy — dual-write window across two graphs

Rejected. The shape of ADR-0005 / ADR-0006's migration ([#108](https://github.com/100rd/Omniscience/issues/108), [#105](https://github.com/100rd/Omniscience/issues/105)) was a true dual-write across distinct backends: pgvector and Neo4j held the same data, the read path could shadow-compare, and the cutover flipped the read backend. It was a backend-substitution migration.

ADR-0008 is a schema-evolution migration on the same backend. A dual-write across two Neo4j databases (or two label sets in the same database) was considered: writers publish to both the pre-bitemporal and bitemporal shape, the read path can shadow-compare, and the cutover flips. The reasons against:

- **Storage doubling for a transitional window**, with a full-graph backfill on the new shape that has no operational benefit beyond what an in-place SET-with-`IF NULL` guard already gives.
- **Two write paths for an extended window** is a regression in operational complexity for a property — the graph's property shape — that has a single source of truth (the writer).
- **Shadow-comparison adds nothing.** ADR-0005 / ADR-0006 needed shadow-reads because the *retrieval semantics* might differ between Postgres recursive CTEs and Cypher. Here, the property semantics are deterministic: `valid_from = created_at`, `valid_to = NULL`, `recorded_at = updated_at` is a mechanical mapping, and the property test in [#138](https://github.com/100rd/Omniscience/issues/138) asserts equivalence directly without a parallel write path.

The chosen migration — bootstrap DDL + idempotent backfill + feature-flagged write path — is a strict subset of the dual-write strategy with the same correctness guarantee at lower operational cost. It is the right shape for an in-place schema evolution.

## Consequences

### Positive

- **`as_of` becomes a single, mechanical Cypher rewrite.** A query rewriter can transform any current-state template into a point-in-time template by injecting `WHERE valid_from <= $as_of AND ($as_of < valid_to OR valid_to IS NULL)` at every `:EntityState` and every relationship match. No per-kind hand-tuning. [#132](https://github.com/100rd/Omniscience/issues/132) is the binding sub-issue.
- **Current-state reads do not regress.** The mirror of current state on the `:Entity` node preserves the dominant-read latency profile from the pre-bitemporal path. The performance-isolation guarantee in §5 is structural, not a tuning artifact.
- **Cross-store alignment with Qdrant is mechanical.** Same units, same interval convention, same sentinel — the graph and vector stores agree on `(valid_from, valid_to, recorded_at)` semantics and a vector search at `as_of=T` returns chunks consistent with the graph at T. ADR-0006 is unchanged in this PR; the cross-reference lands in [#134](https://github.com/100rd/Omniscience/issues/134).
- **Edge tombstoning preserves history.** End-dating replaces hard deletion in the writer path; `as_of` queries against a relationship that has since ended return the relationship as it was, which is the bitemporal contract's whole point.
- **Identity-stability invariant is testable.** The four invariants in §9 are direct property-test targets and a downstream engineer can implement [#138](https://github.com/100rd/Omniscience/issues/138) from this list alone.

### Negative — operational

- **Storage growth.** Every state change adds an `:EntityState` node plus a `[:HAD_STATE]` relationship. At the v0.5 envelope (single-node Neo4j with 100 GiB SSD) this is bounded; at GA scale and one-year warm retention (Vision §5.3, ADR-0005 §Negative-operational) it is the dominant storage cost. ADR-0009 (retention tiering, [#129](https://github.com/100rd/Omniscience/issues/129) parallel sibling) is the operational counter-pressure on this growth and ADR-0008 is silent on the eviction policy by design.
- **Two write Cypher templates** during the rollout window — the pre-bitemporal MERGE and the bitemporal MERGE-plus-`:HAD_STATE`-plus-`SET-valid_to`. Resolved at cutover when the legacy template is removed, but the maintenance load during the window is real.
- **The corruption guards live in the writer, not in Neo4j.** `valid_from < valid_to`, the edge-endpoint validity-window invariant from §3, and `recorded_at` monotonicity per `(workspace_id, id)` cannot be expressed as Neo4j 5.x constraints. The writer enforces them; the property tests in [#138](https://github.com/100rd/Omniscience/issues/138) verify them; reviewers must hold the line that no future PR drops the guards in pursuit of a faster path. The same pattern as the `_ensure_workspace_predicate` import-time guards in `neo4j_store.py`.
- **Backfill is a one-shot operation that cannot be undone in place.** Once `valid_from`, `valid_to`, `recorded_at` are populated on existing nodes and the corresponding `:EntityState` chains are created, rolling back requires the pre-bitemporal Neo4j backup. The operational checklist in [#130](https://github.com/100rd/Omniscience/issues/130) covers backup capture before backfill; reviewers must ensure that step is not deferred.

### Negative — team

- **Bitemporal Cypher fluency is a new skill on top of the existing Cypher onboarding from ADR-0005.** The interval predicate, the open-closed convention, the `IS NULL` sentinel, the `:HAD_STATE` traversal idiom — all are mechanical once internalized but each is one more thing to keep in working memory. Mitigation: the query rewriter in [#132](https://github.com/100rd/Omniscience/issues/132) hides the predicate from application code; `as_of` is a parameter, not a Cypher fragment that callers compose.
- **The "current state on `:Entity` mirrors latest `:EntityState`" rule is a correctness invariant.** A future engineer who writes to `:Entity` properties without writing the matching `:EntityState` row creates skew that is silent until an `as_of=now` read returns inconsistent state. The writer contract in [#131](https://github.com/100rd/Omniscience/issues/131) makes the dual write atomic in a single transaction; the property test in [#138](https://github.com/100rd/Omniscience/issues/138) catches skew in CI. Reviewers must reject any direct-to-`:Entity` write outside the writer.
- **Edge tombstone semantics change the writer contract from PR [#104](https://github.com/100rd/Omniscience/issues/104).** The previous mental model — "the writer deletes relationships that no longer appear in source data" — flips to "the writer end-dates them". The mental shift is small but it is a real model change and the documentation in [#137](https://github.com/100rd/Omniscience/issues/137) is the place to land it for the team.

### Negative — security

The carry-forward from ADR-0005 §Consequences-security and the P0 fix landed by [#117](https://github.com/100rd/Omniscience/issues/117) / [#119](https://github.com/100rd/Omniscience/issues/119) is non-negotiable and tightens here:

1. **Every new constraint and index in §4 is composite on `workspace_id`.** This is checked verbatim in the §4 list. No bitemporal index — entity, EntityState, or relationship — drops the workspace prefix. The `entity_state_workspace_id_valid_from_unique` constraint is uniqueness on `(workspace_id, id, valid_from)`, never on `(id, valid_from)` alone; the `entity_state_workspace_valid_window` index is composite on `(workspace_id, id, valid_from, valid_to)`, never on `(valid_from, valid_to)` alone.
2. **The bitemporal predicate sits on top of the workspace predicate, never replaces it.** The §5 canonical predicate is always preceded by `n.workspace_id = $workspace_id` (or the `MERGE`-pattern equivalent that includes `workspace_id` in the node-key map). A future engineer who refactors the rewriter to "elide the workspace predicate when as_of is set" recreates the [#117](https://github.com/100rd/Omniscience/issues/117) class of bug in a new dimension. The query rewriter in [#132](https://github.com/100rd/Omniscience/issues/132) MUST emit the workspace predicate before the bitemporal predicate, and the import-time regression guard pattern (`_ensure_workspace_predicate` in `neo4j_store.py`) MUST extend to assert workspace presence on every new bitemporal Cypher template.
3. **Cross-workspace planted edges remain a class of attack vector.** A malicious or buggy writer that creates a relationship with `r.workspace_id` set to a foreign workspace can plant a cross-tenant traversal. The carry-forward fix from [#119](https://github.com/100rd/Omniscience/issues/119) — endpoint-workspace verification on every read in `graph_query.py`'s BFS expansion — is unchanged by this ADR but the bitemporal predicate adds a new dimension where it must hold: an end-dated edge from a foreign workspace must not be visible at `as_of < end` to a caller in the local workspace. The endpoint-workspace check on both ends is sufficient because the workspace predicate on the relationship itself plus on the endpoints closes the leak.
4. **The property test in [#138](https://github.com/100rd/Omniscience/issues/138) MUST include cross-workspace bitemporal isolation.** Two workspaces with overlapping entity ids and overlapping validity windows; an `as_of` read authenticated to workspace A must return only workspace A's state, never any version (current or historical) of workspace B's state. This is the ADR-0005 §Consequences-security carry-forward applied to the bitemporal dimension and a non-skippable test target.

The same discipline ADR-0005 mandated for the Neo4j adapter and ADR-0006 mandated for the Qdrant adapter applies here verbatim: workspace_id is the first predicate, the bitemporal predicate composes on top, and the import-time regression guards reject any template that drops the workspace clause.

### Negative — cost

- **Storage cost grows roughly linearly with update rate.** Each state change adds one `:EntityState` node, one `[:HAD_STATE]` relationship, and rewrites one `valid_to`. For an entity that updates daily, one year of warm retention adds ~365 `:EntityState` nodes per identity. Bounded by retention (ADR-0009); meaningful at GA.
- **Edge end-dating delays storage reclamation.** A relationship that would have been deleted in the pre-bitemporal model now persists with `valid_to` set. Storage cost equivalent to keeping the edge with one extra timestamp until the retention worker reaps it. Bounded by retention.
- **Index cost.** Three new node-side indexes (one constraint, two indexes) plus two new relationship-side indexes. Memory cost on the Neo4j page cache is meaningful at GA scale; v0.5 envelope absorbs it.

### Risks

- **Backfill scaling.** A representative dataset for the [#130](https://github.com/100rd/Omniscience/issues/130) backfill does not exist as a fixture today. The backfill on the v0.5 pilot dataset is small enough to run in seconds; the backfill on a customer dataset at GA may take minutes-to-hours and may require a maintenance window. Mitigation: [#130](https://github.com/100rd/Omniscience/issues/130) commits to a chunked, resumable backfill from day one, not a `MATCH (n) SET …` that locks the entire graph.
- **Property-test fixture coverage.** The four invariants in §9 plus the cross-workspace bitemporal isolation invariant in §Consequences-security plus the latency-parity invariant in §5 are five property-test targets. The Wave 6 sub-issue [#138](https://github.com/100rd/Omniscience/issues/138) must own a representative fixture with multiple workspaces, multiple entity kinds, multiple edge types, and a non-trivial version chain depth. Risk: the fixture is under-specified at issue time and under-realistic at test time. Mitigation: the property test landed under [#138](https://github.com/100rd/Omniscience/issues/138) is written against generative `hypothesis`-style fixtures so the invariant is asserted across a class of graphs, not against a fixed shape.
- **Late-arriving connector data.** The `recorded_at` monotonicity rule in §1 clamps very-late-arriving data to "now", which means the operator clock on the graph never lies but it also means a connector that catches up after a multi-day outage produces a `recorded_at` cluster at the catch-up time rather than at the true ingestion time. This is acceptable — the world clock fields preserve the real-world reality — but it is a class of operator confusion that should be documented in the freshness runbook ([`docs/freshness-and-lineage.md`](../freshness-and-lineage.md)) when [#137](https://github.com/100rd/Omniscience/issues/137) lands.
- **Bitemporal-predicate planner stability under Neo4j 5.x.** The planner's choice between the `(workspace_id, id)` constraint and the `(workspace_id, id, valid_from, valid_to)` composite index for an `as_of` read is empirical. Risk: under load, the planner picks the narrower index for the wider seek and the read regresses. Mitigation: the latency-parity test in [#138](https://github.com/100rd/Omniscience/issues/138) measures both shapes; index hints are available as a Neo4j 5.x escape valve if the planner regresses; the ADR does not pre-empt the index-hint decision.

## Revisit triggers

- **Multi-node versioning becomes the right answer if** edge-side write amp on a "current relationship" rewrite (the cost of `[:HAD_STATE]` indirection on edge-target lookups) becomes the dominant write cost, OR the planner regresses on `:HAD_STATE` traversals at large fan-out and index hints do not recover the regression. Reopen the §2 decision.
- **Closed-closed becomes the right answer if** an external system Omniscience integrates with mandates a closed-closed bitemporal contract and the integration cost of converting on every export exceeds the cost of the disambiguation tax on every read. Unlikely; documented for completeness.
- **`+infinity` sentinel becomes the right answer if** Cypher's `IS NULL` planner support regresses in a future Neo4j version (Neo4j 6.x is the watch point) and the workaround cost exceeds the disambiguation cost. Reopen §1.
- **Postgres bitemporal becomes the right answer if** a use case for "what did our operational metadata look like at T" emerges — the current answer is "ingestion runs and tombstones answer this in their own model" but a customer requirement could change that. Reopen §7.
- **Restricted-to-subset bitemporality becomes the right answer if** GA-scale storage cost forces a per-entity-kind opt-out (Vision §11 line 177 explicitly flags this as a possibility). The architectural cost of "some kinds are bitemporal, others are not" is meaningful — reopen the design before committing.

## Cross-doc consequences

- **[ADR-0005](0005-neo4j-as-graph-store.md)** §Schema posture — the placeholder bullet that defers bitemporal enforcement to "a follow-up ADR tied to epic #97" is now satisfied by ADR-0008. ADR-0005 is amended in a follow-up sub-issue (Wave 6 [#139](https://github.com/100rd/Omniscience/issues/139), or a dedicated cross-reference PR — out of scope for this ADR's PR per issue [#128](https://github.com/100rd/Omniscience/issues/128) scope guardrails) to add a one-line "see ADR-0008 for the bitemporal contract" cross-link. The §Consequences-security carry-forward in ADR-0005 is unchanged by ADR-0008 and is reaffirmed here in §Consequences-security.
- **[ADR-0006](0006-qdrant-as-vector-store.md)** §Schema posture — the placeholder properties on the Qdrant payload (`valid_from`, `valid_to`, `recorded_at` with `recorded_at` indexed) are confirmed by ADR-0008 §6 with the open-closed interval, `null`/missing sentinel for `valid_to`, and ISO-8601 datetime unit fixed. ADR-0006 is amended in a follow-up sub-issue ([#134](https://github.com/100rd/Omniscience/issues/134) or [#139](https://github.com/100rd/Omniscience/issues/139)) to add a one-line "see ADR-0008 for the canonical bitemporal contract" cross-link and to add `valid_from` and `valid_to` to the payload-indexed list. ADR-0006 is unchanged in this PR.
- **[`docs/schema.md`](../schema.md)** — the entity / edge schema reference adds a one-line "see ADR-0008 for bitemporal property semantics on Neo4j entities and edges; Postgres operational tables remain non-bitemporal per ADR-0008 §7" cross-link. The cross-link is the only edit `schema.md` takes in epic #97; the table definitions are unchanged because no Postgres column changes.
- **[`docs/vision.md`](../vision.md)** §5.3 (line 71-77) and §11 (line 177) — both already name the bitemporal model. ADR-0008 makes the implementation contract explicit; the vision sections are not edited in this PR. A future cross-reference under `## 12. Reference documents` should list ADR-0008 alongside ADR-0005 and ADR-0006 once the ADRs index is introduced.
- **[`docs/decisions/`](.) index** — when the `docs/decisions/README.md` index is introduced (recommended in ADR-0005 §Consequences-for-related-docs and ADR-0007 §Consequences-for-related-docs), ADR-0008 should be listed alongside ADR-0005, ADR-0006, ADR-0007 with a one-line summary: "Bitemporal schema for Neo4j entities and edges — property-versioned identity nodes with `:HAD_STATE` chains, open-closed intervals, `valid_to IS NULL` for still-valid".

## Links

- Parent epic: [#97](https://github.com/100rd/Omniscience/issues/97)
- This issue: [#128](https://github.com/100rd/Omniscience/issues/128)
- Parallel sibling ADR: [#129](https://github.com/100rd/Omniscience/issues/129) — retention tiering policy and storage layout (ADR-0009)
- Blocks: [#130](https://github.com/100rd/Omniscience/issues/130), [#131](https://github.com/100rd/Omniscience/issues/131), [#132](https://github.com/100rd/Omniscience/issues/132), [#133](https://github.com/100rd/Omniscience/issues/133), [#134](https://github.com/100rd/Omniscience/issues/134), [#137](https://github.com/100rd/Omniscience/issues/137), [#138](https://github.com/100rd/Omniscience/issues/138), [#139](https://github.com/100rd/Omniscience/issues/139)
- Amends: [ADR-0005](0005-neo4j-as-graph-store.md) §Schema posture
- Cross-store contract: [ADR-0006](0006-qdrant-as-vector-store.md) §Schema posture
- ACL carry-forward: [#117](https://github.com/100rd/Omniscience/issues/117), [#119](https://github.com/100rd/Omniscience/issues/119), [ADR-0005](0005-neo4j-as-graph-store.md) §Consequences-security
- Vision sections: [`docs/vision.md`](../vision.md) §5.3 (line 71-77), §11 (line 177)
- Adapter touched by future migration (this ADR specifies, [#130](https://github.com/100rd/Omniscience/issues/130) implements): [`packages/index/src/omniscience_index/stores/neo4j_store.py`](../../packages/index/src/omniscience_index/stores/neo4j_store.py)
