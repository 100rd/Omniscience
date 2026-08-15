"""Cypher templates, DDL bootstrap statements, and import-time ACL guards.

All module-level string constants and the ``_ensure_workspace_predicate``
regression guard live here.  The guard calls at the bottom of this module
run at import time — importing any symbol from this module triggers the
full ACL assertion sweep.

Design rules (from ADR-0005, ADR-0008, ADR-0009, ADR-0012):
- Every read Cypher includes ``workspace_id`` — enforced by the guard below.
- No user-supplied values are interpolated into Cypher (only static, validated
  identifiers like clamped depth integers and allowlisted edge types).
- Bootstrap DDL is idempotent (``IF NOT EXISTS``).
"""

from __future__ import annotations

import re
from typing import Final

# ---------------------------------------------------------------------------
# Label / key constants
# ---------------------------------------------------------------------------

_MAX_DEPTH_CEILING: Final[int] = 6

_EDGE_TYPE_REGEX: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def _is_valid_edge_type(value: str) -> bool:
    """Return True iff ``value`` is safe to render into a Cypher template.

    Relationship type cannot be a Cypher parameter, so every rendered type
    passes through here first.  ``fullmatch``, never ``match``: Python's ``$``
    also matches immediately *before* a trailing newline, so
    ``_EDGE_TYPE_REGEX.match("calls\\n")`` succeeds.  That admitted an edge
    type carrying a newline, which forks the relationship type in Neo4j
    (``calls`` and ``calls\\n`` are two distinct types) and breaks the
    per-type index creation in ``_bootstrap_schema``.
    """
    return _EDGE_TYPE_REGEX.fullmatch(value) is not None


_ENTITY_LABEL: Final[str] = "Entity"
_REL_TYPE_KEY: Final[str] = "edge_type"

_WORKSPACE_PARAM: Final[str] = "workspace_id"
_WRITE_WORKSPACE_PARAM: Final[str] = "workspace_id"

_ENTITY_STATE_LABEL: Final[str] = "EntityState"

_ENTITY_SNAPSHOT_LABELS: Final[str] = "EntitySnapshot:Daily"
_RELATIONSHIP_SNAPSHOT_LABELS: Final[str] = "RelationshipSnapshot:Daily"

_ENTITY_STATE_FINGERPRINT_PROP: Final[str] = "state_fingerprint"
_EDGE_STATE_FINGERPRINT_PROP: Final[str] = "state_fingerprint"

# ---------------------------------------------------------------------------
# Checkpoint labels — two DISTINCT concepts, deliberately not one scalar
# ---------------------------------------------------------------------------
#
# ``:StoreCheckpoint {workspace_id, source_id}`` is a per-source **watermark**:
# the highest ``doc_version`` this store has applied for the source.  It is a
# convergence signal consumed by ``GlobalReconciler._get_neo4j_checkpoints``
# and ``scripts/dr_verify.py`` (documented invariant: Neo4j checkpoint ==
# Postgres ``max(doc_version)`` per source).  It MUST NOT gate a write.
#
# ``:DocumentCheckpoint {workspace_id, source_id, document_id}`` is the
# per-document **applied version**: the ``doc_version`` last written for that
# one document.  This is the stale-write guard.  ``documents.doc_version`` is
# a per-row counter, so a per-document store is the only place a ``>=``
# comparison against it is meaningful.
#
# Conflating the two is the defect this split repairs: every document of a
# source arrives with ``doc_version == 1``, so comparing it against a shared
# per-source scalar dropped every document after the first.
_STORE_CHECKPOINT_LABEL: Final[str] = "StoreCheckpoint"
_DOCUMENT_CHECKPOINT_LABEL: Final[str] = "DocumentCheckpoint"

# ---------------------------------------------------------------------------
# Bootstrap DDL
# ---------------------------------------------------------------------------

_STORE_CHECKPOINT_CONSTRAINT: Final[str] = (
    f"CREATE CONSTRAINT store_checkpoint_workspace_source_unique IF NOT EXISTS "
    f"FOR (c:{_STORE_CHECKPOINT_LABEL}) "
    f"REQUIRE (c.workspace_id, c.source_id) IS UNIQUE"
)

_DOCUMENT_CHECKPOINT_CONSTRAINT: Final[str] = (
    f"CREATE CONSTRAINT document_checkpoint_workspace_source_document_unique "
    f"IF NOT EXISTS FOR (d:{_DOCUMENT_CHECKPOINT_LABEL}) "
    f"REQUIRE (d.workspace_id, d.source_id, d.document_id) IS UNIQUE"
)

_BOOTSTRAP_STATEMENTS: Final[tuple[str, ...]] = (
    f"CREATE CONSTRAINT entity_workspace_id_unique IF NOT EXISTS "
    f"FOR (n:{_ENTITY_LABEL}) REQUIRE (n.workspace_id, n.id) IS UNIQUE",
    f"CREATE INDEX entity_workspace_kind IF NOT EXISTS "
    f"FOR (n:{_ENTITY_LABEL}) ON (n.workspace_id, n.kind)",
    f"CREATE INDEX entity_workspace_name IF NOT EXISTS "
    f"FOR (n:{_ENTITY_LABEL}) ON (n.workspace_id, n.name)",
    f"CREATE INDEX entity_source_id IF NOT EXISTS FOR (n:{_ENTITY_LABEL}) ON (n.source_id)",
    # Gap B (issue #310) — index the first-class `cluster` property promoted
    # from operator metadata so `list_entities` answers
    # "which <kind> exist in cluster X" with an index seek, never a
    # `metadata CONTAINS` substring scan.  workspace_id leads the composite
    # (ADR-0008 §Consequences-security #1 carry-forward).
    f"CREATE INDEX entity_workspace_kind_cluster IF NOT EXISTS "
    f"FOR (n:{_ENTITY_LABEL}) ON (n.workspace_id, n.kind, n.cluster)",
    f"CREATE INDEX entity_state_workspace_kind_cluster IF NOT EXISTS "
    f"FOR (s:{_ENTITY_STATE_LABEL}) ON (s.workspace_id, s.kind, s.cluster)",
    f"CREATE CONSTRAINT entity_state_workspace_id_valid_from_unique IF NOT EXISTS "
    f"FOR (s:{_ENTITY_STATE_LABEL}) "
    f"REQUIRE (s.workspace_id, s.id, s.valid_from) IS UNIQUE",
    f"CREATE INDEX entity_workspace_recorded_at IF NOT EXISTS "
    f"FOR (n:{_ENTITY_LABEL}) ON (n.workspace_id, n.recorded_at)",
    f"CREATE INDEX entity_state_workspace_valid_window IF NOT EXISTS "
    f"FOR (s:{_ENTITY_STATE_LABEL}) "
    f"ON (s.workspace_id, s.id, s.valid_from, s.valid_to)",
    f"CREATE INDEX entity_state_workspace_recorded_at IF NOT EXISTS "
    f"FOR (s:{_ENTITY_STATE_LABEL}) ON (s.workspace_id, s.recorded_at)",
    # Checkpoint uniqueness.  ``_CHECKPOINT_HEAL_STATEMENTS`` MUST have run
    # first — see the comment on that tuple.
    _STORE_CHECKPOINT_CONSTRAINT,
    _DOCUMENT_CHECKPOINT_CONSTRAINT,
)

#: The two bootstrap statements that can fail against *data* rather than
#: against a schema conflict: ``CREATE CONSTRAINT ... IF NOT EXISTS`` aborts
#: outright when the database still holds violating rows.  The heal above
#: normally removes them, but a heal that could not finish (timeout, heap
#: pressure on a large database) leaves them in place — and an exception here
#: propagates out of ``connect()`` and boot-loops the server.
#:
#: ``Neo4jGraphStore._bootstrap_schema`` therefore degrades a failure of these
#: two to a startup WARNING.  Running without the constraint is a *known*
#: degraded state the read path already tolerates: ``_read_checkpoint`` folds
#: the maximum over every duplicate row precisely because pre-constraint
#: databases exist (see its docstring).  Every other bootstrap statement stays
#: fatal.
_CHECKPOINT_CONSTRAINT_STATEMENTS: Final[frozenset[str]] = frozenset(
    {_STORE_CHECKPOINT_CONSTRAINT, _DOCUMENT_CHECKPOINT_CONSTRAINT}
)

# ---------------------------------------------------------------------------
# Pre-DDL data heal
# ---------------------------------------------------------------------------
#
# Deliberately NOT part of ``_BOOTSTRAP_STATEMENTS``: that tuple is pure,
# ``IF NOT EXISTS``-idempotent DDL and is linted as such.  This is a data
# repair, and it MUST run before the checkpoint constraint is created.
#
# ``CREATE CONSTRAINT ... IF NOT EXISTS`` does not tolerate pre-existing
# violations — it fails outright, which would abort ``connect()`` and take the
# server down at startup.  Databases written before the constraint existed do
# contain duplicates: without a uniqueness constraint ``MERGE`` takes no
# cross-transaction lock, so concurrent writers each created their own node.
#
# The heal is idempotent (a second run matches nothing) and strictly
# convergent: it keeps one node per (workspace_id, source_id) and removes the
# rest.  The nodes it deletes are derived bookkeeping, not source data: the
# authoritative version is Postgres ``documents.doc_version``, and
# ``scripts/rebuild_all_projections.py`` reconstructs checkpoints from it.
#
# BOUNDED.  ``$batch_size`` caps how many duplicate groups one transaction
# collapses.  The previous form was an unbounded ``MATCH (c:StoreCheckpoint)``
# — a full-label scan across every tenant, in one transaction, on every
# startup, before the DDL.  On the very database it exists to repair that can
# exhaust the heap or exceed the transaction timeout, and the failure aborted
# ``connect()``: the server then boot-looped with no way to skip the repair.
# ``Neo4jGraphStore._heal_checkpoints`` re-runs this until it reports zero and
# downgrades any failure to a startup warning.
#
# KEEPER SELECTION — the version must belong to the surviving epoch.
# The previous form reduced ``max(version)`` and ``max(epoch)`` independently,
# so {epoch: 5, version: 2} and {epoch: 3, version: 99} fabricated a watermark
# of {epoch: 5, version: 99} that no write had ever produced — and, being a
# high-water mark, that fabrication rejects every legitimate epoch-5 document
# below version 99.  Ordering by ``(epoch DESC, version DESC)`` and keeping
# ``head(nodes)`` makes the survivor a real observed pair.  ``elementId(c)``
# is the final tie-break so a run interrupted between batches resumes on the
# same keeper.  ``collect`` preserves the order established by the preceding
# ``ORDER BY``, so no ``SET`` is needed: the head already carries the right
# values.
_CHECKPOINT_HEAL_STATEMENTS: Final[tuple[str, ...]] = (
    f"MATCH (c:{_STORE_CHECKPOINT_LABEL})\n"
    f"WITH c ORDER BY c.workspace_id, c.source_id,"
    f" coalesce(c.epoch, -1) DESC, coalesce(c.version, -1) DESC, elementId(c)\n"
    f"WITH c.workspace_id AS workspace_id, c.source_id AS source_id,"
    f" collect(c) AS nodes\n"
    f"WHERE size(nodes) > 1\n"
    f"WITH nodes LIMIT $batch_size\n"
    f"WITH head(nodes) AS keep, tail(nodes) AS duplicates\n"
    f"FOREACH (d IN duplicates | DETACH DELETE d)\n"
    f"RETURN count(keep) AS collapsed",
)

# ---------------------------------------------------------------------------
# Duplicate-stub heal  ***DURABLE-DATA MUTATION — OPT-IN***
# ---------------------------------------------------------------------------
#
# Repairs a graph already forked by the by-name MERGE described at
# ``_UPSERT_EDGE_BY_NAME_CYPHER_TEMPLATE``.  Fixing the writer stops new
# duplicates; it cannot collapse the ones a previous build already wrote,
# because the fan-out is real persisted relationships.
#
# Unlike ``_CHECKPOINT_HEAL_STATEMENTS`` this is NOT unconditional.  That heal
# is mandatory — without it ``CREATE CONSTRAINT`` aborts ``connect()``.  This
# one has no such forcing function, it deletes `:Entity` nodes and rewrites
# relationships (graph data, not derived bookkeeping), and nothing here can
# reconstruct it from Postgres.  So it runs only when the operator sets
# ``Settings.graph_stub_heal`` / ``OMNISCIENCE_GRAPH_STUB_HEAL=enabled``, and
# the store logs before and after.
#
# Safety properties, in the order the statements must run:
#
#   * Convergent and idempotent.  A second run finds no group of size > 1 and
#     matches nothing.
#   * Keeper is deterministic: lowest ``(created_at, id)`` within
#     ``(workspace_id, name)``.  Every statement re-derives it identically, so
#     a run interrupted between statements resumes on the same keeper.
#   * Lossless for edges.  Relationships are relocated onto the keeper with
#     their properties before any node is deleted, and relocation is a MERGE,
#     so an edge the keeper already holds is not duplicated by the repair.
#   * Only ``is_stub = true`` nodes are ever deleted.  A real entity is never
#     a candidate, and a name held by exactly one node is never touched.
#
# Relationship type cannot be parameterised in Cypher, so the relocation
# statements carry a ``{rel_type}`` slot and the store renders them per type
# from ``db.relationshipTypes()`` — the same pattern already used for
# ``_EDGE_INDEX_TEMPLATES``.
_DUPLICATE_STUB_GROUP_PREFIX: Final[str] = f"""
MATCH (n:{_ENTITY_LABEL})
WHERE coalesce(n.is_stub, false) = true AND n.name IS NOT NULL
WITH n ORDER BY n.workspace_id, n.name, n.created_at, n.id
WITH n.workspace_id AS ws, n.name AS nm, collect(n) AS nodes
WHERE size(nodes) > 1
WITH head(nodes) AS keep, tail(nodes) AS dups
"""

_DUPLICATE_STUB_RELOCATION_TEMPLATES: Final[tuple[str, ...]] = (
    # Incoming edges: (caller)->(dup)  becomes  (caller)->(keep).
    _DUPLICATE_STUB_GROUP_PREFIX
    + """
UNWIND dups AS dup
MATCH (caller)-[r:`{rel_type}`]->(dup)
WHERE caller <> keep AND NOT caller IN dups
MERGE (caller)-[k:`{rel_type}`
                {workspace_id: coalesce(r.workspace_id, keep.workspace_id)}]->(keep)
ON CREATE SET k += properties(r)
DELETE r
""",
    # Outgoing edges: (dup)->(other)  becomes  (keep)->(other).
    _DUPLICATE_STUB_GROUP_PREFIX
    + """
UNWIND dups AS dup
MATCH (dup)-[r:`{rel_type}`]->(other)
WHERE other <> keep AND NOT other IN dups
MERGE (keep)-[k:`{rel_type}`
              {workspace_id: coalesce(r.workspace_id, keep.workspace_id)}]->(other)
ON CREATE SET k += properties(r)
DELETE r
""",
)

# Runs last, after every relationship type has been relocated.
#
# The doomed nodes are collected and counted BEFORE the delete: aggregating
# over a node in the same clause that removed it can raise "entity has been
# deleted in this transaction".  ``removed`` is the total across every group,
# and it is carried past the delete as a plain scalar.
_DUPLICATE_STUB_DELETE_CYPHER: Final[str] = (
    _DUPLICATE_STUB_GROUP_PREFIX
    + """
UNWIND dups AS dup
WITH collect(dup) AS to_delete
WITH to_delete, size(to_delete) AS removed
UNWIND to_delete AS doomed
DETACH DELETE doomed
RETURN removed
"""
)

_COUNT_DUPLICATE_STUBS_CYPHER: Final[str] = f"""
MATCH (n:{_ENTITY_LABEL})
WHERE coalesce(n.is_stub, false) = true AND n.name IS NOT NULL
WITH n.workspace_id AS ws, n.name AS nm, count(n) AS copies
WHERE copies > 1
RETURN count(*) AS forked_names, sum(copies - 1) AS redundant_nodes
"""

_EDGE_INDEX_TEMPLATES: Final[tuple[tuple[str, str], ...]] = (
    (
        "edge_workspace_id",
        "CREATE INDEX {name} IF NOT EXISTS FOR ()-[r:`{rel_type}`]-() ON (r.workspace_id)",
    ),
    (
        "edge_source_id",
        "CREATE INDEX {name} IF NOT EXISTS FOR ()-[r:`{rel_type}`]-() ON (r.source_id)",
    ),
    (
        "edge_workspace_valid_window",
        "CREATE INDEX {name} IF NOT EXISTS "
        "FOR ()-[r:`{rel_type}`]-() ON (r.workspace_id, r.valid_from, r.valid_to)",
    ),
    (
        "edge_workspace_recorded_at",
        "CREATE INDEX {name} IF NOT EXISTS "
        "FOR ()-[r:`{rel_type}`]-() ON (r.workspace_id, r.recorded_at)",
    ),
)

_LIST_RELATIONSHIP_TYPES_CYPHER: Final[str] = (
    "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType"
)

# ---------------------------------------------------------------------------
# Checkpoint queries
# ---------------------------------------------------------------------------
#
# The reads return every matching row rather than aggregating, and the caller
# folds the maximum in Python (``Neo4jGraphStore._read_checkpoint``).  That is
# deliberate: it survives the duplicate nodes that already exist in databases
# created before ``store_checkpoint_workspace_source_unique``, without relying
# on ``Result.single()``, which warns and returns an arbitrary row when the
# match is not unique.

_READ_SOURCE_CHECKPOINT_CYPHER: Final[str] = f"""
MATCH (c:{_STORE_CHECKPOINT_LABEL}
       {{workspace_id: $workspace_id, source_id: $source_id}})
RETURN c.version AS version, c.epoch AS epoch
"""

_READ_DOCUMENT_CHECKPOINT_CYPHER: Final[str] = f"""
MATCH (d:{_DOCUMENT_CHECKPOINT_LABEL}
       {{workspace_id: $workspace_id, source_id: $source_id,
         document_id: $document_id}})
RETURN d.version AS version, d.epoch AS epoch
"""

# Monotonic advance.  Within one SET clause list Cypher applies assignments in
# order and later expressions observe earlier ones, so ``version`` is computed
# FIRST while ``c.epoch`` still holds the previous epoch.
#
# A newer epoch (ADR-0017 per-source epoch pin / ADR-0018 rebuild) restarts the
# version sequence, so it replaces rather than maximises the watermark;
# otherwise the watermark only ever climbs.
_ADVANCE_SOURCE_CHECKPOINT_CYPHER: Final[str] = f"""
MERGE (c:{_STORE_CHECKPOINT_LABEL}
       {{workspace_id: $workspace_id, source_id: $source_id}})
ON CREATE SET
    c.version = $version,
    c.epoch = $epoch,
    c.created_at = datetime($now),
    c.updated_at = datetime($now)
ON MATCH SET
    c.version = CASE
        WHEN $epoch IS NOT NULL AND c.epoch IS NOT NULL AND $epoch > c.epoch
            THEN $version
        WHEN c.version IS NULL OR $version > c.version THEN $version
        ELSE c.version END,
    c.epoch = CASE
        WHEN c.epoch IS NULL THEN $epoch
        WHEN $epoch IS NULL OR $epoch <= c.epoch THEN c.epoch
        ELSE $epoch END,
    c.updated_at = datetime($now)
"""

_ADVANCE_DOCUMENT_CHECKPOINT_CYPHER: Final[str] = f"""
MERGE (d:{_DOCUMENT_CHECKPOINT_LABEL}
       {{workspace_id: $workspace_id, source_id: $source_id,
         document_id: $document_id}})
ON CREATE SET
    d.version = $version,
    d.epoch = $epoch,
    d.created_at = datetime($now),
    d.updated_at = datetime($now)
ON MATCH SET
    d.version = CASE
        WHEN $epoch IS NOT NULL AND d.epoch IS NOT NULL AND $epoch > d.epoch
            THEN $version
        WHEN d.version IS NULL OR $version > d.version THEN $version
        ELSE d.version END,
    d.epoch = CASE
        WHEN d.epoch IS NULL THEN $epoch
        WHEN $epoch IS NULL OR $epoch <= d.epoch THEN d.epoch
        ELSE $epoch END,
    d.updated_at = datetime($now)
"""

# ---------------------------------------------------------------------------
# Write queries
# ---------------------------------------------------------------------------

_UPSERT_ENTITY_CYPHER: Final[str] = f"""
MERGE (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id, id: $id}})
ON CREATE SET
    n.source_id = $source_id,
    n.kind = $entity_type,
    n.name = $name,
    n.display_name = $display_name,
    n.chunk_id = $chunk_id,
    n.cluster = $cluster,
    n.metadata = $metadata,
    n.content_hash = $content_hash,
    n.is_stub = false,
    n.created_at = $now,
    n.updated_at = $now
ON MATCH SET
    n.source_id = $source_id,
    n.kind = $entity_type,
    n.name = $name,
    n.display_name = $display_name,
    n.chunk_id = $chunk_id,
    n.cluster = $cluster,
    n.metadata = $metadata,
    n.content_hash = $content_hash,
    n.is_stub = false,
    n.updated_at = $now
"""

_UPSERT_EDGE_CYPHER_TEMPLATE: Final[str] = f"""
MATCH (a:{_ENTITY_LABEL} {{workspace_id: $workspace_id, id: $source_id_ent}})
MATCH (b:{_ENTITY_LABEL} {{workspace_id: $workspace_id, id: $target_id_ent}})
MERGE (a)-[r:`{{edge_type}}` {{workspace_id: $workspace_id}}]->(b)
ON CREATE SET
    r.source_id = $source_id,
    r.metadata = $metadata,
    r.edge_type = $edge_type,
    r.created_at = $now,
    r.updated_at = $now
ON MATCH SET
    r.source_id = $source_id,
    r.metadata = $metadata,
    r.updated_at = $now
"""

# ---------------------------------------------------------------------------
# By-name edge upsert — the edge-duplication fix
# ---------------------------------------------------------------------------
#
# WHY THIS IS NO LONGER `MERGE (b:Entity {workspace_id, name})`.
#
# The previous form MERGEd the stub on ``name``.  ``name`` has an *index*
# (``entity_workspace_name``) but no uniqueness *constraint*, and Neo4j only
# takes a cross-transaction lock for MERGE when the merged key is constrained.
# Two failures compounded:
#
#   1. Fork.  Ten refs ingested concurrently (``_DISCOVERY_CONCURRENCY``) each
#      found no stub and each created one, so ten `:Entity` nodes ended up
#      sharing a single name.  This is the identical failure mode the
#      checkpoint heal above documents, on a different key.
#   2. Fan-out — the expensive half.  MERGE binds *every* match, so once ten
#      stubs shared a name the pattern yielded ten rows and the relationship
#      MERGE below it ran ten times.  From then on **every** write of that edge
#      produced ten relationships, deterministically, on every replay.  One
#      inventory crawl produced 8541 `in_account` edges where 938 were due.
#
# Edges whose target resolves inside the batch never took this path: they go
# through `_UPSERT_EDGE_CYPHER_TEMPLATE`, which MATCHes both endpoints on the
# constrained ``(workspace_id, id)`` key, yields exactly one row and was
# idempotent all along.  That asymmetry is why `in_organization` / `in_ou`
# counts were exact while `in_account` / `management_account` were multiplied.
#
# The repair, in order:
#
#   * Resolve the target by name first, preferring a real (non-stub) entity and
#     breaking ties on ``id`` so concurrent writers select the *same* node.
#     `collect(...)[0]` collapses the match to one row, which removes the
#     fan-out even while duplicate stubs still exist in an un-healed database.
#   * Only when nothing carries that name, materialise the stub — MERGEd on
#     ``(workspace_id, id)`` with ``$stub_id`` derived deterministically from
#     ``(workspace_id, name)`` (``mappers._stub_entity_id``).  That key *is*
#     covered by ``entity_workspace_id_unique``, so concurrent creators
#     serialise on it and a replay reuses the node instead of forking one.
#
# `ON MATCH SET` converges content — without it a relationship froze at
# whatever the first write saw — but `updated_at` advances ONLY on a real
# change.  That asymmetry is deliberate and load-bearing: the bitemporal
# backfill derives `r.recorded_at = r.updated_at`
# (`_BACKFILL_EDGE_PROPS_CYPHER`), so touching `updated_at` on a no-op replay
# would restate "when we recorded this fact" as "when we last looked at it" —
# the same corruption the note on `_UPSERT_ENTITY_BITEMPORAL_CYPHER` refuses
# for entities.  The `CASE` is listed FIRST because Cypher applies SET items in
# order and later expressions observe earlier ones; assigning `r.metadata`
# before comparing it would make the predicate always false.
_UPSERT_EDGE_BY_NAME_CYPHER_TEMPLATE: Final[str] = f"""
MATCH (a:{_ENTITY_LABEL} {{workspace_id: $workspace_id, id: $source_id_ent}})
CALL {{
    OPTIONAL MATCH (m:{_ENTITY_LABEL}
                    {{workspace_id: $workspace_id, name: $target_name}})
    WITH m ORDER BY coalesce(m.is_stub, false), m.id
    RETURN collect(m.id)[0] AS existing_id
}}
WITH a, coalesce(existing_id, $stub_id) AS target_id
MERGE (b:{_ENTITY_LABEL} {{workspace_id: $workspace_id, id: target_id}})
ON CREATE SET
    b.name = $target_name,
    b.is_stub = true,
    b.created_at = $now
MERGE (a)-[r:`{{edge_type}}` {{workspace_id: $workspace_id}}]->(b)
ON CREATE SET
    r.source_id = $source_id,
    r.metadata = $metadata,
    r.edge_type = $edge_type,
    r.created_at = $now,
    r.updated_at = $now
ON MATCH SET
    r.updated_at = CASE
        WHEN coalesce(r.metadata, '') <> $metadata
          OR coalesce(r.source_id, '') <> $source_id
        THEN $now ELSE r.updated_at END,
    r.source_id = $source_id,
    r.metadata = $metadata
"""

# ---------------------------------------------------------------------------
# Bitemporal write Cypher (ADR-0008 §2 + §3, issue #131)
# ---------------------------------------------------------------------------

# NOTE — the deliberate absence of an unconditional `ON MATCH SET`.
#
# Unlike `_UPSERT_ENTITY_CYPHER` (legacy) and `_UPSERT_ENTITY_BITEMPORAL_CAS_CYPHER`
# (per-entity CAS), this template writes nothing when a re-ingested entity's
# state fingerprint is unchanged — not even `updated_at`.  That asymmetry is
# intentional and was re-examined alongside the checkpoint fix:
#
#   1. `updated_at` is load-bearing on the bitemporal axis.  The backfill
#      derives `n.recorded_at = n.updated_at` (`_BACKFILL_ENTITY_PROPS_CYPHER`).
#      Advancing `updated_at` on a no-op re-ingest would restate "when we
#      recorded this fact" as "when we last looked at it", corrupting exactly
#      the axis ADR-0008 exists to protect.
#   2. It would write nothing new.  The fingerprint covers kind, name,
#      display_name, source_id, chunk_id and metadata (`_entity_state_fingerprint`),
#      so an unchanged fingerprint means every property in the SET list is
#      already byte-identical.  On a full re-crawl that is a write per entity
#      per document for zero observable change.
#   3. Idempotent re-ingest is a required property, not an accident.  The
#      snapshot end-dating in `_run_upsert_graph` excludes batch entities for
#      the same reason; advancing timestamps here would make a replayed
#      snapshot produce a different graph than the original.
#
# "When did we last see this?" is a real operational question, but its answer
# belongs on the checkpoint nodes — which now carry `updated_at` per document
# and per source — not on every entity in the graph.
_UPSERT_ENTITY_BITEMPORAL_CYPHER: Final[str] = f"""
// ADR-0008 §2 — property-versioned identity-node with [:HAD_STATE] chain.
// One MERGE transaction; either all sub-writes commit or none do.
// No unconditional ON MATCH SET by design — see the note above this constant.
MERGE (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id, id: $id}})
ON CREATE SET
    n.source_id = $source_id,
    n.kind = $entity_type,
    n.name = $name,
    n.display_name = $display_name,
    n.chunk_id = $chunk_id,
    n.cluster = $cluster,
    n.metadata = $metadata,
    n.{_ENTITY_STATE_FINGERPRINT_PROP} = $state_fingerprint,
    n.created_at = $now,
    n.updated_at = $now,
    n.valid_from = datetime($now),
    n.valid_to = NULL,
    n.recorded_at = datetime($now)
WITH n,
     // True iff this MERGE matched an existing node whose fingerprint
     // disagrees with the incoming one.  On CREATE, fingerprint was just
     // set so the predicate is False and no extra version is produced.
     (
         coalesce(n.{_ENTITY_STATE_FINGERPRINT_PROP}, '') <> $state_fingerprint
         AND n.created_at <> $now
     ) AS state_changed
// End-date the previous still-open :EntityState (if any) when the state
// changed.  ADR-0008 §2: previous valid_to becomes the new valid_from.
FOREACH (_ IN CASE WHEN state_changed THEN [1] ELSE [] END |
    SET n.kind = $entity_type,
        n.source_id = $source_id,
        n.name = $name,
        n.display_name = $display_name,
        n.chunk_id = $chunk_id,
        n.cluster = $cluster,
        n.metadata = $metadata,
        n.{_ENTITY_STATE_FINGERPRINT_PROP} = $state_fingerprint,
        n.updated_at = $now,
        n.valid_from = datetime($now),
        n.valid_to = NULL,
        n.recorded_at = datetime($now)
)
WITH n, state_changed
// End-date the previous open `[:HAD_STATE]` and `(:EntityState)` together.
// `valid_to IS NULL` selects the still-current version; we close it to $now.
CALL {{
    WITH n, state_changed
    MATCH (n)-[h:HAD_STATE]->(s:{_ENTITY_STATE_LABEL})
    WHERE state_changed AND h.valid_to IS NULL AND s.valid_to IS NULL
    SET h.valid_to = datetime($now),
        s.valid_to = datetime($now)
    RETURN count(s) AS closed
}}
WITH n, state_changed, closed
// Insert the new :EntityState + [:HAD_STATE] iff (a) state changed OR
// (b) this is a brand-new identity (first write).  Brand-new identities
// have no incoming [:HAD_STATE] yet; we materialise the first version.
CALL {{
    WITH n, state_changed
    OPTIONAL MATCH (n)-[:HAD_STATE]->(existing:{_ENTITY_STATE_LABEL})
    WITH n, state_changed, count(existing) AS existing_count
    WITH n, (state_changed OR existing_count = 0) AS need_new_state
    FOREACH (_ IN CASE WHEN need_new_state THEN [1] ELSE [] END |
        CREATE (s2:{_ENTITY_STATE_LABEL} {{
            workspace_id: $workspace_id,
            id: $id,
            valid_from: datetime($now),
            valid_to: NULL,
            recorded_at: datetime($now),
            kind: $entity_type,
            source_id: $source_id,
            name: $name,
            display_name: $display_name,
            chunk_id: $chunk_id,
            cluster: $cluster,
            metadata: $metadata
        }})
        CREATE (n)-[:HAD_STATE {{
            workspace_id: $workspace_id,
            valid_from: datetime($now),
            valid_to: NULL,
            recorded_at: datetime($now)
        }}]->(s2)
    )
    RETURN count(*) AS _added
}}
RETURN n.id AS id, state_changed AS state_changed, closed AS closed_versions
"""


_UPSERT_EDGE_BITEMPORAL_CYPHER_TEMPLATE: Final[str] = f"""
// ADR-0008 §3 — edges remain identity-to-identity; bitemporal triple lives
// on the relationship.  Idempotent on no-change; end-dates the existing
// open relationship + creates a new one when the fingerprint differs.
MATCH (a:{_ENTITY_LABEL} {{workspace_id: $workspace_id, id: $source_id_ent}})
MATCH (b:{_ENTITY_LABEL} {{workspace_id: $workspace_id, id: $target_id_ent}})
// Find the still-current edge (valid_to IS NULL) of this type, if any.
OPTIONAL MATCH (a)-[r_open:`{{edge_type}}`]->(b)
WHERE r_open.workspace_id = $workspace_id AND r_open.valid_to IS NULL
WITH a, b, r_open,
     coalesce(r_open.{_EDGE_STATE_FINGERPRINT_PROP}, '') AS old_fp,
     (r_open IS NOT NULL) AS has_open
WITH a, b, r_open, has_open,
     // brand-new edge OR fingerprint differs => state changed
     (NOT has_open OR old_fp <> $state_fingerprint) AS state_changed
// End-date the previous open edge when state changed.
FOREACH (_ IN CASE WHEN state_changed AND has_open THEN [1] ELSE [] END |
    SET r_open.valid_to = datetime($now),
        r_open.updated_at = $now
)
WITH a, b, state_changed
// Create a new versioned edge iff state_changed.
FOREACH (_ IN CASE WHEN state_changed THEN [1] ELSE [] END |
    CREATE (a)-[:`{{edge_type}}` {{
        workspace_id: $workspace_id,
        source_id: $source_id,
        edge_type: $edge_type,
        metadata: $metadata,
        {_EDGE_STATE_FINGERPRINT_PROP}: $state_fingerprint,
        created_at: $now,
        updated_at: $now,
        valid_from: datetime($now),
        valid_to: NULL,
        recorded_at: datetime($now)
    }}]->(b)
)
RETURN state_changed AS state_changed
"""

_DELETE_BY_SOURCE_CYPHER: Final[str] = f"""
MATCH (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id, source_id: $source_id}})
DETACH DELETE n
"""

_DELETE_TOMBSTONED_CYPHER: Final[str] = f"""
MATCH (n:{_ENTITY_LABEL})
WHERE n.tombstoned_at IS NOT NULL
DETACH DELETE n
RETURN count(n) AS deleted
"""

# ---------------------------------------------------------------------------
# Tombstone end-dating (ADR-0008 §3, issue #137)
# ---------------------------------------------------------------------------

_END_DATE_BY_SOURCE_CYPHER: Final[str] = f"""
// ADR-0008 §3 + issue #137 — set valid_to on every still-open
// :Entity (and its open [:HAD_STATE] + :EntityState mirror) and every
// incident relationship for (workspace_id, source_id).  No DETACH DELETE.
//
// Phase A — end-date entity identity, open [:HAD_STATE], open :EntityState.
CALL {{
    MATCH (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id, source_id: $source_id}})
    WHERE n.valid_to IS NULL AND NOT n.id IN $batch_entity_ids
    OPTIONAL MATCH (n)-[h:HAD_STATE]->(s:{_ENTITY_STATE_LABEL})
    WHERE h.valid_to IS NULL AND s.valid_to IS NULL
    SET n.valid_to = datetime($now),
        n.updated_at = $now
    FOREACH (_ IN CASE WHEN h IS NOT NULL THEN [1] ELSE [] END |
        SET h.valid_to = datetime($now),
            s.valid_to = datetime($now)
    )
    RETURN count(DISTINCT n) AS entities_end_dated
}}
//
// Phase B — end-date every still-open relationship incident to a
// :Entity in (workspace_id, source_id).  Match anchored on the entity
// to keep the workspace_id predicate on the node side; then enforce
// workspace_id on the relationship for defence-in-depth (an edge with
// a foreign workspace_id stamped on it must NOT be touched here).
CALL {{
    MATCH (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id, source_id: $source_id}})
    OPTIONAL MATCH (n)-[r]-()
    WHERE r.workspace_id = $workspace_id AND r.valid_to IS NULL
    FOREACH (_ IN CASE WHEN r IS NOT NULL THEN [1] ELSE [] END |
        SET r.valid_to = datetime($now),
            r.updated_at = $now
    )
    RETURN count(DISTINCT r) AS edges_end_dated
}}
RETURN entities_end_dated, edges_end_dated
"""

_END_DATE_TOMBSTONED_CYPHER: Final[str] = f"""
// ADR-0008 §3 + issue #137 — end-date tombstoned entities (and their
// open [:HAD_STATE] + :EntityState mirror) instead of DETACH DELETE.
CALL {{
    MATCH (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id}})
    WHERE n.tombstoned_at IS NOT NULL AND n.valid_to IS NULL
    OPTIONAL MATCH (n)-[h:HAD_STATE]->(s:{_ENTITY_STATE_LABEL})
    WHERE h.valid_to IS NULL AND s.valid_to IS NULL
    SET n.valid_to = datetime($now),
        n.updated_at = $now
    FOREACH (_ IN CASE WHEN h IS NOT NULL THEN [1] ELSE [] END |
        SET h.valid_to = datetime($now),
            s.valid_to = datetime($now)
    )
    RETURN count(DISTINCT n) AS entities_end_dated
}}
CALL {{
    MATCH (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id}})
    WHERE n.tombstoned_at IS NOT NULL
    OPTIONAL MATCH (n)-[r]-()
    WHERE r.workspace_id = $workspace_id AND r.valid_to IS NULL
    FOREACH (_ IN CASE WHEN r IS NOT NULL THEN [1] ELSE [] END |
        SET r.valid_to = datetime($now),
            r.updated_at = $now
    )
    RETURN count(DISTINCT r) AS edges_end_dated
}}
RETURN entities_end_dated, edges_end_dated
"""

# ---------------------------------------------------------------------------
# Bitemporal backfill (ADR-0008 §8 phase 1, issue #130)
# ---------------------------------------------------------------------------

BACKFILL_DEFAULT_BATCH_SIZE: Final[int] = 500

_BACKFILL_ENTITY_PROPS_CYPHER: Final[str] = f"""
MATCH (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id}})
WHERE n.valid_from IS NULL
WITH n LIMIT $batch_size
SET n.valid_from = n.created_at,
    n.valid_to = NULL,
    n.recorded_at = n.updated_at
RETURN count(n) AS modified
"""

_BACKFILL_ENTITY_STATE_NODES_CYPHER: Final[str] = f"""
MATCH (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id}})
WHERE n.valid_from IS NOT NULL
  AND NOT (n)-[:HAD_STATE]->(:{_ENTITY_STATE_LABEL})
WITH n LIMIT $batch_size
MERGE (s:{_ENTITY_STATE_LABEL} {{
    workspace_id: n.workspace_id,
    id: n.id,
    valid_from: n.valid_from
}})
ON CREATE SET
    s.valid_to = n.valid_to,
    s.recorded_at = n.recorded_at,
    s.kind = n.kind,
    s.name = n.name,
    s.display_name = n.display_name,
    s.source_id = n.source_id,
    s.chunk_id = n.chunk_id,
    s.cluster = n.cluster,
    s.metadata = n.metadata
MERGE (n)-[:HAD_STATE]->(s)
RETURN count(s) AS modified
"""

_BACKFILL_EDGE_PROPS_CYPHER: Final[str] = """
MATCH ()-[r]->()
WHERE r.workspace_id = $workspace_id
  AND r.valid_from IS NULL
WITH r LIMIT $batch_size
SET r.valid_from = r.created_at,
    r.valid_to = NULL,
    r.recorded_at = r.updated_at
RETURN count(r) AS modified
"""

_BITEMPORAL_BACKFILL_STATEMENTS: Final[tuple[str, ...]] = (
    _BACKFILL_ENTITY_PROPS_CYPHER,
    _BACKFILL_ENTITY_STATE_NODES_CYPHER,
    _BACKFILL_EDGE_PROPS_CYPHER,
)

# ---------------------------------------------------------------------------
# Retention Cypher (ADR-0009 §2 / §3, issue #135)
# ---------------------------------------------------------------------------

_COUNT_HOT_TO_WARM_ENTITY_STATE_ELIGIBLE: Final[str] = f"""
MATCH (s:{_ENTITY_STATE_LABEL} {{workspace_id: $workspace_id}})
WHERE s.recorded_at < $hot_cutoff
  AND s.valid_to IS NOT NULL
RETURN count(s) AS eligible
"""

_COUNT_HOT_TO_WARM_EDGE_ELIGIBLE: Final[str] = """
MATCH ()-[r]->()
WHERE r.workspace_id = $workspace_id
  AND r.recorded_at < $hot_cutoff
  AND r.valid_to IS NOT NULL
RETURN count(r) AS eligible
"""

_SAMPLE_HOT_TO_WARM_ENTITY_STATE: Final[str] = f"""
MATCH (s:{_ENTITY_STATE_LABEL} {{workspace_id: $workspace_id}})
WHERE s.recorded_at < $hot_cutoff
  AND s.valid_to IS NOT NULL
RETURN s.id AS id, s.valid_from AS valid_from, s.recorded_at AS recorded_at
ORDER BY s.recorded_at ASC
LIMIT $limit
"""

_OLDEST_ELIGIBLE_HOT_RECORDED_AT: Final[str] = f"""
MATCH (s:{_ENTITY_STATE_LABEL} {{workspace_id: $workspace_id}})
WHERE s.recorded_at < $hot_cutoff
  AND s.valid_to IS NOT NULL
RETURN min(s.recorded_at) AS oldest
"""

_MARK_HOT_TO_WARM_ENTITY_STATE: Final[str] = f"""
MATCH (s:{_ENTITY_STATE_LABEL} {{workspace_id: $workspace_id}})
WHERE s.recorded_at < $hot_cutoff
  AND s.valid_to IS NOT NULL
  AND coalesce(s.tier_pending, '') <> 'warm'
WITH s LIMIT $batch_size
SET s.tier_pending = 'warm'
RETURN count(s) AS marked
"""

_MARK_HOT_TO_WARM_EDGE: Final[str] = """
MATCH ()-[r]->()
WHERE r.workspace_id = $workspace_id
  AND r.recorded_at < $hot_cutoff
  AND r.valid_to IS NOT NULL
  AND coalesce(r.tier_pending, '') <> 'warm'
WITH r LIMIT $batch_size
SET r.tier_pending = 'warm'
RETURN count(r) AS marked
"""

_MOVE_HOT_TO_WARM_ENTITY_STATE: Final[str] = f"""
MATCH (s:{_ENTITY_STATE_LABEL} {{workspace_id: $workspace_id}})
WHERE s.tier_pending = 'warm'
WITH s LIMIT $batch_size
WITH s, date(datetime(s.recorded_at)) AS snap_date
MERGE (snap:{_ENTITY_SNAPSHOT_LABELS} {{
    workspace_id: s.workspace_id,
    id: s.id,
    snapshot_date: snap_date
}})
ON CREATE SET
    snap.valid_from = s.valid_from,
    snap.valid_to = s.valid_to,
    snap.recorded_at = s.recorded_at,
    snap.kind = s.kind,
    snap.name = s.name,
    snap.display_name = s.display_name,
    snap.source_id = s.source_id,
    snap.chunk_id = s.chunk_id,
    snap.metadata = s.metadata
ON MATCH SET
    snap.valid_from =
        CASE WHEN s.valid_from > snap.valid_from THEN s.valid_from
             ELSE snap.valid_from END,
    snap.valid_to =
        CASE WHEN s.valid_from > snap.valid_from THEN s.valid_to
             ELSE snap.valid_to END,
    snap.recorded_at =
        CASE WHEN s.valid_from > snap.valid_from THEN s.recorded_at
             ELSE snap.recorded_at END,
    snap.kind =
        CASE WHEN s.valid_from > snap.valid_from THEN s.kind
             ELSE snap.kind END,
    snap.name =
        CASE WHEN s.valid_from > snap.valid_from THEN s.name
             ELSE snap.name END,
    snap.display_name =
        CASE WHEN s.valid_from > snap.valid_from THEN s.display_name
             ELSE snap.display_name END,
    snap.source_id =
        CASE WHEN s.valid_from > snap.valid_from THEN s.source_id
             ELSE snap.source_id END,
    snap.chunk_id =
        CASE WHEN s.valid_from > snap.valid_from THEN s.chunk_id
             ELSE snap.chunk_id END,
    snap.metadata =
        CASE WHEN s.valid_from > snap.valid_from THEN s.metadata
             ELSE snap.metadata END
DETACH DELETE s
RETURN count(snap) AS moved
"""

_MOVE_HOT_TO_WARM_EDGE: Final[str] = f"""
MATCH (a:{_ENTITY_LABEL})-[r]->(b:{_ENTITY_LABEL})
WHERE r.workspace_id = $workspace_id
  AND r.tier_pending = 'warm'
WITH a, r, b LIMIT $batch_size
WITH a, r, b, date(datetime(r.recorded_at)) AS snap_date
MERGE (snap:{_RELATIONSHIP_SNAPSHOT_LABELS} {{
    workspace_id: r.workspace_id,
    source_entity_id: a.id,
    target_entity_id: b.id,
    edge_type: r.edge_type,
    snapshot_date: snap_date
}})
ON CREATE SET
    snap.valid_from = r.valid_from,
    snap.valid_to = r.valid_to,
    snap.recorded_at = r.recorded_at,
    snap.source_id = r.source_id,
    snap.metadata = r.metadata
DELETE r
RETURN count(snap) AS moved
"""

_COUNT_WARM_TO_ARCHIVE_ELIGIBLE: Final[str] = f"""
MATCH (snap:{_ENTITY_SNAPSHOT_LABELS} {{workspace_id: $workspace_id}})
WHERE snap.snapshot_date < $warm_cutoff_date
RETURN count(snap) AS eligible
"""

_LIST_WARM_TO_ARCHIVE_DATES: Final[str] = f"""
MATCH (snap:{_ENTITY_SNAPSHOT_LABELS} {{workspace_id: $workspace_id}})
WHERE snap.snapshot_date < $warm_cutoff_date
RETURN DISTINCT snap.snapshot_date AS snapshot_date
ORDER BY snapshot_date ASC
LIMIT $limit
"""

_FETCH_WARM_ENTITY_SNAPSHOT_ROWS: Final[str] = f"""
MATCH (snap:{_ENTITY_SNAPSHOT_LABELS} {{
    workspace_id: $workspace_id,
    snapshot_date: $snapshot_date
}})
RETURN
    snap.id AS id,
    snap.kind AS kind,
    snap.name AS name,
    snap.display_name AS display_name,
    snap.source_id AS source_id,
    snap.chunk_id AS chunk_id,
    toString(snap.valid_from) AS valid_from,
    toString(snap.valid_to) AS valid_to,
    toString(snap.recorded_at) AS recorded_at,
    snap.metadata AS metadata
"""

_FETCH_WARM_RELATIONSHIP_SNAPSHOT_ROWS: Final[str] = f"""
MATCH (snap:{_RELATIONSHIP_SNAPSHOT_LABELS} {{
    workspace_id: $workspace_id,
    snapshot_date: $snapshot_date
}})
RETURN
    snap.source_entity_id AS source_entity_id,
    snap.target_entity_id AS target_entity_id,
    snap.edge_type AS edge_type,
    snap.source_id AS source_id,
    toString(snap.valid_from) AS valid_from,
    toString(snap.valid_to) AS valid_to,
    toString(snap.recorded_at) AS recorded_at,
    snap.metadata AS metadata
"""

_DELETE_WARM_ENTITY_SNAPSHOT: Final[str] = f"""
MATCH (snap:{_ENTITY_SNAPSHOT_LABELS} {{
    workspace_id: $workspace_id,
    snapshot_date: $snapshot_date
}})
DETACH DELETE snap
RETURN count(snap) AS deleted
"""

_DELETE_WARM_RELATIONSHIP_SNAPSHOT: Final[str] = f"""
MATCH (snap:{_RELATIONSHIP_SNAPSHOT_LABELS} {{
    workspace_id: $workspace_id,
    snapshot_date: $snapshot_date
}})
DELETE snap
RETURN count(snap) AS deleted
"""

_COUNT_HOT_ENTITY_STATES: Final[str] = f"""
MATCH (s:{_ENTITY_STATE_LABEL} {{workspace_id: $workspace_id}})
RETURN count(s) AS total
"""

_COUNT_WARM_ENTITY_SNAPSHOTS: Final[str] = f"""
MATCH (snap:{_ENTITY_SNAPSHOT_LABELS} {{workspace_id: $workspace_id}})
RETURN count(snap) AS total
"""

# ---------------------------------------------------------------------------
# Read queries
# ---------------------------------------------------------------------------

_GET_ENTITY_BY_NAME_CYPHER: Final[str] = f"""
MATCH (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id, name: $entity_name}})
RETURN n.id AS id,
       n.name AS name,
       n.kind AS kind,
       n.source_id AS source_id,
       n.chunk_text AS chunk_text,
       n.valid_from AS valid_from,
       n.valid_to AS valid_to,
       n.recorded_at AS recorded_at,
       n.metadata AS metadata
LIMIT 1
"""

_GET_ENTITY_BY_NAME_AS_OF_CYPHER: Final[str] = f"""
MATCH (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id, name: $entity_name}})
MATCH (n)-[:HAD_STATE]->(s:{_ENTITY_STATE_LABEL})
WHERE s.workspace_id = $workspace_id
  AND s.valid_from <= datetime($as_of)
  AND (datetime($as_of) < s.valid_to OR s.valid_to IS NULL)
RETURN n.id AS id,
       n.name AS name,
       s.kind AS kind,
       s.source_id AS source_id,
       n.chunk_text AS chunk_text,
       s.valid_from AS valid_from,
       s.valid_to AS valid_to,
       s.recorded_at AS recorded_at,
       s.metadata AS metadata
LIMIT 1
"""

# ---------------------------------------------------------------------------
# list_entities — kind + optional cluster/name filter (Gap B, issue #310)
# ---------------------------------------------------------------------------
#
# Two templates mirror the get_entity split (ADR-0008 §5): the current-state
# read hits the `:Entity` mirror via the (workspace_id, kind, cluster) index;
# the as_of read traverses `[:HAD_STATE]` to the `:EntityState` version valid
# at T.  Optional `cluster` / `name` predicates are appended by
# `_build_list_entities_cypher` ONLY when supplied so the planner keeps the
# index seek (an unconditional `$cluster IS NULL OR …` would force a scan).
#
# `__CLUSTER_FILTER__` / `__NAME_FILTER__` are static, validated placeholders
# (no user value is interpolated — the value rides as a $parameter); the
# builder substitutes either "" or the fixed predicate fragment.

_LIST_ENTITIES_CYPHER_TEMPLATE: Final[str] = f"""
MATCH (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id, kind: $kind}})
WHERE true__CLUSTER_FILTER____NAME_FILTER__
RETURN n.id AS id,
       n.name AS name,
       n.kind AS kind,
       n.source_id AS source_id,
       n.chunk_text AS chunk_text,
       n.valid_from AS valid_from,
       n.valid_to AS valid_to,
       n.recorded_at AS recorded_at,
       n.metadata AS metadata
ORDER BY name
"""

_LIST_ENTITIES_AS_OF_CYPHER_TEMPLATE: Final[str] = f"""
MATCH (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id}})
MATCH (n)-[:HAD_STATE]->(s:{_ENTITY_STATE_LABEL})
WHERE s.workspace_id = $workspace_id
  AND s.kind = $kind
  AND s.valid_from <= datetime($as_of)
  AND (datetime($as_of) < s.valid_to OR s.valid_to IS NULL)__CLUSTER_FILTER____NAME_FILTER__
RETURN n.id AS id,
       n.name AS name,
       s.kind AS kind,
       s.source_id AS source_id,
       n.chunk_text AS chunk_text,
       s.valid_from AS valid_from,
       s.valid_to AS valid_to,
       s.recorded_at AS recorded_at,
       s.metadata AS metadata
ORDER BY name
"""

# Fixed predicate fragments — current-state matches on the `:Entity` mirror,
# the as_of path matches on the `:EntityState` row (`s`).
_LIST_CLUSTER_FILTER: Final[str] = "\n  AND n.cluster = $cluster"
_LIST_NAME_FILTER: Final[str] = "\n  AND n.name = $name"
_LIST_AS_OF_CLUSTER_FILTER: Final[str] = "\n  AND s.cluster = $cluster"
_LIST_AS_OF_NAME_FILTER: Final[str] = "\n  AND n.name = $name"


_TRAVERSE_CYPHER_TEMPLATE: Final[str] = (
    "MATCH (seed:" + _ENTITY_LABEL + " {workspace_id: $workspace_id, name: $entity_name})\n"
    "OPTIONAL MATCH path = (seed)-[rels*1..__MAX_DEPTH__]-(neighbour:" + _ENTITY_LABEL + ")\n"
    "WHERE ALL(r IN rels WHERE r.workspace_id = $workspace_id__EDGE_TYPE_FILTER__)\n"
    "  AND neighbour.workspace_id = $workspace_id\n"
    "  AND neighbour <> seed\n"
    "WITH seed, neighbour, rels, length(path) AS depth\n"
    "RETURN\n"
    "    seed.id AS id,\n"
    "    seed.name AS seed_name,\n"
    "    seed.kind AS seed_kind,\n"
    "    seed.source_id AS seed_source_id,\n"
    "    seed.chunk_text AS seed_chunk_text,\n"
    "    seed.valid_from AS seed_valid_from,\n"
    "    seed.valid_to AS seed_valid_to,\n"
    "    seed.recorded_at AS seed_recorded_at,\n"
    "    seed.metadata AS seed_metadata,\n"
    "    collect(CASE WHEN neighbour IS NULL THEN NULL ELSE {\n"
    "        id: neighbour.id,\n"
    "        name: neighbour.name,\n"
    "        kind: neighbour.kind,\n"
    "        source_id: neighbour.source_id,\n"
    "        chunk_text: neighbour.chunk_text,\n"
    "        depth: depth,\n"
    "        edge_type: rels[-1].edge_type,\n"
    "        valid_from: neighbour.valid_from,\n"
    "        valid_to: neighbour.valid_to,\n"
    "        recorded_at: neighbour.recorded_at,\n"
    "        metadata: neighbour.metadata\n"
    "    } END) AS neighbours,\n"
    "    collect(CASE WHEN rels IS NULL THEN NULL ELSE\n"
    "        [startNode(last(rels)).name, endNode(last(rels)).name,"
    " last(rels).edge_type, last(rels).valid_from, last(rels).valid_to,"
    " last(rels).recorded_at]\n"
    "    END) AS edges\n"
)

_TRAVERSE_AS_OF_CYPHER_TEMPLATE: Final[str] = (
    "MATCH (seed:" + _ENTITY_LABEL + " {workspace_id: $workspace_id, name: $entity_name})\n"
    "MATCH (seed)-[:HAD_STATE]->(seed_state:" + _ENTITY_STATE_LABEL + ")\n"
    "WHERE seed_state.workspace_id = $workspace_id\n"
    "  AND seed_state.valid_from <= datetime($as_of)\n"
    "  AND (datetime($as_of) < seed_state.valid_to OR seed_state.valid_to IS NULL)\n"
    "OPTIONAL MATCH path = (seed)-[rels*1..__MAX_DEPTH__]-(neighbour:" + _ENTITY_LABEL + ")\n"
    "WHERE ALL(r IN rels WHERE r.workspace_id = $workspace_id\n"
    "      AND r.valid_from <= datetime($as_of)\n"
    "      AND (datetime($as_of) < r.valid_to OR r.valid_to IS NULL)"
    "__EDGE_TYPE_FILTER__)\n"
    "  AND neighbour.workspace_id = $workspace_id\n"
    "  AND neighbour <> seed\n"
    "  AND EXISTS {\n"
    "      MATCH (neighbour)-[:HAD_STATE]->(ns:" + _ENTITY_STATE_LABEL + ")\n"
    "      WHERE ns.workspace_id = $workspace_id\n"
    "        AND ns.valid_from <= datetime($as_of)\n"
    "        AND (datetime($as_of) < ns.valid_to OR ns.valid_to IS NULL)\n"
    "  }\n"
    "OPTIONAL MATCH (neighbour)-[:HAD_STATE]->(n_state:" + _ENTITY_STATE_LABEL + ")\n"
    "  WHERE n_state.workspace_id = $workspace_id\n"
    "    AND n_state.valid_from <= datetime($as_of)\n"
    "    AND (datetime($as_of) < n_state.valid_to OR n_state.valid_to IS NULL)\n"
    "WITH seed, seed_state, neighbour, n_state, rels, length(path) AS depth\n"
    "RETURN\n"
    "    seed.id AS id,\n"
    "    seed.name AS seed_name,\n"
    "    seed_state.kind AS seed_kind,\n"
    "    seed_state.source_id AS seed_source_id,\n"
    "    seed.chunk_text AS seed_chunk_text,\n"
    "    seed_state.valid_from AS seed_valid_from,\n"
    "    seed_state.valid_to AS seed_valid_to,\n"
    "    seed_state.recorded_at AS seed_recorded_at,\n"
    "    seed_state.metadata AS seed_metadata,\n"
    "    collect(CASE WHEN neighbour IS NULL THEN NULL ELSE {\n"
    "        id: neighbour.id,\n"
    "        name: neighbour.name,\n"
    "        kind: coalesce(n_state.kind, neighbour.kind),\n"
    "        source_id: coalesce(n_state.source_id, neighbour.source_id),\n"
    "        chunk_text: neighbour.chunk_text,\n"
    "        depth: depth,\n"
    "        edge_type: rels[-1].edge_type,\n"
    "        valid_from: n_state.valid_from,\n"
    "        valid_to: n_state.valid_to,\n"
    "        recorded_at: n_state.recorded_at,\n"
    "        metadata: coalesce(n_state.metadata, neighbour.metadata)\n"
    "    } END) AS neighbours,\n"
    "    collect(CASE WHEN rels IS NULL THEN NULL ELSE\n"
    "        [startNode(last(rels)).name, endNode(last(rels)).name,"
    " last(rels).edge_type, last(rels).valid_from, last(rels).valid_to,"
    " last(rels).recorded_at]\n"
    "    END) AS edges\n"
)

_SEED_ONLY_CYPHER: Final[str] = _GET_ENTITY_BY_NAME_CYPHER

# ---------------------------------------------------------------------------
# Stats queries (issue #111)
# ---------------------------------------------------------------------------

_COUNT_ENTITIES_CYPHER: Final[str] = f"""
MATCH (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id}})
RETURN count(n) AS total
"""

_COUNT_ENTITIES_BY_KIND_CYPHER: Final[str] = f"""
MATCH (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id}})
RETURN n.kind AS kind, count(n) AS total
ORDER BY kind
"""

_COUNT_ENTITIES_BY_SOURCE_CYPHER: Final[str] = f"""
MATCH (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id}})
RETURN n.source_id AS source_id, count(n) AS total
"""

_COUNT_EDGES_BY_TYPE_CYPHER: Final[str] = """
MATCH ()-[r]->()
WHERE r.workspace_id = $workspace_id
RETURN r.edge_type AS edge_type, count(r) AS total
ORDER BY edge_type
"""

# ---------------------------------------------------------------------------
# Stub-resolution Cypher (keep here with other write queries)
# ---------------------------------------------------------------------------

# WHY THIS IS NO LONGER ONE STATEMENT THAT CREATES `:calls`.
#
# The previous form relocated every relationship incident to a stub as::
#
#     CREATE (caller)-[r2:calls {workspace_id: $workspace_id}]->(real)
#     SET r2 = properties(r), r2.edge_type = 'calls'
#     DELETE r
#
# Relationship *type* cannot be parameterised in Cypher, and when this was
# written the only by-name edges in the graph were `calls` edges between code
# symbols, so the type was hard-coded.  The infrastructure extractor emits
# `depends_on`, `in_vpc`, `in_ou`, `in_organization` and `in_account` edges
# whose target is defined by a *later* document — exactly the shape that
# produces a stub.  Ordinary crawl order (instance document, then VPC
# document) therefore ran those edges through this statement and silently
# relabelled every one of them `calls`.  `EntityLinker.link_entities` calls
# `resolve_stubs` unconditionally and the pipeline's `_stage_link` swallows
# failures, so nothing surfaced.
#
# Three defects are repaired, in the order the statements must run:
#
#   1. Type preservation.  The relocation is rendered per relationship type —
#      the same pattern `_DUPLICATE_STUB_RELOCATION_TEMPLATES` and
#      `_EDGE_INDEX_TEMPLATES` already use — so `type(r)` and `r.edge_type`
#      both survive.  The store validates every type against
#      `_EDGE_TYPE_REGEX` first and refuses the whole pass if one fails, so
#      nothing unvalidated reaches the rendered Cypher.
#   2. Both directions.  The old subquery matched only `(caller)-[r]->(stub)`,
#      so a stub's OUTGOING edges were destroyed by the `DETACH DELETE` that
#      followed.  Incoming and outgoing are relocated separately.
#   3. One target, not N.  `MATCH (real {name: stub.name})` bound every node
#      carrying that name — `name` is indexed, not unique — re-creating the
#      fan-out the by-name edge fix removed.  The target is reduced to a
#      single node with the same `collect(...)[0]` + explicit-ordering
#      discipline used by `_UPSERT_EDGE_BY_NAME_CYPHER_TEMPLATE`.
#
# The delete refuses to touch a stub that still carries any relationship
# (`WHERE NOT (stub)--()`): deleting a node whose edges could not be relocated
# is the data loss this fix exists to prevent, and an un-deleted stub is
# merely an unresolved reference.  Every statement re-derives the batch with
# the same `ORDER BY stub.id LIMIT $batch_size`, so relocation and deletion
# operate on an identical stub set.
_RESOLVE_STUB_PAIR_PREFIX: Final[str] = f"""
MATCH (stub:{_ENTITY_LABEL} {{workspace_id: $workspace_id, is_stub: true}})
WHERE stub.name IS NOT NULL
CALL {{
    WITH stub
    OPTIONAL MATCH (candidate:{_ENTITY_LABEL}
                    {{workspace_id: $workspace_id, name: stub.name}})
    WHERE candidate <> stub AND coalesce(candidate.is_stub, false) = false
    WITH candidate ORDER BY candidate.id
    RETURN collect(candidate)[0] AS real
}}
WITH stub, real
WHERE real IS NOT NULL
WITH stub, real ORDER BY stub.id
LIMIT $batch_size
"""

# `caller <> real` / `other <> real` drop the edge that would become a
# self-loop on the surviving entity.  `_edge_to_params` already refuses to
# write a self-loop ("parity with pgvector writer"), so materialising one here
# would introduce an edge the writer itself would never produce.
_RESOLVE_STUB_RELOCATION_TEMPLATES: Final[tuple[str, ...]] = (
    # Incoming: (caller)->(stub)  becomes  (caller)->(real).
    _RESOLVE_STUB_PAIR_PREFIX
    + """
MATCH (caller)-[r:`{rel_type}`]->(stub)
WHERE r.workspace_id = $workspace_id AND caller <> real
MERGE (caller)-[k:`{rel_type}` {workspace_id: $workspace_id}]->(real)
ON CREATE SET k += properties(r)
DELETE r
""",
    # Outgoing: (stub)->(other)  becomes  (real)->(other).
    _RESOLVE_STUB_PAIR_PREFIX
    + """
MATCH (stub)-[r:`{rel_type}`]->(other)
WHERE r.workspace_id = $workspace_id AND other <> real
MERGE (real)-[k:`{rel_type}` {workspace_id: $workspace_id}]->(other)
ON CREATE SET k += properties(r)
DELETE r
""",
)

# Runs last, after every relationship type has been relocated.  The doomed
# nodes are collected and counted BEFORE the delete: aggregating over a node
# in the same clause that removed it can raise "entity has been deleted in
# this transaction" (same reason as `_DUPLICATE_STUB_DELETE_CYPHER`).
_RESOLVE_STUBS_DELETE_CYPHER: Final[str] = (
    _RESOLVE_STUB_PAIR_PREFIX
    + """
WITH stub
WHERE NOT (stub)--()
WITH collect(stub) AS doomed
WITH doomed, size(doomed) AS resolved
FOREACH (d IN doomed | DETACH DELETE d)
RETURN resolved
"""
)

# Every relationship type incident to a stub in this workspace.  Deliberately
# NOT filtered on ``r.workspace_id``: the fail-closed gate in the store must
# see every type it would have to render, including one it cannot.
_LIST_STUB_RELATIONSHIP_TYPES_CYPHER: Final[str] = f"""
MATCH (stub:{_ENTITY_LABEL} {{workspace_id: $workspace_id, is_stub: true}})
MATCH (stub)-[r]-()
RETURN DISTINCT type(r) AS rel_type
"""


# ---------------------------------------------------------------------------
# AP1 — Per-entity conditional-apply Cyphers (consilium-v9, ADR-0012 §AP1)
#
# Both variants MERGE on the stable identity key (workspace_id, id) and then
# apply a CASE-based CAS guard.  The guard writes only when:
#   - $forced is True  (admin forced-replay bypass), OR
#   - $incoming_version > coalesce(n.version, -1)  (strict monotonic advance)
#
# When version=None the store skips these Cyphers entirely (legacy/test path).
# The written n.version is read by get_entity_versions() which feeds the AP3
# min-watermark and the AP2 drift detector — must be monotonic.
#
# Returns: id (str), applied (bool)
#
# ── CAS ATOMICITY GUARANTEE (v11-AP4) ──────────────────────────────────────
# The read-compare-set (``coalesce(n.version, -1) → CASE → FOREACH SET``) is
# executed inside a SINGLE Cypher statement.  Neo4j MERGE acquires a per-node
# write lock on the matched (or newly created) node for the duration of the
# enclosing transaction, so no concurrent transaction can read or set
# ``n.version`` between the MERGE and the FOREACH SET.  This means the
# compare-and-set is atomic at the per-entity level:
#
#   - Concurrent backfill (bypass_source_checkpoint=True) and live upserts
#     for the SAME entity are serialised by Neo4j's node-level write lock.
#   - No lost update is possible: if two writers race on the same (workspace_id,
#     id), one acquires the lock first, updates n.version, and the other then
#     re-reads the updated n.version before evaluating its own CASE predicate.
#   - The final n.version is always the maximum of the applied writes.
#
# This is proven by the concurrency test in
# ``tests/integration/test_ap4_cas_concurrency.py`` (v11-AP4), which fires N
# parallel asyncio tasks writing the same entity at permuted version orders
# and asserts: (1) final version == max applied, (2) no lost update.
#
# Reference: Neo4j 5.x MERGE locking semantics — "MERGE acquires a write lock
# on the node or relationship being merged, preventing other transactions from
# concurrently creating or modifying it." (Neo4j Operations Manual §Locking).
# See also ADR-0017 §CAS Atomicity and ADR-0012 §AP1.
# ---------------------------------------------------------------------------

_UPSERT_ENTITY_CAS_CYPHER: Final[str] = f"""
MERGE (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id, id: $id}})
WITH n,
     CASE WHEN $forced THEN TRUE
          ELSE $incoming_version > coalesce(n.version, -1)
     END AS should_write
FOREACH (_ IN CASE WHEN should_write THEN [1] ELSE [] END |
    SET n.source_id    = $source_id,
        n.kind         = $entity_type,
        n.name         = $name,
        n.display_name = $display_name,
        n.chunk_id     = $chunk_id,
        n.cluster      = $cluster,
        n.metadata     = $metadata,
        n.content_hash = $content_hash,
        n.is_stub      = false,
        n.version      = $incoming_version,
        n.updated_at   = $now
)
RETURN n.id AS id, should_write AS applied
"""

_UPSERT_ENTITY_BITEMPORAL_CAS_CYPHER: Final[str] = f"""
// AP1 CAS + bitemporal triple (ADR-0008 §2 + ADR-0012 §AP1).
// MERGE ensures the identity node exists; the CAS predicate guards all writes.
MERGE (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id, id: $id}})
WITH n,
     CASE WHEN $forced THEN TRUE
          ELSE $incoming_version > coalesce(n.version, -1)
     END AS should_write
FOREACH (_ IN CASE WHEN should_write THEN [1] ELSE [] END |
    SET n.source_id                         = $source_id,
        n.kind                              = $entity_type,
        n.name                              = $name,
        n.display_name                      = $display_name,
        n.chunk_id                          = $chunk_id,
        n.cluster                           = $cluster,
        n.metadata                          = $metadata,
        n.content_hash                      = $content_hash,
        n.is_stub                           = false,
        n.version                           = $incoming_version,
        n.updated_at                        = $now,
        n.valid_from                        = datetime($now),
        n.valid_to                          = NULL,
        n.recorded_at                       = datetime($now),
        n.{_ENTITY_STATE_FINGERPRINT_PROP}  = $state_fingerprint
)
RETURN n.id AS id, should_write AS applied
"""

# ---------------------------------------------------------------------------
# Import-time regression guards
# ---------------------------------------------------------------------------


def _ensure_workspace_predicate(cypher: str, label: str) -> str:
    """Assert that ``cypher`` references the workspace_id predicate.

    Raises :class:`RuntimeError` at import time if a read template has
    been refactored to drop ``workspace_id``.  Write-only templates are
    exempted via the ``label`` argument.
    """
    if _WORKSPACE_PARAM not in cypher:
        raise RuntimeError(
            f"Neo4j Cypher template '{label}' is missing the "
            f"workspace_id predicate — refusing to load the adapter. "
            f"This is a hard regression guard for ACL isolation "
            f"(see ADR-0005 §Consequences and issue #117)."
        )
    return cypher


# Run the guard at module load.
_ensure_workspace_predicate(_UPSERT_ENTITY_CYPHER, "_UPSERT_ENTITY_CYPHER")
_ensure_workspace_predicate(_UPSERT_EDGE_CYPHER_TEMPLATE, "_UPSERT_EDGE_CYPHER_TEMPLATE")
_ensure_workspace_predicate(_DELETE_BY_SOURCE_CYPHER, "_DELETE_BY_SOURCE_CYPHER")
_ensure_workspace_predicate(_GET_ENTITY_BY_NAME_CYPHER, "_GET_ENTITY_BY_NAME_CYPHER")
_ensure_workspace_predicate(_TRAVERSE_CYPHER_TEMPLATE, "_TRAVERSE_CYPHER_TEMPLATE")
_ensure_workspace_predicate(_GET_ENTITY_BY_NAME_AS_OF_CYPHER, "_GET_ENTITY_BY_NAME_AS_OF_CYPHER")
_ensure_workspace_predicate(_TRAVERSE_AS_OF_CYPHER_TEMPLATE, "_TRAVERSE_AS_OF_CYPHER_TEMPLATE")
_ensure_workspace_predicate(_LIST_ENTITIES_CYPHER_TEMPLATE, "_LIST_ENTITIES_CYPHER_TEMPLATE")
_ensure_workspace_predicate(
    _LIST_ENTITIES_AS_OF_CYPHER_TEMPLATE, "_LIST_ENTITIES_AS_OF_CYPHER_TEMPLATE"
)
_ensure_workspace_predicate(_COUNT_ENTITIES_CYPHER, "_COUNT_ENTITIES_CYPHER")
_ensure_workspace_predicate(_COUNT_ENTITIES_BY_KIND_CYPHER, "_COUNT_ENTITIES_BY_KIND_CYPHER")
_ensure_workspace_predicate(_COUNT_ENTITIES_BY_SOURCE_CYPHER, "_COUNT_ENTITIES_BY_SOURCE_CYPHER")
_ensure_workspace_predicate(_COUNT_EDGES_BY_TYPE_CYPHER, "_COUNT_EDGES_BY_TYPE_CYPHER")
_ensure_workspace_predicate(_BACKFILL_ENTITY_PROPS_CYPHER, "_BACKFILL_ENTITY_PROPS_CYPHER")
_ensure_workspace_predicate(
    _BACKFILL_ENTITY_STATE_NODES_CYPHER, "_BACKFILL_ENTITY_STATE_NODES_CYPHER"
)
_ensure_workspace_predicate(_BACKFILL_EDGE_PROPS_CYPHER, "_BACKFILL_EDGE_PROPS_CYPHER")
_ensure_workspace_predicate(_UPSERT_ENTITY_BITEMPORAL_CYPHER, "_UPSERT_ENTITY_BITEMPORAL_CYPHER")
_ensure_workspace_predicate(
    _UPSERT_EDGE_BITEMPORAL_CYPHER_TEMPLATE, "_UPSERT_EDGE_BITEMPORAL_CYPHER_TEMPLATE"
)
_ensure_workspace_predicate(_END_DATE_BY_SOURCE_CYPHER, "_END_DATE_BY_SOURCE_CYPHER")
_ensure_workspace_predicate(_END_DATE_TOMBSTONED_CYPHER, "_END_DATE_TOMBSTONED_CYPHER")
_ensure_workspace_predicate(
    _COUNT_HOT_TO_WARM_ENTITY_STATE_ELIGIBLE,
    "_COUNT_HOT_TO_WARM_ENTITY_STATE_ELIGIBLE",
)
_ensure_workspace_predicate(_COUNT_HOT_TO_WARM_EDGE_ELIGIBLE, "_COUNT_HOT_TO_WARM_EDGE_ELIGIBLE")
_ensure_workspace_predicate(_SAMPLE_HOT_TO_WARM_ENTITY_STATE, "_SAMPLE_HOT_TO_WARM_ENTITY_STATE")
_ensure_workspace_predicate(_OLDEST_ELIGIBLE_HOT_RECORDED_AT, "_OLDEST_ELIGIBLE_HOT_RECORDED_AT")
_ensure_workspace_predicate(_MARK_HOT_TO_WARM_ENTITY_STATE, "_MARK_HOT_TO_WARM_ENTITY_STATE")
_ensure_workspace_predicate(_MARK_HOT_TO_WARM_EDGE, "_MARK_HOT_TO_WARM_EDGE")
_ensure_workspace_predicate(_MOVE_HOT_TO_WARM_ENTITY_STATE, "_MOVE_HOT_TO_WARM_ENTITY_STATE")
_ensure_workspace_predicate(_MOVE_HOT_TO_WARM_EDGE, "_MOVE_HOT_TO_WARM_EDGE")
_ensure_workspace_predicate(_COUNT_WARM_TO_ARCHIVE_ELIGIBLE, "_COUNT_WARM_TO_ARCHIVE_ELIGIBLE")
_ensure_workspace_predicate(_LIST_WARM_TO_ARCHIVE_DATES, "_LIST_WARM_TO_ARCHIVE_DATES")
_ensure_workspace_predicate(_FETCH_WARM_ENTITY_SNAPSHOT_ROWS, "_FETCH_WARM_ENTITY_SNAPSHOT_ROWS")
_ensure_workspace_predicate(
    _FETCH_WARM_RELATIONSHIP_SNAPSHOT_ROWS,
    "_FETCH_WARM_RELATIONSHIP_SNAPSHOT_ROWS",
)
_ensure_workspace_predicate(_DELETE_WARM_ENTITY_SNAPSHOT, "_DELETE_WARM_ENTITY_SNAPSHOT")
_ensure_workspace_predicate(
    _DELETE_WARM_RELATIONSHIP_SNAPSHOT, "_DELETE_WARM_RELATIONSHIP_SNAPSHOT"
)
_ensure_workspace_predicate(_COUNT_HOT_ENTITY_STATES, "_COUNT_HOT_ENTITY_STATES")
_ensure_workspace_predicate(_COUNT_WARM_ENTITY_SNAPSHOTS, "_COUNT_WARM_ENTITY_SNAPSHOTS")
_ensure_workspace_predicate(_RESOLVE_STUBS_DELETE_CYPHER, "_RESOLVE_STUBS_DELETE_CYPHER")
_ensure_workspace_predicate(
    _LIST_STUB_RELATIONSHIP_TYPES_CYPHER, "_LIST_STUB_RELATIONSHIP_TYPES_CYPHER"
)
for _index, _template in enumerate(_RESOLVE_STUB_RELOCATION_TEMPLATES):
    _ensure_workspace_predicate(_template, f"_RESOLVE_STUB_RELOCATION_TEMPLATES[{_index}]")
_ensure_workspace_predicate(_UPSERT_ENTITY_CAS_CYPHER, "_UPSERT_ENTITY_CAS_CYPHER")
_ensure_workspace_predicate(
    _UPSERT_ENTITY_BITEMPORAL_CAS_CYPHER, "_UPSERT_ENTITY_BITEMPORAL_CAS_CYPHER"
)
