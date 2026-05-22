# ADR-0012 — Per-type relationship-property indexes for Neo4j bootstrap

- **Status**: Accepted
- **Date**: 2026-05-22
- **Deciders**: Backend Engineer, Architect
- **Issue**: [#224](https://github.com/100rd/Omniscience/issues/224)
- **Builds on**: [ADR-0005](0005-neo4j-as-graph-store.md) (Neo4j as the
  graph store), [ADR-0008](0008-bitemporal-schema-for-neo4j.md) (bitemporal
  schema), [ADR-0009](0009-retention-tiering-policy.md) (hot/warm/archive
  tiering — the dominant consumer of edge indexes).

## Context

`packages/index/src/omniscience_index/stores/neo4j_store.py` declared four
relationship-property indexes in `_BOOTSTRAP_STATEMENTS` with an untyped
relationship pattern:

```cypher
CREATE INDEX edge_workspace_id          IF NOT EXISTS FOR ()-[r]-() ON (r.workspace_id)
CREATE INDEX edge_source_id             IF NOT EXISTS FOR ()-[r]-() ON (r.source_id)
CREATE INDEX edge_workspace_valid_window IF NOT EXISTS FOR ()-[r]-() ON (r.workspace_id, r.valid_from, r.valid_to)
CREATE INDEX edge_workspace_recorded_at  IF NOT EXISTS FOR ()-[r]-() ON (r.workspace_id, r.recorded_at)
```

Neo4j 5.x **rejects** this syntax at parse time — a specific relationship
type is required (`FOR ()-[r:TYPE]-() ON (r.prop)`). The compose stack
pins `neo4j:5.19-community`, so every cold-start of the application
container raises `CypherSyntaxError` from `_bootstrap_schema()` (invoked
in the FastAPI lifespan at `apps/server/src/omniscience_server/app.py:172`).
The app container enters a restart loop and `admin` (which depends on
`app: service_healthy`) never starts.

Edges in this codebase are written with **dynamic** relationship types:

```cypher
MERGE (a)-[r:`{edge_type}` {workspace_id: $workspace_id}]->(b)
```

`edge_type` is sourced from:

1. **Connectors** — Slack (`in_channel`, `in_thread`, `authored`,
   `mentions`), alerts (`FIRES_AGAINST`), infra parsers (`depends_on`,
   `references`, `owns`, `selects`, `mounts`, `tfstate_of`), code parsers
   (`imports`, `calls`, `inherits`, `defines`).
2. **API consumers** — `_validate_edge_types` (line 1237) accepts any
   value matching `_EDGE_TYPE_REGEX = ^[A-Za-z][A-Za-z0-9_]{0,63}$`. The
   set is **not** drawn from a static enum.

The set is therefore **bounded in practice** (the union over connectors
is ~15 types today) but **unbounded at the type-system surface** (a new
connector or a new API client can introduce a type with no code change in
this module).

## Decision

**Per-relationship-type indexes, discovered dynamically at bootstrap time.**

1. The four broken untyped statements are removed from `_BOOTSTRAP_STATEMENTS`.
2. The four index shapes are kept as **DDL templates** in a new tuple
   `_EDGE_INDEX_TEMPLATES`, each carrying `{name}` and `{rel_type}`
   placeholders.
3. `_bootstrap_schema()` runs in two phases:
   - **Phase 1** — execute `_BOOTSTRAP_STATEMENTS` (the static node-label
     DDL from ADR-0005 and the ADR-0008 §4 node-label additions).
   - **Phase 2** — issue `CALL db.relationshipTypes()` to enumerate the
     live relationship-type set, validate each type against
     `_EDGE_TYPE_REGEX` (defence in depth), and render
     `len(_EDGE_INDEX_TEMPLATES) × len(types)` `CREATE INDEX … IF NOT
     EXISTS` statements. Per-type index names follow the convention
     `<prefix>__<rel_type>` (e.g. `edge_workspace_id__CALLS`) so the
     prefix is parseable back out for debugging.

## Alternatives considered

### Option A — Drop the four indexes entirely

Drop the broken statements; never recreate any relationship-property
index. Simplest possible change.

**Rejected.** Two production query paths depend on these indexes:

- `_COUNT_EDGES_BY_TYPE_CYPHER` (line 1116 of `neo4j_store.py`) and the
  retention worker (`_COUNT_HOT_TO_WARM_EDGE_ELIGIBLE`,
  `_MARK_HOT_TO_WARM_EDGE`, `_MOVE_HOT_TO_WARM_EDGE`,
  `_BACKFILL_EDGE_PROPS_CYPHER`) are not node-anchored — they all start
  with `MATCH ()-[r]->() WHERE r.workspace_id = $workspace_id …`. Without
  a relationship-property index Neo4j falls back to
  `AllRelationshipsScan`, which on the v0.4 envelope (millions of edges
  per workspace) makes the hot-to-warm retention pass minutes-long.
- The traversal templates (`_TRAVERSE_*_CYPHER_TEMPLATE`) are node-
  anchored on the seed entity and benefit less, but the workspace
  predicate is still verified on every traversed relationship; without
  an index the verification is a property lookup per edge rather than an
  index seek.

The retention worker is the dominant consumer here, and ADR-0009
§Risks explicitly calls out "scan cost on the hot-to-warm eligibility
predicate" as a non-negotiable. Dropping the indexes regresses §2 of
that ADR.

### Option B — Static allowlist of known edge types

Hard-code the union of connector-emitted types in
`_BOOTSTRAP_STATEMENTS` and emit per-type DDL at module load.

**Rejected.** Three problems:

1. **Coupling.** `packages/index/` would need to know about every
   relationship type emitted by every connector / parser package. The
   `packages/connectors/` and `packages/parsers/` packages currently have
   zero compile-time coupling to `packages/index/`; introducing a
   reverse dependency makes the connector inventory part of the index
   adapter's API.
2. **API exposure.** `_validate_edge_types` accepts any regex-matching
   identifier — API clients can introduce types at runtime. A static
   allowlist silently leaves those types unindexed.
3. **Drift.** Every new connector / parser PR would need a paired
   schema bump here; nothing in CI catches the missing index until
   production hot-to-warm latency degrades. Forgetting one is a
   silent SLO regression.

### Option C — Schema redesign: edges as nodes

The bitemporal warm tier already materialises edges as
`(:RelationshipSnapshot:Daily)` nodes (line 155, `_RELATIONSHIP_SNAPSHOT_LABELS`).
Extend the same pattern to the hot tier — write every edge as a node, link
endpoints via two dedicated relationships, and index on the node label.

**Rejected.** This is the largest possible change, and it conflicts with
multiple existing invariants:

- ADR-0008 §3 explicitly states "edges remain identity-to-identity; the
  bitemporal triple lives on the relationship". Every read template
  (`_TRAVERSE_*`, `_GET_ENTITY_BY_NAME_AS_OF_CYPHER`) and every write
  template (`_UPSERT_EDGE_BITEMPORAL_CYPHER_TEMPLATE`,
  `_END_DATE_BY_SOURCE_CYPHER`, `_END_DATE_TOMBSTONED_CYPHER`) assumes
  this shape. Rewriting them is touching ~500 lines of Cypher and the
  entire bitemporal test suite (`tests/test_neo4j_store_bitemporal_*`).
- ADR-0009 §1 deliberately distinguishes the **hot** tier (identity-to-
  identity edges) from the **warm** tier (edges-as-snapshot-nodes) for
  query-plan isolation. Collapsing the distinction loses that property.
- The bug we are fixing is a cold-start crash; the smallest correct fix
  should not require an architectural rewrite.

## Consequences

### Positive

1. **Cold-start works.** `_bootstrap_schema()` no longer raises
   `CypherSyntaxError`; `docker compose up -d` reaches `app (healthy)`
   on a fresh checkout. The v0.4 release is unblocked.
2. **No coupling.** Connectors and parsers continue to introduce new
   relationship types with zero changes in `packages/index/`. The
   first cold-start after a new type appears in the data picks it up
   automatically via `db.relationshipTypes()`.
3. **Index coverage is data-driven.** Every type the system has ever
   written gets indexed on the next restart; on a populated production
   database the index set is complete after one restart.
4. **Idempotent.** Every per-type CREATE uses `IF NOT EXISTS`; re-runs
   are O(1) per statement against the schema cache.

### Negative

1. **New types are unindexed until the next restart.** A relationship
   type that first appears between two cold-starts is queryable but
   uses `AllRelationshipsScan` for the lookups that depend on the
   relationship-property indexes (retention worker, stats). Restart
   triggers re-indexing; in steady state the gap is bounded by pod
   uptime. Operators can also invoke `_bootstrap_schema()` manually
   via a maintenance task if a new type is introduced mid-window —
   the method is public-facing (private-underscore convention, but
   reachable on `Neo4jGraphStore`) and idempotent.
2. **Bootstrap cost scales linearly with the type cardinality.**
   `len(_EDGE_INDEX_TEMPLATES) × len(types)` round-trips per startup.
   With the four templates and an expected ~15 types, that is 60
   DDL statements — each a no-op `IF NOT EXISTS` on a warm DB,
   resolving in single-digit ms from the schema cache. Sanity floor
   asserted by `test_bootstrap_is_fast_on_empty_container`.
3. **One extra read round-trip.** `CALL db.relationshipTypes()` runs
   once per `connect()`. Sub-millisecond on the v0.4 envelope.

### Security

1. **Defence in depth at index time.** Every relationship type returned
   by `db.relationshipTypes()` is validated against `_EDGE_TYPE_REGEX`
   before being substituted into Cypher. The writer enforces the same
   regex at write time (`Neo4jGraphStore.upsert_edge` line 1901), so a
   non-matching type can only appear if (a) the database was populated
   by an out-of-band tool or (b) the regex was tightened after data
   was written. In either case bootstrap continues — the type is
   logged and skipped, the dependent writer call would reject the
   next write of that type, and the operator has a structured-log
   signal to investigate.
2. **No new ACL surface.** The dynamic indexes carry the same
   `workspace_id`-leading composite ordering as the static node-label
   indexes; ACL isolation invariants from ADR-0005 §Consequences,
   ADR-0008 §Consequences-security #1, and ADR-0009 §Consequences-
   security #1 are unchanged.
3. **No SQL/Cypher injection.** The regex restricts `rel_type` to
   `[A-Za-z][A-Za-z0-9_]{0,63}` — no characters can break out of the
   backtick-quoted identifier (`r:`\`{rel_type}\``).

## Why this wasn't caught — and what changes

The bug shipped because `tests/test_neo4j_store_bitemporal_schema.py:113`
only string-grepped for `edge_workspace_id` in `_BOOTSTRAP_STATEMENTS` —
it never executed the DDL. The repo declares `testcontainers[neo4j,
qdrant]>=4.8.0` in `[dependency-groups] dev` but every existing Neo4j
testcontainer test is gated behind `OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1`,
which CI does not set. The bug was therefore invisible to the lint
suite, invisible to CI, and only surfaced on `docker compose up`.

**Fix going forward**: a new integration test at
`tests/integration/test_neo4j_bootstrap_schema.py` runs
`_bootstrap_schema()` end-to-end against `neo4j:5.19-community` and
asserts (a) the static DDL completes, (b) per-type indexes are created
for every live relationship type with the expected shape, and (c)
re-running the bootstrap is a no-op. This test is **not** gated behind
any opt-in env var — it skips only when Docker / testcontainers are
unavailable, which is a hard environmental constraint, not an opt-in.

The existing opt-in Neo4j contract tests are left as-is in this ADR's
scope; widening their gate is a separate cleanup that should land as
its own PR.

## Out of scope

- **Re-running the index discovery during steady state** (e.g., after
  every upsert that introduces a new edge type). The CALL is cheap but
  not free; per-write discovery is a latency regression for a problem
  bounded by pod uptime. Revisit if the gap shows up in production
  telemetry.
- **Materialising `(:RelationshipSnapshot)` for hot edges** (Option C).
  If a future ADR redesigns the hot tier, this ADR's per-type indexes
  become moot and can be removed.
- **Adding a `force_reindex=True` option to `_bootstrap_schema()`** for
  drops of pre-existing per-type indexes. Not needed today —
  `IF NOT EXISTS` keeps the dynamic path safe; if we ever need to
  rebuild an index with a different shape, that is a manual operator
  action with its own runbook.
