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
# Bootstrap DDL
# ---------------------------------------------------------------------------

_BOOTSTRAP_STATEMENTS: Final[tuple[str, ...]] = (
    f"CREATE CONSTRAINT entity_workspace_id_unique IF NOT EXISTS "
    f"FOR (n:{_ENTITY_LABEL}) REQUIRE (n.workspace_id, n.id) IS UNIQUE",
    f"CREATE INDEX entity_workspace_kind IF NOT EXISTS "
    f"FOR (n:{_ENTITY_LABEL}) ON (n.workspace_id, n.kind)",
    f"CREATE INDEX entity_workspace_name IF NOT EXISTS "
    f"FOR (n:{_ENTITY_LABEL}) ON (n.workspace_id, n.name)",
    f"CREATE INDEX entity_source_id IF NOT EXISTS FOR (n:{_ENTITY_LABEL}) ON (n.source_id)",
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
)

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
    n.metadata = $metadata,
    n.is_stub = false,
    n.created_at = $now,
    n.updated_at = $now
ON MATCH SET
    n.source_id = $source_id,
    n.kind = $entity_type,
    n.name = $name,
    n.display_name = $display_name,
    n.chunk_id = $chunk_id,
    n.metadata = $metadata,
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

_UPSERT_EDGE_BY_NAME_CYPHER_TEMPLATE: Final[str] = f"""
MATCH (a:{_ENTITY_LABEL} {{workspace_id: $workspace_id, id: $source_id_ent}})
MERGE (b:{_ENTITY_LABEL} {{workspace_id: $workspace_id, name: $target_name}})
ON CREATE SET
    b.id = $generated_id,
    b.is_stub = true,
    b.created_at = $now
MERGE (a)-[r:`{{edge_type}}` {{workspace_id: $workspace_id}}]->(b)
ON CREATE SET
    r.source_id = $source_id,
    r.metadata = $metadata,
    r.edge_type = $edge_type,
    r.created_at = $now,
    r.updated_at = $now
"""

# ---------------------------------------------------------------------------
# Bitemporal write Cypher (ADR-0008 §2 + §3, issue #131)
# ---------------------------------------------------------------------------

_UPSERT_ENTITY_BITEMPORAL_CYPHER: Final[str] = f"""
// ADR-0008 §2 — property-versioned identity-node with [:HAD_STATE] chain.
// One MERGE transaction; either all sub-writes commit or none do.
MERGE (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id, id: $id}})
ON CREATE SET
    n.source_id = $source_id,
    n.kind = $entity_type,
    n.name = $name,
    n.display_name = $display_name,
    n.chunk_id = $chunk_id,
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
       n.recorded_at AS recorded_at
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
       s.recorded_at AS recorded_at
LIMIT 1
"""

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
    "        recorded_at: neighbour.recorded_at\n"
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
    "        recorded_at: n_state.recorded_at\n"
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

_RESOLVE_STUBS_CYPHER: Final[str] = f"""
MATCH (stub:{_ENTITY_LABEL} {{workspace_id: $workspace_id, is_stub: true}})
MATCH (real:{_ENTITY_LABEL} {{workspace_id: $workspace_id, name: stub.name}})
WHERE real <> stub AND (real.is_stub IS NULL OR real.is_stub = false)
WITH stub, real
LIMIT $batch_size
CALL {{
    WITH stub, real
    MATCH (caller)-[r]->(stub)
    WHERE r.workspace_id = $workspace_id
    CREATE (caller)-[r2:calls {{workspace_id: $workspace_id}}]->(real)
    SET r2 = properties(r), r2.edge_type = 'calls'
    DELETE r
    RETURN count(*) AS in_moved
}}
DETACH DELETE stub
RETURN count(DISTINCT stub) AS resolved
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
