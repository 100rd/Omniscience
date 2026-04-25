# ADR 0005 — Neo4j as graph store

- **Status**: Implemented
- **Date**: 2026-04-22
- **Implemented**: 2026-04-24 (Epic #96 Phase 5 cutover, #105)
- **Amends**: [ADR-0004](0004-retrieval-strategy-staged.md) — the "Use Neo4j as primary graph store" rejection

## Implementation notes

This ADR landed across three PRs:

- **[#103](https://github.com/100rd/Omniscience/issues/103)** — introduced the backend-neutral `GraphStore` protocol and wrapped the legacy pgvector writer behind a feature flag. No behaviour change.
- **[#104](https://github.com/100rd/Omniscience/issues/104)** — landed `Neo4jGraphStore` as the Phase-2a graph backend with full parity tests, behind `STORAGE_GRAPH_BACKEND=neo4j`.
- **[#107](https://github.com/100rd/Omniscience/issues/107)** — `GraphRAGComposer` composed `GraphStore` + `VectorStore` into the single retrieval entry point consumed by MCP and REST.

Phase 5 (**[#105](https://github.com/100rd/Omniscience/issues/105)**, v0.2.0) removed the pgvector adapter and made Neo4j the only supported graph backend. The `STORAGE_GRAPH_BACKEND` default flipped to `neo4j`; any other value is rejected at startup. Operational metadata (sources, ingestion runs, tokens, workspaces) remains in Postgres.

## Context

ADR-0004 rejected a dedicated graph database for v0.2 on the grounds that Postgres + an `edges` table + recursive CTEs (with Apache AGE as a fallback for Cypher) would cover the structural queries we expected. That decision was correct for the product Omniscience was then: a retrieval service that added lightweight structural edges to a hybrid vector + BM25 baseline.

The product has since been re-scoped. The current statement of direction is [`docs/vision.md`](../vision.md), which describes Omniscience as a **Living Semantic Core** — a causal, temporal, semantic graph exposed via MCP (vision §1). Two sections are load-bearing for this ADR:

- **§5.1 Hybrid knowledge graph** makes the graph store the **primary** data model. The product's core query pattern is **GraphRAG composition**: resolve an entity in the graph, traverse causal/ownership edges to candidate causes, scope a vector search to that candidate set, return a ranked evidence bundle. This is not a retrieval service that happens to have a few edges; the graph is the product.
- **§5.3 Temporal graph** requires **bitemporal** semantics on every entity and every edge: `valid_from` / `valid_to` (real-world time) plus `recorded_at` (ingestion time), with 90-day hot / 1-year warm / archive retention tiers and — eventually — graph overlays for Action Mode.

ADR-0004 explicitly said: "Revisit at v0.4+ if scale or query complexity genuinely needs it." That trigger is now reached. The Postgres-table approach no longer fits for three independent reasons:

1. **Query complexity.** GraphRAG as defined in §5.1 requires multi-hop, variable-depth traversals over mixed edge types with weighted path scoring. Recursive CTEs can express this, but they are hard to write, hard to read, hard to tune, and the plan stability story under load is weak. Every non-trivial retrieval change becomes a CTE rewrite.
2. **Temporal semantics.** Bitemporal queries in a flat relational schema require interval predicates on every join and every edge; correctness under retention tier transitions (hot → warm → archive, §5.3) is fragile. Graph-native property semantics on edges — `[:DEPLOYED_BY {valid_from, valid_to, recorded_at}]` — express this directly.
3. **Consumer model.** Callers are LLM-driven agents composing retrieval plans (ADR-0003). Cypher is a more natural target for agent-generated structural queries than recursive CTEs; the MCP surface (`get_related_entities`, `resolve_incident` in §5.5) maps cleanly onto graph primitives.

The scope of what has to move is stated in the epic ([#96](https://github.com/100rd/Omniscience/issues/96)): a multi-month refactor of the writer, retrieval layer, schema, tests, and deployment. Postgres is retained for **operational metadata only** — sources, ingestion runs, auth tokens, workspaces, locks. This ADR does not re-open ADR-0004's staged approach; it amends it at the storage layer for the specific product pivot.

This ADR covers the **graph** half of the hybrid. The vector half (Qdrant vs pgvector vs alternatives) is decided separately in ADR-0006 (issue [#102](https://github.com/100rd/Omniscience/issues/102)).

## Decision

**Neo4j Community Edition is the graph store for Omniscience** starting with v0.5 (the Neo4j + Qdrant migration wave).

- **Model**: labeled property graph.
- **Query language**: Cypher (openCypher-compatible).
- **Driver**: official `neo4j` Python driver, async API (`neo4j.AsyncGraphDatabase`).
- **Transactions**: ACID with read/write transaction separation; writes via explicit `session.execute_write`, reads via `session.execute_read`.
- **Edition**: **Community Edition** for v0.5. Enterprise-only features (multi-database, role-based access control, causal clustering, online backups) are not relied on in v0.5. Revisit when a demonstrated need arises (see Revisit triggers below).
- **Deployment shape (v0.5)**: single-node, container or Helm sub-chart, with PV-backed volumes for data and transaction logs.
- **Deployment shape (v0.6+)**: read replicas if read load warrants; clustering only on a measured trigger.

Postgres is retained exclusively for operational metadata: sources, ingestion runs, API tokens, workspaces, locks, schedules. No entity or edge data in Postgres after cutover ([#105](https://github.com/100rd/Omniscience/issues/105)).

### Why Neo4j specifically

| Criterion | Why Neo4j |
|---|---|
| Maturity | 15+ years of production use; largest graph-DB install base; stable semantics |
| Query language | Cypher is a declarative, well-documented standard (openCypher); agent-friendly |
| Driver quality | Official async Python driver, typed, reactive support, well-maintained |
| Operator maturity | Official [Neo4j Operator](https://neo4j.com/docs/operations-manual/current/kubernetes/) for Kubernetes plus community `neo4j/neo4j` Helm chart |
| Licensing | Community Edition is GPLv3 — acceptable for self-hosted use; Enterprise under commercial license when/if needed |
| Ecosystem | APOC procedures, GDS library (not relied on in v0.5 but available), broad tooling |
| Temporal support | Property-graph model makes bitemporal properties on relationships idiomatic; fits §5.3 directly |
| Observability | Prometheus endpoint, structured query logs, explain/profile for plan inspection |

### Schema posture (v0.5, non-normative)

The schema is fixed by [#103](https://github.com/100rd/Omniscience/issues/103) (GraphStore / VectorStore interfaces) and implemented by [#104](https://github.com/100rd/Omniscience/issues/104). This ADR fixes the idioms, not the shape.

- **Node labels per entity kind**: `(:Entity:K8sDeployment)`, `(:Entity:TerraformResource)`, `(:Entity:GitCommit)`. The `:Entity` super-label simplifies cross-kind queries and indexing.
- **Relationship types per edge kind**: `[:DEPLOYED_BY]`, `[:DEPENDS_ON]`, `[:CAUSED_BY]`, `[:CROSS_REF]`, `[:OWNS]`. Directed. Edge type vocabulary matches [`docs/schema.md`](../schema.md).
- **Required node properties**: `id` (stable), `workspace_id` (tenant boundary, **non-null**, see Consequences), `source_id`, `kind`, `canonical_name`, `provenance` (ingestion run id, strategy, confidence score per §5.2), `created_at`, `updated_at`.
- **Required edge properties**: `workspace_id`, `source_id`, `provenance`, `confidence` (0.0–1.0, per §5.2), `strategy` (e.g. `exact_address`, `arn_match`, `otel_trace`, `label_match`).
- **Bitemporal properties** (`valid_from`, `valid_to`, `recorded_at`) on every node and edge. The enforcement path (constraints, query rewriting, retention workers) is the subject of a follow-up ADR tied to epic [#97](https://github.com/100rd/Omniscience/issues/97).
- **Constraints**: `CREATE CONSTRAINT FOR (n:Entity) REQUIRE (n.workspace_id, n.id) IS UNIQUE` — composite uniqueness including `workspace_id` so the same source-level id across workspaces does not collide.
- **Indexes**: on `Entity(workspace_id, kind)`, `Entity(workspace_id, canonical_name)`, edge indexes on `workspace_id` and `source_id`.

Graph-algorithm workloads (centrality, community detection) are explicitly out of scope for v0.5.

### Deployment posture

- **Development**: a single Neo4j container in `docker-compose.yml`, published on `7687` (Bolt) and `7474` (HTTP for admin). Data volume mounted.
- **Helm**: add `neo4j/neo4j` as a sub-chart dependency in `helm/omniscience/Chart.yaml`, disabled by default so existing pgvector installs do not break mid-wave; enabled for v0.5 clusters. Persistent volume sized for v0.5 scale with room for 1-year warm retention (§5.3).
- **Backups**: `neo4j-admin backup` on Community Edition is offline — schedule nightly during the v0.5 pilot; revisit for online backup (Enterprise) before GA.
- **Authentication**: Neo4j auth enabled; credentials injected via Kubernetes Secret, not baked into values.
- **TLS**: Bolt TLS enabled in all non-dev environments.
- **Resource envelope (v0.5 target)**: 4 vCPU / 16 GiB RAM / 100 GiB SSD, with JVM heap tuned to ~50% of container memory per Neo4j guidance. Revisit on measured load.

### Migration path

Pairs with the migration tooling ([#108](https://github.com/100rd/Omniscience/issues/108)) and the cutover ticket ([#105](https://github.com/100rd/Omniscience/issues/105)).

1. **Scaffold** (issues [#103](https://github.com/100rd/Omniscience/issues/103), [#104](https://github.com/100rd/Omniscience/issues/104)): `GraphStore` interface plus Neo4j adapter; adapter covered by contract tests. Existing pgvector path untouched.
2. **Dual-write window** (via [#108](https://github.com/100rd/Omniscience/issues/108)): the writer publishes to both Postgres `entities`/`edges` and Neo4j. Reads still go to Postgres. Duration: at least one full ingestion cycle per active source type.
3. **Shadow reads**: retrieval layer ([#107](https://github.com/100rd/Omniscience/issues/107)) issues the Cypher equivalent alongside the recursive-CTE query, compares result sets on a fixed regression corpus, logs divergence. Gate to cutover is a divergence budget (fraction TBD in [#107](https://github.com/100rd/Omniscience/issues/107)).
4. **Cutover** ([#105](https://github.com/100rd/Omniscience/issues/105)): flip the read path to Neo4j, stop the Postgres dual-write, drop the `entities`/`edges` tables in a follow-up migration.
5. **Rollback**: during the dual-write + shadow-read window, flipping back to Postgres is safe. Post-cutover rollback requires re-hydration from Postgres backups taken immediately pre-cutover; retention of those backups is part of the [#105](https://github.com/100rd/Omniscience/issues/105) checklist.

Feature flag (`retrieval_graph_backend`) governs the read path during the window; default remains `postgres` until cutover.

## Alternatives rejected

### Apache AGE (keep Postgres, bolt Cypher on top)

Rejected now, though it was the explicit fallback in ADR-0004. Reasons it no longer fits:

- AGE is a Cypher layer over Postgres graph extensions. It is still Postgres underneath — the same recursive-CTE costs, the same plan-stability concerns, with an additional translation layer.
- Bitemporal edges in AGE are not first-class; the workaround is adding properties to the graph-edge JSON payload, which reintroduces the interval-predicate-on-every-join problem we are trying to leave behind.
- AGE is maintained by a smaller community; the Kubernetes / Helm story is less developed than Neo4j's.
- Using AGE would mean keeping a single bloated Postgres for metadata, graph, **and** (in the previous world) vectors — the opposite of the separation we're choosing at storage level.

AGE was a reasonable position when the graph was a supporting feature. It is not the right position when the graph is the product.

### Memgraph

Rejected. Memgraph is Cypher-compatible and performant on in-memory workloads, but:

- The real-time / streaming-analytics positioning is not our workload. Our writes are batch ingestion; our reads are complex multi-hop traversals with retrieval-layer composition — a workload Neo4j has been tuned against for a decade.
- Licensing has shifted repeatedly (BSL, MAGE licensed differently from core); the OSS / commercial split is less predictable than Neo4j's.
- Operator and Helm maturity lag Neo4j's by a wide margin.

### JanusGraph

Rejected. JanusGraph requires a backing store (Cassandra, HBase, or ScyllaDB) and an external index (Elasticsearch or Solr). That is three distributed systems to operate for one graph. The operational burden is unjustifiable for v0.5 scale and exceeds our SRE capacity.

### ArangoDB

Rejected. ArangoDB is multi-model (document, graph, key-value); the graph capability is credible but second-class relative to its document engine. AQL is a different surface from Cypher and smaller in community. If we wanted a multi-model store we would reopen ADR-0004 entirely, not land here.

### Dgraph

Rejected. GraphQL-native is appealing for application layers, but:

- We are not building a GraphQL API surface; we are building MCP tools backed by traversal + retrieval composition.
- Smaller community than Neo4j; the hiring pool for Dgraph operational experience is meaningfully thinner.
- A license change in 2021 (from Apache 2.0 to a more restrictive model, since relaxed) created churn that is still a real trust cost.

### FalkorDB (formerly RedisGraph)

Rejected. GraphBLAS-based execution is interesting and fast on certain patterns, but the project is young (post-RedisGraph-deprecation rebrand in 2024), the production footprint is small, Cypher coverage is partial, and the temporal-query story is immature. Not a foundation for a multi-year product.

### Stay on Postgres (recursive CTEs, no graph DB)

Rejected. This is the position ADR-0004 took and this ADR is amending. The explicit revisit trigger — scale and query complexity — has arrived together with the product pivot.

## Consequences

### Positive

- GraphRAG composition (§5.1) becomes expressible in the native query language of the store, not a compiled-down set of CTEs.
- Bitemporal edges (§5.3) land on a substrate that treats edges as first-class; the follow-up ADR tied to [#97](https://github.com/100rd/Omniscience/issues/97) has a far simpler job than it would on Postgres.
- MCP tools (`get_related_entities`, `resolve_incident`) map onto Cypher primitives; agent-generated queries have a cleaner target.
- Storage concerns are separated: Neo4j owns the graph, Qdrant (ADR-0006) owns vectors, Postgres owns operational metadata. Each component has a narrow, well-understood job.

### Negative — operational

- **JVM tuning** becomes a standing concern: heap size, GC policy, page cache tuning, and query-log review belong in the ops runbook.
- **Backups** on Community Edition are offline; nightly backup windows need scheduling, and RPO is worse than online-backup Enterprise deployments. Accepted for v0.5 pilot; revisit for GA.
- **Upgrade cadence**: Neo4j major versions are not trivially in-place upgrades. Version pinning and tested upgrade runbooks are required.
- **Disk footprint** grows with temporal retention; 1-year warm retention (§5.3) will be the dominant storage cost.

### Negative — team

- **Cypher fluency** is not currently on the team; onboarding cost for the adapter author and retrieval-layer rewriter is real. Mitigation: pair the adapter PR ([#104](https://github.com/100rd/Omniscience/issues/104)) with a short internal Cypher cheat-sheet in `docs/` and require Cypher review on retrieval changes for one release cycle.
- **Query patterns previously expressed as recursive CTEs** (graph traversal in `packages/retrieval`) must be rewritten. The retrieval-layer rewrite is [#107](https://github.com/100rd/Omniscience/issues/107); patterns and idioms will be documented there rather than here.

### Negative — security (pre-existing, carried forward)

- **Workspace (tenant) isolation must be enforced in Cypher.** The current `GraphQueryService.get_related` does not filter by `workspace_id` anywhere — a read-side ACL bypass. This defect predates the migration and must not be carried forward. The Neo4j adapter ([#104](https://github.com/100rd/Omniscience/issues/104)) MUST:
  1. Require `workspace_id` as a non-null property on every node and every edge (constraint above).
  2. Inject `WHERE n.workspace_id = $workspace_id AND r.workspace_id = $workspace_id` into every read path. No query function accepts a caller-supplied workspace; it is derived from the authenticated principal at the transport layer and threaded through as a transaction parameter.
  3. Composite-index `(workspace_id, kind)` and `(workspace_id, canonical_name)` so the predicate is index-backed.
  4. Add a linter or review rule that rejects raw Cypher strings in application code that omit the predicate; prefer a thin typed query-builder.

  A follow-up ticket tracks back-filling workspace filtering into the Postgres code path **before** the dual-write window starts, so both stores enforce the same boundary. Failing to do this turns the migration into a regression opportunity.

### Negative — cost

- Additional cluster component: Neo4j container / StatefulSet with persistent volume. v0.5 envelope (4 vCPU / 16 GiB / 100 GiB) is modest; the step-up cost appears when read-replica or clustering triggers fire later.

### Risks

- **Scope creep**: once Neo4j lands, pressure to use graph-algorithm libraries (GDS for centrality, community detection) will be real. Explicitly out of scope for v0.5. Any use of GDS is a separate ADR.
- **Licensing surprise**: Enterprise-only features (RBAC, causal clustering, online backups, fine-grained authorization) are attractive at GA. The cost must be budgeted before any adapter code assumes them. The v0.5 design explicitly does not depend on any of them.
- **Regression coverage**: the shadow-read phase requires a fixed retrieval regression corpus that does not exist yet in test form. Scoped under [#107](https://github.com/100rd/Omniscience/issues/107).

## Revisit triggers

- v0.5 pilot shows JVM / GC instability we cannot tune out — revisit store choice (unlikely but possible).
- GA approaches and RBAC / online backups become non-negotiable — evaluate Neo4j Enterprise vs a stay-on-Community posture with operational compensating controls.
- Graph-algorithm workloads (centrality, community detection) become a real product requirement — reopen GDS scope.
- Managed Neo4j (Aura) pricing and data-residency story meet self-hosted requirements — re-evaluate the deployment posture, not the store choice.

## Consequences for related docs

- [`docs/vision.md`](../vision.md) §5.1 already names Neo4j; no change required. Cross-link this ADR from §5.1.
- [`docs/architecture.md`](../architecture.md) storage section needs an update post-cutover ([#105](https://github.com/100rd/Omniscience/issues/105)).
- [`docs/schema.md`](../schema.md) entity / edge schema idioms reference this ADR for label and property conventions.
- A new `docs/decisions/README.md` (or index) should list ADRs 0001–0005 in order; create as part of this PR if absent.

## Links

- Parent epic: [#96](https://github.com/100rd/Omniscience/issues/96)
- This issue: [#100](https://github.com/100rd/Omniscience/issues/100)
- Depends on cutover: [#105](https://github.com/100rd/Omniscience/issues/105)
- Blocks: [#103](https://github.com/100rd/Omniscience/issues/103), [#104](https://github.com/100rd/Omniscience/issues/104)
- Pairs with: ADR-0006 (Qdrant, issue [#102](https://github.com/100rd/Omniscience/issues/102))
- Follow-up: bitemporal ADR tied to epic [#97](https://github.com/100rd/Omniscience/issues/97)
