# ADR-0024: Graph-write idempotency model — document checkpoints, deterministic stub identity, and edge-type safety

**Status**: Accepted
**Date**: 2026-08-15
**Deciders**: Platform Engineering
**Supersedes**: —
**Related**: ADR-0012 (Neo4j-native entity linking), ADR-0017 (per-source epoch pin), ADR-0018 (rebuild DR exception), ADR-0008 (bitemporal axis)

## Context

Ingesting the first real non-code corpus — an AWS inventory of 978 documents across
four Betterix accounts — surfaced three defects in the Neo4j graph-write path that a
code-only corpus had never exercised. Each produced a graph that looked healthy in the
logs while being wrong in the store.

1. **Source checkpoint gated document writes.** The write guard compared each document's
   own `doc_version` (a per-row counter, `1` for every document of a freshly crawled
   source) against a single per-source `:StoreCheckpoint`. The first document set the
   checkpoint to `1`; every later document then failed `existing >= incoming` and was
   dropped *before* the entity write, while the success log still reported the input
   count. Result: **963 of 978 documents silently discarded** — entities-per-source
   equalled the checkpoint version exactly. The same guard sat in the DR rebuild
   (`rebuild_all_projections.py`), so a restore produced one entity per source too.

2. **Unconstrained stub identity forked and fanned out.** The by-name edge write
   `MERGE (b:Entity {workspace_id, name})` used a key with an index but no uniqueness
   constraint. Neo4j takes no cross-transaction lock on an unconstrained `MERGE`, so with
   `_DISCOVERY_CONCURRENCY = 10` ten concurrent transactions each created their own stub
   for the same name (ten `arn:aws:iam::…:root` nodes in 4.1 s). Once N nodes shared a
   name, `MERGE` bound all N rows and the relationship `MERGE` beneath it ran N times —
   every subsequent edge write produced N relationships. Measured: **8898 relationships
   for 1080 distinct pairs.**

3. **Edge type reached Cypher text without a gate on one path.** Relationship types
   cannot be parameterised in Cypher, so they are rendered into templates. The by-name
   path rendered `edge_type` without the regex validation the keyed path applies, because
   `_edge_to_params` returned `None` for an unresolved target before validation ran. A
   connector-derived type is therefore a latent Cypher-injection surface.

## Decision

**1. Separate the two roles the checkpoint node was serving.**
`:DocumentCheckpoint {workspace_id, source_id, document_id}` guards writes — comparing a
document's version against the version last applied *for that document*, like against
like. `:StoreCheckpoint {workspace_id, source_id}` is a pure monotonic watermark
advanced to `max(existing, incoming)` and gates nothing. Epoch supersession (ADR-0017)
is preserved: a newer epoch replaces rather than maximises the version. Both nodes carry
composite uniqueness constraints; a bootstrap heal collapses any pre-existing duplicates,
batched and degrading to a startup warning rather than aborting `connect()`.

**2. Make stub identity deterministic and constrained.**
Stub ids are `uuid5(namespace, f"{workspace_id}|{name}")` and the by-name edge resolves
its target through a `CALL` subquery reduced to a single row (`collect(m.id)[0]`, real
node preferred over stub, `id` tiebreak), then `MERGE (b:Entity {workspace_id, id})` — a
**constrained** key. Neo4j serialises concurrent creators on the uniqueness constraint,
so a fork produces one node and a replay reuses it. Stub resolution
(`resolve_stubs` / duplicate-stub heal) preserves the original relationship type
per-type rather than rewriting every edge to `calls`.

**3. Gate every edge-type render.**
The regex validation is applied before every `.replace()` that renders an `edge_type`
into Cypher, on the by-name path as well as the keyed path, using `fullmatch` so a
trailing newline cannot slip a second edge type past it. An invalid type fails closed.

**4. Report persistence, not intent.**
`GraphWriteResult` counts rows the transaction actually wrote (from the driver's
`ResultSummary.counters`), never the input batch — the reporting failure that hid
defect 1 for a full ingestion run must not be re-introduced by the fix that names it.

## Consequences

- Bulk ingestion of any source with more than one document per crawl now persists every
  document. The AWS corpus goes from 15 entities to the full extraction.
- The DR rebuild is repaired by the same change.
- `resolve_stubs` remains a per-document operation; its type-preservation fix is a
  prerequisite for wiring `EntityLinker` into the ingestion pipeline (which this same
  change does for the first time).
- A residual instance of the per-source/per-document guard mismatch remains in
  `upsert_entity` / `upsert_edge` and is marked `# KNOWN GAP (tracked)` at both call
  sites; it is out of scope here because those paths carry their own per-entity CAS.
- The duplicate-stub heal is opt-in (`OMNISCIENCE_GRAPH_STUB_HEAL`) because it deletes
  non-reconstructible graph data; the checkpoint heal is mandatory but bounded and
  fail-soft because a uniqueness constraint cannot be created over violating data.

## Verification

The write path is exercised against a real Neo4j (testcontainers), not a fake
transaction. A barrier-synchronised reproducer forces the concurrency the naive
`asyncio.gather` never achieved: it fails on the reconstructed pre-fix code with the
exact tenfold fork factor and passes on the fix. Edge-type injection, heal boundedness,
epoch reset, and persisted-vs-submitted counts each carry a test that fails on the old
behaviour.
