# ADR-0013 — JSON-encoded `metadata` property for Neo4j entities and edges

- **Status**: Accepted
- **Date**: 2026-05-22
- **Deciders**: Backend Engineer
- **Issue**: [#226](https://github.com/100rd/Omniscience/issues/226)
- **Builds on**: [ADR-0005](0005-neo4j-as-graph-store.md) (Neo4j as the
  graph store), [ADR-0008](0008-bitemporal-schema-for-neo4j.md) (bitemporal
  schema — `metadata` is part of the per-version state per §2/§3).

## Context

Neo4j 5.x property values are restricted to **primitive types or arrays
of primitives**. A Cypher write like `SET n.metadata = $metadata` with
`$metadata` bound to a Python `dict` triggers
`Neo.ClientError.Statement.TypeError`:

> Property values can only be of primitive types or arrays thereof.
> Encountered: Map{}.

This rejected every `Neo4jGraphStore.upsert_entity` /
`Neo4jGraphStore.upsert_edge` call against a real Neo4j 5.x container
(empty metadata included — the driver still serialised `{}` as `Map{}`).
The bug shipped in PR #104 and stayed silent for a month because the
writer contract suite at `tests/test_store_contract.py` was gated
behind `OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1`, which CI never sets.
See issue #226 for the full incident.

ADR-0005 §Schema and ADR-0008 §2/§3 reference `metadata` as part of the
versioned state payload but do **not** prescribe an on-disk shape. No
Cypher template in `packages/index/src/omniscience_index/` indexes into
`metadata.<key>` (verified by
`grep -rn "metadata\." packages/index/src/omniscience_index/`), so the
property is opaque to query plans — we read the whole value at the
application boundary or not at all.

## Decision

`metadata` is persisted on `:Entity`, `:EntityState`, every relationship
type, and the corresponding warm snapshot labels as a **JSON-encoded
string**. Encoding is deterministic
(`json.dumps(obj, separators=(",",":"), sort_keys=True, default=str)`)
so the on-disk representation is stable across Python interpreter
versions and string equality on the property is meaningful.

A pair of helpers in `packages/index/src/omniscience_index/stores/neo4j_store.py`
keeps the encoding in one place:

- `_serialise_metadata(value: Any) -> str` — encode at write time.
- `_deserialise_metadata(raw: Any) -> dict[str, Any]` — decode at read
  time. Tolerates `str` (post-#226), `dict` (test fakes, pre-#226
  legacy rows), and `None`/missing.
- `_serialise_metadata_param(params: dict[str, Any]) -> None` —
  idempotent in-place rewrite invoked by every writer path.

Empty/None mapping is documented:
`_serialise_metadata(None) == _serialise_metadata({}) == "{}"` and
`_deserialise_metadata("") == _deserialise_metadata(None) == {}`.

### State fingerprint (ADR-0008 §2)

The bitemporal writer computes `state_fingerprint` over the in-memory
`metadata` dict **before** serialisation, so the fingerprint hash basis
is unchanged by this ADR. Fingerprints persisted on existing
`:Entity.state_fingerprint` / relationship `state_fingerprint`
properties before the fix remain comparable to fingerprints computed
after the fix — no spurious version bumps on the first post-fix upsert.

### Retention archive (ADR-0009 §7)

`apps/server/src/omniscience_server/retention_archive.py::_rows_to_table`
already serialises `metadata` to a JSON string column
(`metadata_json`) when building the parquet payload. It now accepts
both shapes for the source value (post-#226 `str`, pre-#226 `dict`)
and passes JSON strings through verbatim instead of double-encoding.

## Consequences

### Positive

- Writer works against any Neo4j 5.x version, including 5.19-community
  (the version pinned in `docker-compose.yml`).
- The single-helper-pair shape keeps the encoding contract auditable —
  one place to change if we ever switch to JSONB-on-Postgres semantics
  or a different encoder.
- Forward compatible: if a future Neo4j release loosens the property-
  value restriction we can revert to Map by changing the helpers
  without touching call sites.

### Negative / accepted

- Loss of server-side path queries into `metadata.<key>` — none exist
  today, and Cypher does not provide a `JSONExtract` operator, so a
  hypothetical future query of that shape would need a flattened
  property (e.g. `metadata_team`) rather than `metadata.team`. We will
  cross that bridge in a follow-up ADR if a use case materialises.
- A migration is required for any existing production graph that was
  written via the unit-test fake (which accepts dicts) and then read
  from a real Neo4j container — none exist today because the writer
  has been broken against real Neo4j for a month, but a one-shot
  `MATCH (n:Entity) WHERE NOT n.metadata STARTS WITH '{' SET n.metadata = '{}'`
  is the rescue script if someone seeded a dev DB via a different path.

### Test gate

The parametric writer contract suite at
`tests/test_store_contract.py` no longer gates on
`OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1`. The skip condition is now
"testcontainers-neo4j package missing OR Docker daemon unreachable",
matching the pattern from `tests/integration/test_neo4j_bootstrap_schema.py`
(PR #227). A targeted regression test
`tests/integration/test_neo4j_writer_metadata.py` exercises the
upsert → read-back round trip on real Neo4j 5.19-community and is the
fence against the bug recurring.

## Alternatives considered

1. **Flatten metadata into prefixed scalar properties** (e.g.
   `metadata_team = "payments"`). Rejected — the metadata schema is
   open-ended (connectors emit arbitrary keys) and a flat namespace
   would conflict with the bitemporal triple (`valid_from`, `valid_to`,
   `recorded_at`) and the `state_fingerprint` property. Would also
   require schema migrations whenever a connector adds a new key.
2. **Use Neo4j's `apoc.convert.toJson` for the encoding inside Cypher**.
   Rejected — APOC is an optional plugin and `docker-compose.yml`
   ships the community image without it. Adds a deploy-time dependency
   for no functional gain over Python-side `json.dumps`.
3. **Store `metadata` on a sibling `(:EntityMetadata)` node linked from
   the identity**. Rejected — adds a write per upsert, doubles the
   read-side surface for what is currently a single property read, and
   complicates the bitemporal version chain (per ADR-0008 §2 the state
   payload is one node, not a fan-out).
