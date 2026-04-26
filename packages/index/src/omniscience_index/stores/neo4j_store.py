"""Neo4j-backed adapter for the ``GraphStore`` protocol (issue #104).

Implements ``omniscience_core.storage.GraphStore`` against the official
``neo4j`` Python driver (async variant), per ADR-0005
(``docs/decisions/0005-neo4j-as-graph-store.md``).

Design rules
------------

1.  **Workspace invariant.**  Every read Cypher statement in this module
    includes ``{workspace_id: $workspace_id}`` on its pattern or an
    explicit ``WHERE`` predicate on ``workspace_id``.  The helper
    :func:`_ensure_workspace_predicate` is applied to every Cypher
    template at import time so a refactor that drops the predicate is
    caught before the driver is ever opened.  The contract tests in
    ``tests/test_neo4j_store.py`` cover the runtime side.

2.  **ACID split.**  Writes go through ``session.execute_write``; reads
    through ``session.execute_read``.  This matches ADR-0005 §Decision.

3.  **Parameterized queries only.**  No Cypher is ever built by string
    interpolation of user-supplied values.  The only string pieces we
    concatenate are the bootstrap DDL and a *static* allowlisted edge
    type list in :meth:`find_related` — user-supplied edge types are
    matched against a regex before they reach Cypher.

4.  **Idempotent MERGE.**  Every write uses
    ``MERGE ... ON CREATE SET ... ON MATCH SET ...`` so re-ingestion is
    safe.

5.  **No magic numbers.**  Pool size, timeouts, and max traversal depth
    live on :class:`Neo4jStoreConfig` and are populated from
    :class:`omniscience_core.config.Settings`.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final

import structlog
from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncManagedTransaction
from omniscience_core.storage.graph import (
    EdgeUpsert,
    EntityNodeView,
    EntityUpsert,
    GraphEdgeView,
    GraphResultView,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Neo4jStoreConfig:
    """Runtime configuration for :class:`Neo4jGraphStore`.

    All values are injected from :class:`omniscience_core.config.Settings`.
    No defaults reach production — the caller must provide a populated
    config.  A convenience :meth:`from_settings` factory is provided for
    the application startup path.
    """

    uri: str
    username: str
    password: str
    database: str
    max_connection_pool_size: int
    connection_acquisition_timeout_seconds: float
    max_transaction_retry_time_seconds: float
    default_max_depth: int
    # ADR-0008 §8 phase 2 — bitemporal write-path rollout flag.  Read once
    # at adapter `__init__` and pinned onto the instance per ADR-0008's
    # "consistency over flexibility" rollout rule (issue #131).  ``False``
    # is the default that preserves PR #104's writer behaviour byte-for-byte.
    bitemporal_enabled: bool = False

    @classmethod
    def from_settings(cls, settings: Any) -> Neo4jStoreConfig:
        """Build a config from the canonical ``Settings`` object.

        Typed as ``Any`` because importing ``Settings`` here would create
        an import cycle (``omniscience_core`` -> ``omniscience_index``).
        The duck-typed attribute access matches the exact field names on
        the Pydantic settings class.
        """
        return cls(
            uri=str(settings.neo4j_uri),
            username=str(settings.neo4j_username),
            password=str(settings.neo4j_password),
            database=str(settings.neo4j_database),
            max_connection_pool_size=int(settings.neo4j_max_pool_size),
            connection_acquisition_timeout_seconds=float(
                settings.neo4j_acquisition_timeout_seconds
            ),
            max_transaction_retry_time_seconds=float(settings.neo4j_max_retry_time_seconds),
            default_max_depth=int(settings.neo4j_default_max_depth),
            # ADR-0008 §8 — gates the writer changes from issue #131 only.
            # Default 'disabled' means: writer behaves exactly as PR #104.
            bitemporal_enabled=(str(settings.graph_bitemporal) == "enabled"),
        )


# ---------------------------------------------------------------------------
# Constants & Cypher templates
# ---------------------------------------------------------------------------

# Hard clamp for the BFS depth — protects the Neo4j planner from
# user-provided huge depths while still being well above realistic
# GraphRAG traversals.
_MAX_DEPTH_CEILING: Final[int] = 6

# Allowlist regex for user-supplied edge types.  We never interpolate raw
# user input into Cypher without validating it first.  The regex is
# deliberately narrow: match the edge type vocabulary in ``docs/schema.md``.
_EDGE_TYPE_REGEX: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

# Node super-label and relationship "kind" property names.
_ENTITY_LABEL: Final[str] = "Entity"
_REL_TYPE_KEY: Final[str] = "edge_type"

# The canonical workspace predicate.  Used both as a runtime parameter
# key and as the substring the import-time regression guard looks for.
_WORKSPACE_PARAM: Final[str] = "workspace_id"

# Parameter name for the non-authoritative "actor" tenant_id in writes.
# Intentionally identical to the read-path predicate to keep the mental
# model single-threaded.
_WRITE_WORKSPACE_PARAM: Final[str] = "workspace_id"


# --- Bootstrap DDL ---------------------------------------------------------

# Label name for per-version `(:EntityState)` snapshots — see ADR-0008 §2.
# Identity nodes are still `:Entity`; each version is reachable from its
# identity via `[:HAD_STATE]`, with the still-current version mirrored on
# the identity node so current-state reads stay one MERGE.
_ENTITY_STATE_LABEL: Final[str] = "EntityState"

# Discriminator labels for warm-tier snapshot rows (ADR-0009 §1 / §7).
# Snapshot rows live in the same Neo4j database as hot but carry these
# discriminator labels so the hot-path indexes (composite on `:Entity`
# / `:EntityState`) never see them.  Performance isolation is index-
# backed, not query-rewrite-only.
_ENTITY_SNAPSHOT_LABELS: Final[str] = "EntitySnapshot:Daily"
_RELATIONSHIP_SNAPSHOT_LABELS: Final[str] = "RelationshipSnapshot:Daily"

_BOOTSTRAP_STATEMENTS: Final[tuple[str, ...]] = (
    # --- ADR-0005 carry-forward — unchanged. -------------------------------
    # Composite uniqueness including workspace_id — same source id across
    # workspaces must not collide.  (ADR-0005 §Schema.)
    f"CREATE CONSTRAINT entity_workspace_id_unique IF NOT EXISTS "
    f"FOR (n:{_ENTITY_LABEL}) REQUIRE (n.workspace_id, n.id) IS UNIQUE",
    # Index-backed workspace predicates so the ACL filter is cheap.
    f"CREATE INDEX entity_workspace_kind IF NOT EXISTS "
    f"FOR (n:{_ENTITY_LABEL}) ON (n.workspace_id, n.kind)",
    f"CREATE INDEX entity_workspace_name IF NOT EXISTS "
    f"FOR (n:{_ENTITY_LABEL}) ON (n.workspace_id, n.name)",
    f"CREATE INDEX entity_source_id IF NOT EXISTS FOR (n:{_ENTITY_LABEL}) ON (n.source_id)",
    # Edges: workspace_id is property on the relationship too.  Neo4j 5.x
    # supports relationship property indexes.
    "CREATE INDEX edge_workspace_id IF NOT EXISTS FOR ()-[r]-() ON (r.workspace_id)",
    "CREATE INDEX edge_source_id IF NOT EXISTS FOR ()-[r]-() ON (r.source_id)",
    # --- ADR-0008 §4 — bitemporal schema (issue #130). ---------------------
    # Each statement below is copied verbatim from ADR-0008 §4.  Every
    # index is composite on `workspace_id` first per ADR-0008
    # §Consequences-security #1 and the ACL carry-forward from #117/#119.
    # `IF NOT EXISTS` keeps bootstrap idempotent — re-runs are no-ops.
    #
    # New EntityState uniqueness — one row per (workspace_id, id, valid_from).
    # Per ADR-0008 §4: same valid_from twice for the same identity is corruption
    # (a writer race); recorded_at is NOT in the uniqueness key because
    # monotonicity (§1) makes it redundant for distinguishing rows.
    f"CREATE CONSTRAINT entity_state_workspace_id_valid_from_unique IF NOT EXISTS "
    f"FOR (s:{_ENTITY_STATE_LABEL}) "
    f"REQUIRE (s.workspace_id, s.id, s.valid_from) IS UNIQUE",
    # Hot-path identity by ingestion freshness; used by the dominant
    # "current state" read narrowing and by the retention worker for
    # hot -> warm eviction (ADR-0009 §2).
    f"CREATE INDEX entity_workspace_recorded_at IF NOT EXISTS "
    f"FOR (n:{_ENTITY_LABEL}) ON (n.workspace_id, n.recorded_at)",
    # Validity-window seek for as_of reads.  Composite ordering matters
    # per ADR-0008 §4: planner narrows by (workspace_id, id) first, then
    # ranges over (valid_from, valid_to) — the shape §5's predicate emits.
    f"CREATE INDEX entity_state_workspace_valid_window IF NOT EXISTS "
    f"FOR (s:{_ENTITY_STATE_LABEL}) "
    f"ON (s.workspace_id, s.id, s.valid_from, s.valid_to)",
    # Retention-side EntityState lookup by ingestion freshness — used by
    # the retention worker (ADR-0009).
    f"CREATE INDEX entity_state_workspace_recorded_at IF NOT EXISTS "
    f"FOR (s:{_ENTITY_STATE_LABEL}) ON (s.workspace_id, s.recorded_at)",
    # Edge bitemporal seek (relationship property index, Neo4j 5.x).
    "CREATE INDEX edge_workspace_valid_window IF NOT EXISTS "
    "FOR ()-[r]-() ON (r.workspace_id, r.valid_from, r.valid_to)",
    "CREATE INDEX edge_workspace_recorded_at IF NOT EXISTS "
    "FOR ()-[r]-() ON (r.workspace_id, r.recorded_at)",
)


# --- Write queries ---------------------------------------------------------

_UPSERT_ENTITY_CYPHER: Final[str] = f"""
MERGE (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id, id: $id}})
ON CREATE SET
    n.source_id = $source_id,
    n.kind = $entity_type,
    n.name = $name,
    n.display_name = $display_name,
    n.chunk_id = $chunk_id,
    n.metadata = $metadata,
    n.created_at = $now,
    n.updated_at = $now
ON MATCH SET
    n.source_id = $source_id,
    n.kind = $entity_type,
    n.name = $name,
    n.display_name = $display_name,
    n.chunk_id = $chunk_id,
    n.metadata = $metadata,
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


# --- Bitemporal write Cypher (ADR-0008 §2 + §3, issue #131) ---------------
#
# These templates are activated when ``Neo4jStoreConfig.bitemporal_enabled``
# is True (which mirrors ``Settings.graph_bitemporal == "enabled"``).  When
# the flag is False, the legacy ``_UPSERT_ENTITY_CYPHER`` and
# ``_UPSERT_EDGE_CYPHER_TEMPLATE`` above are used unchanged from PR #104 —
# this is the "no regressions" contract from ADR-0008 §8 phase 2.
#
# Identity model: property-versioned identity-node + ``[:HAD_STATE]`` to
# per-version ``(:EntityState)`` nodes (ADR-0008 §2).  The ``:Entity`` node
# carries the **current** state mirror; each version is a ``:EntityState``
# with its own validity window.
#
# State-change detection uses a Python-computed ``$state_fingerprint``
# string — see ``_entity_state_fingerprint`` and ``_edge_state_fingerprint``.
# Computing the fingerprint client-side (rather than concatenating in
# Cypher) keeps the comparison deterministic for NULLs, list ordering, and
# nested dicts, and gives us a single ``coalesce(n.state_fingerprint, "")
# <> $state_fingerprint`` predicate to drive the conditional version bump.
# That predicate is the no-op short-circuit per ADR-0008 §2 last paragraph
# ("an upsert that does not change state is a no-op on the version chain").
#
# Atomicity (ADR-0008 §Negative-team #2): the previous-state end-date and
# the new-state insert MUST land in the same transaction.  Cypher's
# ``FOREACH(_ IN CASE WHEN cond THEN [1] ELSE [] END | ...)`` pattern is
# the canonical idiom for conditional sub-writes inside a single MERGE
# transaction — every write below sits inside the same ``tx.run`` call so
# either all writes commit or none do.

# Writer fingerprint property name on `:Entity`.  Not bitemporal itself —
# it is operational metadata used solely for change detection.  Stored on
# the identity node (mirror) so the comparison is one property read.
_ENTITY_STATE_FINGERPRINT_PROP: Final[str] = "state_fingerprint"
_EDGE_STATE_FINGERPRINT_PROP: Final[str] = "state_fingerprint"


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

# Batch-replace entities+edges for a single source.  Mirrors the
# pgvector ``IndexWriter.upsert_graph`` semantics: deleting by
# source_id purges both entities and their incident edges via
# DETACH DELETE.
_DELETE_BY_SOURCE_CYPHER: Final[str] = f"""
MATCH (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id, source_id: $source_id}})
DETACH DELETE n
"""

# delete_tombstoned: entities marked tombstoned by the ingestion layer.
# Mirrors the pgvector no-op contract but is a real operation here —
# Neo4j has no cascade from a Postgres source row, so proactive cleanup
# is required when a document is tombstoned.
_DELETE_TOMBSTONED_CYPHER: Final[str] = f"""
MATCH (n:{_ENTITY_LABEL})
WHERE n.tombstoned_at IS NOT NULL
DETACH DELETE n
RETURN count(n) AS deleted
"""


# --- Bitemporal backfill (ADR-0008 §8 phase 1, issue #130) ---------------
#
# These statements are kept SEPARATE from `_BOOTSTRAP_STATEMENTS` so the
# `connect()` startup path stays sub-200ms on a 1M-node graph.  They are
# operator-driven (invoked from a one-shot CLI), never run from the
# FastAPI lifespan.  See `Neo4jGraphStore.backfill_bitemporal`.
#
# The migration is idempotent: every SET is guarded by `WHERE n.valid_from
# IS NULL` per ADR-0008 §8, so re-runs are no-ops on rows already migrated.
# Every MATCH is workspace-scoped — the caller pins `$workspace_id` so
# workspace A's backfill never touches workspace B (ADR-0008 §Consequences-
# security #1, ACL carry-forward from #117/#119).

# Default batch size for the chunked backfill loop.  ADR-0008 §Risks names
# "chunked, resumable, never a `MATCH (n) SET ...` that locks the entire
# graph" as a non-negotiable; 500 keeps individual transactions well under
# Neo4j's default 30s tx timeout on the v0.5 envelope while amortising
# session overhead.  Raise via `batch_size=` kwarg if a wider window is
# desired during a maintenance window.
BACKFILL_DEFAULT_BATCH_SIZE: Final[int] = 500

# Phase 1a — populate the bitemporal triple on existing identity `:Entity`
# nodes that pre-date the schema.  Maps `created_at`/`updated_at` (the
# pre-bitemporal property names from `_UPSERT_ENTITY_CYPHER`) onto the new
# `valid_from`/`recorded_at` per ADR-0008 §8 phase 1.  `valid_to` is set
# to NULL — the still-valid sentinel from §1.  Returns the number of rows
# touched so the caller can loop until zero (resumable per §8).
_BACKFILL_ENTITY_PROPS_CYPHER: Final[str] = f"""
MATCH (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id}})
WHERE n.valid_from IS NULL
WITH n LIMIT $batch_size
SET n.valid_from = n.created_at,
    n.valid_to = NULL,
    n.recorded_at = n.updated_at
RETURN count(n) AS modified
"""

# Phase 1b — materialize the initial `(:EntityState)` row for every
# identity that does not yet have one, and link it via `[:HAD_STATE]`
# (ADR-0008 §2, §8).  The MERGE on `(:EntityState {workspace_id, id,
# valid_from})` is keyed by the new uniqueness constraint, so re-running
# this query is a no-op once the chain exists for an identity.
#
# Runs only on identities that already carry the bitemporal triple
# (Phase 1a populates that triple), so the order of phases is fixed:
# props first, state nodes second.
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

# Phase 1c — populate the bitemporal triple on existing relationships.
# Edges remain identity-to-identity per ADR-0008 §3; only the relationship
# properties carry the new triple.  Mirrors phase 1a's idempotence guard.
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

# Group the templates so the import-time guard treats them as a unit and
# anyone touching them sees the §8 contract together.  Tuple ordering is
# the execution order: props -> state nodes -> edges.  The state-nodes
# pass depends on phase 1a, so re-ordering breaks idempotence.
_BITEMPORAL_BACKFILL_STATEMENTS: Final[tuple[str, ...]] = (
    _BACKFILL_ENTITY_PROPS_CYPHER,
    _BACKFILL_ENTITY_STATE_NODES_CYPHER,
    _BACKFILL_EDGE_PROPS_CYPHER,
)


# --- Retention Cypher (ADR-0009 §2 / §3, issue #135) --------------------
#
# Per-version eviction (ADR-0009 §2): a node identity whose latest version
# is hot but whose history extends into warm has its older versions
# evicted to warm while the latest stays hot.  The eligibility predicate
# selects `:EntityState` rows whose `recorded_at < $hot_cutoff` AND which
# are NOT the still-current version (`valid_to IS NOT NULL`).  This
# preserves the identity-stability invariant from ADR-0008 §9 #4: the
# `valid_to IS NULL` row for an identity never moves to warm.
#
# Edge end-dating does NOT trigger eviction (ADR-0009 §2): a relationship
# with `valid_to` set in the past stays hot as long as its `recorded_at`
# is within the hot window.  The eligibility predicate is on
# `recorded_at`, never on `valid_to`.
#
# Every Cypher template below is composite on `workspace_id` (ADR-0009
# §Consequences-security #1, ACL carry-forward from ADR-0005/#117/#119).
# The retention worker iterates per-workspace; cross-tenant batches are
# forbidden.

# Phase 1 (read): count rows eligible for hot-to-warm eviction.  Used by
# the dry-run reporter and the live worker to size batches.  No mutation.
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

# Phase 1 (read): sample eligible rows for the dry-run report.  Bounded
# limit; `id` and `recorded_at` only — no full row payload.
_SAMPLE_HOT_TO_WARM_ENTITY_STATE: Final[str] = f"""
MATCH (s:{_ENTITY_STATE_LABEL} {{workspace_id: $workspace_id}})
WHERE s.recorded_at < $hot_cutoff
  AND s.valid_to IS NOT NULL
RETURN s.id AS id, s.valid_from AS valid_from, s.recorded_at AS recorded_at
ORDER BY s.recorded_at ASC
LIMIT $limit
"""

# Phase 1 (read): oldest eligible `recorded_at` for the lag SLO.
# Returns NULL when nothing is overdue.
_OLDEST_ELIGIBLE_HOT_RECORDED_AT: Final[str] = f"""
MATCH (s:{_ENTITY_STATE_LABEL} {{workspace_id: $workspace_id}})
WHERE s.recorded_at < $hot_cutoff
  AND s.valid_to IS NOT NULL
RETURN min(s.recorded_at) AS oldest
"""

# Phase 2 (mark): tag eligible :EntityState rows with `tier_pending = 'warm'`
# in batches.  Idempotent — already-marked rows are skipped.  The marker
# is the resumability anchor per ADR-0009 §3: a crash between mark and
# move leaves rows in a re-runnable state.
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

# Phase 3 (move): project marked :EntityState rows to :EntitySnapshot:Daily
# rows under the snapshot-per-day projection (ADR-0009 §1 warm tier).
# `snapshot_date` is `date(recorded_at)` — UTC calendar day.  The MERGE
# on (workspace_id, id, snapshot_date) makes this idempotent so multiple
# eligible versions on the same day collapse into one snapshot row
# (the as-of-end-of-day projection is implemented by ordering on
# `valid_from` DESC and keeping the latest version for each (id, day)).
#
# Returns the count of (workspace_id, id, snapshot_date) triples
# materialized.  After the snapshot row is in place the source
# :EntityState row is deleted in the same transaction — we do not leave
# a half-evicted state where the row exists in both tiers.
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

# Phase 3 (move): project marked relationships to :RelationshipSnapshot:Daily.
# Edges remain identity-to-identity; the snapshot carries the bitemporal
# triple at the day boundary.  Post-snapshot the source relationship is
# deleted.  The MERGE key is
# (workspace_id, source_id, target_id, edge_type, snapshot_date) —
# multiple eligible versions on the same day collapse identically.
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

# Phase 1 (read): count rows eligible for warm-to-archive transition.
# `:EntitySnapshot:Daily` rows are eligible when their `snapshot_date`
# is older than `now - warm_days` (ADR-0009 §1 archive boundary).
_COUNT_WARM_TO_ARCHIVE_ELIGIBLE: Final[str] = f"""
MATCH (snap:{_ENTITY_SNAPSHOT_LABELS} {{workspace_id: $workspace_id}})
WHERE snap.snapshot_date < $warm_cutoff_date
RETURN count(snap) AS eligible
"""

# Phase 1 (read): list distinct snapshot dates eligible for archive,
# bounded.  The archive worker writes one parquet object per
# (workspace_id, snapshot_date), so the unit of work is the date.
_LIST_WARM_TO_ARCHIVE_DATES: Final[str] = f"""
MATCH (snap:{_ENTITY_SNAPSHOT_LABELS} {{workspace_id: $workspace_id}})
WHERE snap.snapshot_date < $warm_cutoff_date
RETURN DISTINCT snap.snapshot_date AS snapshot_date
ORDER BY snapshot_date ASC
LIMIT $limit
"""

# Phase 1 (read): fetch all entity-snapshot rows for one (workspace_id,
# snapshot_date), used by the parquet writer.  Returns the full payload
# so the writer can serialise without a second round-trip.
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

# Phase 1 (read): fetch all relationship-snapshot rows for one
# (workspace_id, snapshot_date).
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

# Phase 3 (move): hard-delete warm rows for one (workspace_id, snapshot_date)
# AFTER the parquet write succeeded.  Keep entity and relationship
# snapshot deletes in separate templates so a relationship delete does
# not block on a missing entity snapshot.
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

# Stats: count records by tier for the metrics gauge.
_COUNT_HOT_ENTITY_STATES: Final[str] = f"""
MATCH (s:{_ENTITY_STATE_LABEL} {{workspace_id: $workspace_id}})
RETURN count(s) AS total
"""

_COUNT_WARM_ENTITY_SNAPSHOTS: Final[str] = f"""
MATCH (snap:{_ENTITY_SNAPSHOT_LABELS} {{workspace_id: $workspace_id}})
RETURN count(snap) AS total
"""


# --- Read queries ----------------------------------------------------------

_GET_ENTITY_BY_NAME_CYPHER: Final[str] = f"""
MATCH (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id, name: $entity_name}})
RETURN n.name AS name,
       n.kind AS kind,
       n.source_id AS source_id,
       n.chunk_text AS chunk_text
LIMIT 1
"""

# Traversal with optional edge-type filter.  Parametrising the max_depth
# requires string interpolation because Cypher's variable-length pattern
# syntax does not accept parameters for the depth.  We clamp the value
# before interpolation via ``_clamp_depth`` and reject anything
# non-integer upstream.
_TRAVERSE_CYPHER_TEMPLATE: Final[str] = (
    "MATCH (seed:" + _ENTITY_LABEL + " {workspace_id: $workspace_id, name: $entity_name})\n"
    "OPTIONAL MATCH path = (seed)-[rels*1..__MAX_DEPTH__]-(neighbour:" + _ENTITY_LABEL + ")\n"
    "WHERE ALL(r IN rels WHERE r.workspace_id = $workspace_id__EDGE_TYPE_FILTER__)\n"
    "  AND neighbour.workspace_id = $workspace_id\n"
    "  AND neighbour <> seed\n"
    "WITH seed, neighbour, rels, length(path) AS depth\n"
    "RETURN\n"
    "    seed.name AS seed_name,\n"
    "    seed.kind AS seed_kind,\n"
    "    seed.source_id AS seed_source_id,\n"
    "    seed.chunk_text AS seed_chunk_text,\n"
    "    collect(CASE WHEN neighbour IS NULL THEN NULL ELSE {\n"
    "        name: neighbour.name,\n"
    "        kind: neighbour.kind,\n"
    "        source_id: neighbour.source_id,\n"
    "        chunk_text: neighbour.chunk_text,\n"
    "        depth: depth,\n"
    "        edge_type: rels[-1].edge_type\n"
    "    } END) AS neighbours,\n"
    "    collect(CASE WHEN rels IS NULL THEN NULL ELSE\n"
    "        [startNode(last(rels)).name, endNode(last(rels)).name,"
    " last(rels).edge_type]\n"
    "    END) AS edges\n"
)

# Just the seed — used when find_related runs with depth 0 behaviour
# (entity exists, no neighbours).
_SEED_ONLY_CYPHER: Final[str] = _GET_ENTITY_BY_NAME_CYPHER

# --- Stats queries (issue #111) -------------------------------------------
#
# Each statement carries the ``workspace_id`` predicate so the import-time
# regression guard accepts the template.  The Cypher is index-backed by
# ``entity_workspace_kind``, ``entity_workspace_name`` (entities) and
# ``edge_workspace_id`` (edges) created in the bootstrap DDL.

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

# Edges: ``MATCH ()-[r]-()`` would double-count undirected relationships,
# so we anchor on ``()-[r]->()`` and rely on the workspace property on
# the relationship itself (mirrored from the upsert path).
_COUNT_EDGES_BY_TYPE_CYPHER: Final[str] = """
MATCH ()-[r]->()
WHERE r.workspace_id = $workspace_id
RETURN r.edge_type AS edge_type, count(r) AS total
ORDER BY edge_type
"""


# ---------------------------------------------------------------------------
# Import-time regression guards
# ---------------------------------------------------------------------------


def _ensure_workspace_predicate(cypher: str, label: str) -> str:
    """Assert that ``cypher`` references the workspace_id predicate.

    Raises :class:`RuntimeError` at import time if a read template has
    been refactored to drop ``workspace_id``.  Mirrors the SQL-shape
    regression tests on the pgvector path
    (``tests/test_graph_workspace_isolation.py``).

    Write-only templates (e.g. ``_DELETE_TOMBSTONED_CYPHER``) are
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


# Run the guard at module load.  If any template drops the predicate,
# importing this module raises and the application fails fast.
_ensure_workspace_predicate(_UPSERT_ENTITY_CYPHER, "_UPSERT_ENTITY_CYPHER")
_ensure_workspace_predicate(_UPSERT_EDGE_CYPHER_TEMPLATE, "_UPSERT_EDGE_CYPHER_TEMPLATE")
_ensure_workspace_predicate(_DELETE_BY_SOURCE_CYPHER, "_DELETE_BY_SOURCE_CYPHER")
_ensure_workspace_predicate(_GET_ENTITY_BY_NAME_CYPHER, "_GET_ENTITY_BY_NAME_CYPHER")
_ensure_workspace_predicate(_TRAVERSE_CYPHER_TEMPLATE, "_TRAVERSE_CYPHER_TEMPLATE")
_ensure_workspace_predicate(_COUNT_ENTITIES_CYPHER, "_COUNT_ENTITIES_CYPHER")
_ensure_workspace_predicate(_COUNT_ENTITIES_BY_KIND_CYPHER, "_COUNT_ENTITIES_BY_KIND_CYPHER")
_ensure_workspace_predicate(_COUNT_ENTITIES_BY_SOURCE_CYPHER, "_COUNT_ENTITIES_BY_SOURCE_CYPHER")
_ensure_workspace_predicate(_COUNT_EDGES_BY_TYPE_CYPHER, "_COUNT_EDGES_BY_TYPE_CYPHER")
# ADR-0008 §Consequences-security #1: every bitemporal backfill template
# is workspace-scoped so a per-workspace backfill never touches another
# tenant's data.  Drop the predicate, the module fails to import.
_ensure_workspace_predicate(_BACKFILL_ENTITY_PROPS_CYPHER, "_BACKFILL_ENTITY_PROPS_CYPHER")
_ensure_workspace_predicate(
    _BACKFILL_ENTITY_STATE_NODES_CYPHER, "_BACKFILL_ENTITY_STATE_NODES_CYPHER"
)
_ensure_workspace_predicate(_BACKFILL_EDGE_PROPS_CYPHER, "_BACKFILL_EDGE_PROPS_CYPHER")
# ADR-0008 §Consequences-security #1 (issue #131) — every bitemporal write
# template is workspace-scoped on every node, every state node, and every
# relationship.  Drop the predicate, the module fails to import.  These
# templates introduce `:EntityState` as a new node label and `[:HAD_STATE]`
# as a new relationship — both are new attack surfaces, so workspace_id
# must be on every read AND every write of those shapes.
_ensure_workspace_predicate(_UPSERT_ENTITY_BITEMPORAL_CYPHER, "_UPSERT_ENTITY_BITEMPORAL_CYPHER")
_ensure_workspace_predicate(
    _UPSERT_EDGE_BITEMPORAL_CYPHER_TEMPLATE, "_UPSERT_EDGE_BITEMPORAL_CYPHER_TEMPLATE"
)
# ADR-0009 §Consequences-security #1: every retention Cypher template
# iterates ONE workspace at a time and filters every MATCH on
# `workspace_id`.  Cross-tenant batches are forbidden — drop the
# predicate and the module fails to import.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp_depth(requested: int, ceiling: int) -> int:
    """Clamp ``requested`` into ``[1, ceiling]`` — protects the planner."""
    if requested < 1:
        return 1
    if requested > ceiling:
        return ceiling
    return requested


def _validate_edge_types(edge_types: list[str] | None) -> list[str] | None:
    """Validate user-supplied edge types against the allowlist regex."""
    if edge_types is None:
        return None
    for value in edge_types:
        if not _EDGE_TYPE_REGEX.match(value):
            raise ValueError(f"invalid_edge_type:{value}")
    return edge_types


def _build_traverse_cypher(max_depth: int, edge_types: list[str] | None) -> str:
    """Render the traversal Cypher with a static (validated) depth bound.

    ``max_depth`` is an integer that has already been clamped by
    :func:`_clamp_depth`.  ``edge_types`` has been validated by
    :func:`_validate_edge_types` — both are safe to interpolate.
    """
    if edge_types:
        quoted = ",".join(f"'{et}'" for et in edge_types)
        filter_clause = f" AND r.edge_type IN [{quoted}]"
    else:
        filter_clause = ""
    rendered = _TRAVERSE_CYPHER_TEMPLATE.replace("__MAX_DEPTH__", str(max_depth))
    return rendered.replace("__EDGE_TYPE_FILTER__", filter_clause)


def _entity_record_to_view(record: dict[str, Any]) -> EntityNodeView:
    """Build an :class:`EntityNodeView` from a single-row Cypher result."""
    return EntityNodeView(
        name=str(record["name"]),
        kind=str(record["kind"]),
        source=str(record["source_id"]),
        chunk_text=record.get("chunk_text"),
        depth=0,
        edge_type=None,
    )


def _neighbour_to_view(payload: dict[str, Any]) -> EntityNodeView:
    """Build a neighbour :class:`EntityNodeView` from collect() output."""
    return EntityNodeView(
        name=str(payload["name"]),
        kind=str(payload["kind"]),
        source=str(payload["source_id"]),
        chunk_text=payload.get("chunk_text"),
        depth=int(payload.get("depth", 1)),
        edge_type=(str(payload["edge_type"]) if payload.get("edge_type") else None),
    )


def _edge_tuple_to_view(triplet: list[Any]) -> GraphEdgeView:
    """Build a :class:`GraphEdgeView` from a ``[from, to, edge_type]`` row."""
    return GraphEdgeView(
        from_entity=str(triplet[0]),
        to_entity=str(triplet[1]),
        edge_type=str(triplet[2]),
    )


def _coerce_metadata(value: Any) -> dict[str, Any]:
    """Coerce a duck-typed ``metadata`` value into a dict[str, Any]."""
    if value is None:
        return {}
    if isinstance(value, dict):
        # Re-type explicitly — mypy --strict refuses ``dict`` without params.
        return {str(k): v for k, v in value.items()}
    raise TypeError(f"metadata must be dict, got {type(value).__name__}")


def _coerce_to_datetime(value: Any) -> datetime:
    """Coerce a Neo4j temporal-like value to a Python aware ``datetime``.

    Neo4j returns its own ``DateTime`` type from the driver, which
    duck-types ``to_native()`` returning ``datetime.datetime``.  ISO-8601
    strings (the form we round-trip via ``isoformat()``) are also
    accepted.  Naive datetimes are stamped UTC so all comparisons in
    the worker happen in one timezone.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if hasattr(value, "to_native"):
        native = value.to_native()
        if isinstance(native, datetime):
            return native if native.tzinfo else native.replace(tzinfo=UTC)
    if isinstance(value, str):
        # ISO-8601 string; isoformat round-trip from the writer.
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    raise TypeError(f"cannot coerce to datetime: {type(value).__name__}")


def _coerce_to_date(value: Any) -> date:
    """Coerce a Neo4j-like temporal value to ``datetime.date``."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "to_native"):
        native = value.to_native()
        if isinstance(native, datetime):
            return native.date()
        if isinstance(native, date):
            return native
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError(f"cannot coerce to date: {type(value).__name__}")


# ---------------------------------------------------------------------------
# State-change fingerprinting (ADR-0008 §2 — issue #131)
# ---------------------------------------------------------------------------
#
# The bitemporal writer compares the incoming state to the still-current
# state on the `:Entity` mirror to decide whether to emit a new version.
# Doing this in Python (rather than concatenating in Cypher) is more
# robust to NULL handling, list ordering, and nested-dict serialisation
# differences across the driver.  The fingerprint is stored as a string
# property on the `:Entity` mirror and on each open relationship; a single
# string-equality predicate inside the Cypher template drives the
# conditional version bump.
#
# The fingerprint covers exactly the fields that ADR-0008 §2 names as
# "state-at-this-version" — state changes that callers can observe via
# `as_of` reads.  Operational metadata that is not bitemporal (e.g. the
# pre-bitemporal `created_at`/`updated_at` columns kept for backward
# compatibility) is intentionally excluded.

# SHA-256 hex digest length — used for the fingerprint property.  Picked
# because it is collision-resistant for the field-set sizes we see and
# the digest length is bounded so the index footprint is predictable.
_FINGERPRINT_DIGEST_BYTES: Final[int] = 32  # SHA-256 output


def _stable_repr(value: Any) -> str:
    """Stable string repr for a primitive / dict / list — sorted keys.

    Python's ``repr`` is not stable across dict insertion order on values
    that round-trip through JSON (e.g. metadata coming from a connector
    YAML file).  We sort keys at every level so the fingerprint is purely
    a function of *content*, not iteration order.
    """
    if value is None:
        return "None"
    if isinstance(value, dict):
        items = sorted(((str(k), _stable_repr(v)) for k, v in value.items()))
        return "{" + ",".join(f"{k}:{v}" for k, v in items) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_stable_repr(v) for v in value) + "]"
    return repr(value)


def _entity_state_fingerprint(params: dict[str, Any]) -> str:
    """Compute the entity-state fingerprint for ``params``.

    Covers the fields that ADR-0008 §2 names as the versioned snapshot:
    ``kind``, ``name``, ``display_name``, ``source_id``, ``chunk_id``,
    ``metadata``.  ``id`` and ``workspace_id`` are identity, not state —
    excluded.  ``created_at`` / ``updated_at`` are operational, not
    bitemporal — excluded.
    """
    import hashlib

    payload = "|".join(
        (
            _stable_repr(params.get("entity_type")),
            _stable_repr(params.get("name")),
            _stable_repr(params.get("display_name")),
            _stable_repr(params.get("source_id")),
            _stable_repr(params.get("chunk_id")),
            _stable_repr(params.get("metadata")),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _edge_state_fingerprint(params: dict[str, Any]) -> str:
    """Compute the edge-state fingerprint.

    Covers ``edge_type``, ``source_id`` (origin document/source), and
    ``metadata``.  Endpoints (``source_id_ent`` / ``target_id_ent``) and
    ``workspace_id`` are part of the relationship identity (the MERGE key)
    so they are excluded — a change there is not a "state change", it is
    a different relationship.
    """
    import hashlib

    payload = "|".join(
        (
            _stable_repr(params.get("edge_type")),
            _stable_repr(params.get("source_id")),
            _stable_repr(params.get("metadata")),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class Neo4jGraphStore:
    """Neo4j-backed ``GraphStore`` — Phase-2a adapter for issue #104.

    Lifecycle
    ---------
    ``__init__`` builds the async driver but does NOT open a connection.
    Callers MUST invoke :meth:`connect` before the first query so the
    driver verifies connectivity and the schema bootstrap runs exactly
    once.  :meth:`close` releases the driver.

    The DI layer (``apps/server/.../app.py``) drives lifecycle inside
    the FastAPI lifespan context.
    """

    def __init__(self, *, config: Neo4jStoreConfig) -> None:
        self._config = config
        self._driver: AsyncDriver = AsyncGraphDatabase.driver(
            config.uri,
            auth=(config.username, config.password),
            max_connection_pool_size=config.max_connection_pool_size,
            connection_acquisition_timeout=(config.connection_acquisition_timeout_seconds),
            max_transaction_retry_time=(config.max_transaction_retry_time_seconds),
        )
        self._bootstrapped: bool = False
        # ADR-0008 §8 phase 2 + issue #131 — read once at __init__ and pin
        # onto the instance.  "Consistency over flexibility": runtime flips
        # require restart, which is by design.  Mirrors ADR-0009 §1's
        # "eviction operates on `recorded_at`" stance — a writer that flips
        # mid-process would publish two versioning regimes into the same
        # graph, which the retention worker cannot reconcile.
        self._bitemporal_enabled: bool = bool(config.bitemporal_enabled)

    async def connect(self) -> None:
        """Verify connectivity and run the idempotent schema bootstrap."""
        await self._driver.verify_connectivity()
        await self._bootstrap_schema()
        self._bootstrapped = True
        log.info("neo4j_graph_store_ready", database=self._config.database)

    async def close(self) -> None:
        """Close the underlying async driver."""
        await self._driver.close()

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    async def _bootstrap_schema(self) -> None:
        """Run constraint + index DDL idempotently (ADR-0005 §Schema)."""
        async with self._driver.session(database=self._config.database) as session:
            for stmt in _BOOTSTRAP_STATEMENTS:
                await session.execute_write(_run_write_stmt, stmt, {})

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    async def upsert_graph(
        self,
        *,
        source_id: uuid.UUID,
        document_id: uuid.UUID,
        entities: list[Any],
        edges: list[Any],
    ) -> None:
        """Persist a batch of entities+edges for one document (idempotent).

        ``document_id`` is currently passed for symmetry with the
        pgvector path; Neo4j keys on ``source_id`` per ADR-0005.  It is
        kept in the signature to preserve call-site compatibility and
        is logged for audit.
        """
        workspace_id = self._workspace_from_entities(entities)
        async with self._driver.session(database=self._config.database) as session:
            await session.execute_write(
                self._run_upsert_graph,
                workspace_id,
                source_id,
                entities,
                edges,
                self._bitemporal_enabled,
            )
        log.info(
            "neo4j_upsert_graph",
            source_id=str(source_id),
            document_id=str(document_id),
            entities=len(entities),
            edges=len(edges),
        )

    @staticmethod
    def _workspace_from_entities(entities: list[Any]) -> uuid.UUID:
        """Pull workspace_id off the first entity; reject an empty batch.

        The ingestion layer tags each entity with a ``workspace_id``
        attribute (Phase 2 extension — pre-Phase 2 entities live in the
        pgvector path).  For the duck-typed parser ``ExtractedEntity``
        shape which does NOT carry ``workspace_id``, the caller (the
        ingestion worker) must wrap the batch in an
        :class:`EntityUpsert`-like object or set
        ``entity.metadata['workspace_id']``.  See ``upsert_entity``
        for the single-entity path which pins it at the protocol level.
        """
        if not entities:
            raise ValueError("upsert_graph_empty_batch")
        head = entities[0]
        ws = getattr(head, "workspace_id", None)
        if ws is None:
            metadata = getattr(head, "metadata", None) or {}
            ws = metadata.get("workspace_id")
        if ws is None:
            raise ValueError("upsert_graph_missing_workspace_id")
        if isinstance(ws, uuid.UUID):
            return ws
        return uuid.UUID(str(ws))

    @staticmethod
    async def _run_upsert_graph(
        tx: AsyncManagedTransaction,
        workspace_id: uuid.UUID,
        source_id: uuid.UUID,
        entities: list[Any],
        edges: list[Any],
        bitemporal_enabled: bool,
    ) -> None:
        """Transaction body for :meth:`upsert_graph` (idempotent replace).

        ``bitemporal_enabled`` is the per-instance flag forwarded from
        :meth:`upsert_graph`.  Static method by design (Neo4j managed-tx
        retry passes positional args to the callable), so the flag has to
        come in through the parameter list rather than ``self``.
        """
        now = datetime.now(UTC).isoformat()
        # Replace-by-source: drop old entities (and their edges) for this
        # source, then re-insert.  Mirrors IndexWriter.upsert_graph.
        # NOTE: ADR-0008 §3 says the writer no longer DELETEs once the
        # bitemporal flag is on — but per issue #131's "Non-goals", the
        # `_DELETE_BY_SOURCE_CYPHER` template is owned by Wave 5 (#137)
        # and is intentionally left alone in this PR.  The writer keeps
        # delete-by-source semantics until that wave lands.
        await tx.run(
            _DELETE_BY_SOURCE_CYPHER,
            {_WRITE_WORKSPACE_PARAM: str(workspace_id), "source_id": str(source_id)},
        )

        entity_cypher = (
            _UPSERT_ENTITY_BITEMPORAL_CYPHER if bitemporal_enabled else _UPSERT_ENTITY_CYPHER
        )

        name_to_id: dict[str, uuid.UUID] = {}
        for ext_ent in entities:
            params = _entity_to_params(ext_ent, source_id, workspace_id, now)
            if bitemporal_enabled:
                params["state_fingerprint"] = _entity_state_fingerprint(params)
            await tx.run(entity_cypher, params)
            name_to_id[str(params["name"])] = uuid.UUID(str(params["id"]))
            display = str(params.get("display_name") or "")
            if display:
                name_to_id.setdefault(display, uuid.UUID(str(params["id"])))

        edge_template = (
            _UPSERT_EDGE_BITEMPORAL_CYPHER_TEMPLATE
            if bitemporal_enabled
            else _UPSERT_EDGE_CYPHER_TEMPLATE
        )
        for ext_edge in edges:
            edge_params = _edge_to_params(ext_edge, source_id, workspace_id, name_to_id, now)
            if edge_params is None:
                continue
            if bitemporal_enabled:
                edge_params["state_fingerprint"] = _edge_state_fingerprint(edge_params)
            rendered = edge_template.replace("{edge_type}", str(edge_params["edge_type"]))
            await tx.run(rendered, edge_params)

    async def upsert_entity(
        self,
        *,
        entity: EntityUpsert,
        workspace_id: uuid.UUID,
    ) -> None:
        """Upsert a single entity within ``workspace_id`` (idempotent).

        Behaviour depends on the bitemporal flag pinned at ``__init__``:

        - ``bitemporal_enabled=False`` (default during rollout): emits the
          PR #104 MERGE shape verbatim — no version chain accumulates.
        - ``bitemporal_enabled=True`` (post-cutover): emits the ADR-0008
          §2 versioning shape — the previous ``(:EntityState)`` is
          end-dated and a new one is created iff the state fingerprint
          differs from the still-current version on the identity mirror.
          A no-op upsert (same content) does not produce a new version.
        """
        now = datetime.now(UTC).isoformat()
        params: dict[str, Any] = {
            _WRITE_WORKSPACE_PARAM: str(workspace_id),
            "id": str(entity.id),
            "source_id": str(entity.source_id),
            "entity_type": entity.entity_type,
            "name": entity.name,
            "display_name": entity.display_name,
            "chunk_id": (str(entity.chunk_id) if entity.chunk_id else None),
            "metadata": _coerce_metadata(entity.metadata),
            "now": now,
        }
        cypher = self._select_entity_upsert_cypher(params)
        async with self._driver.session(database=self._config.database) as session:
            await session.execute_write(_run_write_stmt, cypher, params)

    async def upsert_edge(
        self,
        *,
        edge: EdgeUpsert,
        workspace_id: uuid.UUID,
    ) -> None:
        """Upsert a single edge within ``workspace_id`` (idempotent).

        See :meth:`upsert_entity` for the flag-gated behaviour split.
        """
        edge_type = edge.edge_type
        if not _EDGE_TYPE_REGEX.match(edge_type):
            raise ValueError(f"invalid_edge_type:{edge_type}")
        now = datetime.now(UTC).isoformat()
        params: dict[str, Any] = {
            _WRITE_WORKSPACE_PARAM: str(workspace_id),
            "source_id_ent": str(edge.source_entity_id),
            "target_id_ent": str(edge.target_entity_id),
            "source_id": str(edge.metadata.get("source_id") or ""),
            "edge_type": edge_type,
            "metadata": _coerce_metadata(edge.metadata),
            "now": now,
        }
        rendered = self._select_edge_upsert_cypher(params, edge_type)
        async with self._driver.session(database=self._config.database) as session:
            await session.execute_write(_run_write_stmt, rendered, params)

    def _select_entity_upsert_cypher(self, params: dict[str, Any]) -> str:
        """Pick legacy or bitemporal entity upsert Cypher; mutate params if needed.

        Mutates ``params`` in place to add the ``state_fingerprint`` key
        when the bitemporal path is active.  Keeps the call sites short
        (``upsert_entity`` and ``_run_upsert_graph`` share this).
        """
        if not self._bitemporal_enabled:
            return _UPSERT_ENTITY_CYPHER
        params["state_fingerprint"] = _entity_state_fingerprint(params)
        return _UPSERT_ENTITY_BITEMPORAL_CYPHER

    def _select_edge_upsert_cypher(self, params: dict[str, Any], edge_type: str) -> str:
        """Pick legacy or bitemporal edge upsert Cypher; render edge-type slot."""
        if not self._bitemporal_enabled:
            return _UPSERT_EDGE_CYPHER_TEMPLATE.replace("{edge_type}", edge_type)
        params["state_fingerprint"] = _edge_state_fingerprint(params)
        return _UPSERT_EDGE_BITEMPORAL_CYPHER_TEMPLATE.replace("{edge_type}", edge_type)

    async def delete_tombstoned(self) -> int:
        """Hard-delete tombstoned entities; return the count removed."""
        async with self._driver.session(database=self._config.database) as session:
            rows = await session.execute_write(_run_write_returning, _DELETE_TOMBSTONED_CYPHER, {})
        if not rows:
            return 0
        return int(rows[0].get("deleted", 0))

    # ------------------------------------------------------------------
    # Bitemporal backfill (ADR-0008 §8 phase 1, issue #130)
    # ------------------------------------------------------------------

    async def backfill_bitemporal(
        self,
        *,
        workspace_id: uuid.UUID,
        batch_size: int = BACKFILL_DEFAULT_BATCH_SIZE,
    ) -> int:
        """Populate the bitemporal triple on legacy nodes/edges in one workspace.

        ADR-0008 §8 phase 1.  Maps the pre-bitemporal `created_at` /
        `updated_at` properties onto `valid_from` / `recorded_at` and
        sets `valid_to = NULL` (the still-valid sentinel from §1) on
        every existing `:Entity` and every existing relationship in
        ``workspace_id``, then materializes the initial
        ``(:EntityState)`` row plus a single ``[:HAD_STATE]`` link from
        the identity per §2.

        The method runs the three phase-1 templates as a chunked loop
        until each pass touches zero rows; the caller can re-invoke
        safely (the migration is idempotent — re-runs are no-ops on
        already-migrated rows because every SET is guarded by
        ``WHERE n.valid_from IS NULL``).

        ACL invariant per ADR-0008 §Consequences-security #1: every
        Cypher template iterates ONE workspace at a time and filters
        every MATCH on ``workspace_id``.  Workspace A's backfill never
        touches workspace B; the cross-workspace isolation regression
        test in ``tests/test_graph_workspace_isolation.py`` exercises
        this contract.

        Returns the total number of rows modified across all three
        phases, summed across all chunks.  A return value of zero on a
        fresh invocation means the workspace is already fully migrated.

        NOT auto-invoked at ``connect()`` — this is operator-driven, run
        via the admin CLI scheduled by issue #135 or a sibling admin
        endpoint.  The FastAPI lifespan never runs the backfill.
        """
        if batch_size < 1:
            raise ValueError(f"backfill_batch_size_must_be_positive:{batch_size}")
        params: dict[str, Any] = {
            _WORKSPACE_PARAM: str(workspace_id),
            "batch_size": int(batch_size),
        }
        total_modified = 0
        for cypher in _BITEMPORAL_BACKFILL_STATEMENTS:
            total_modified += await self._run_backfill_phase(cypher, params)
        return total_modified

    async def _run_backfill_phase(
        self,
        cypher: str,
        params: dict[str, Any],
    ) -> int:
        """Loop one phase's Cypher until a chunk modifies zero rows.

        Each chunk is its own write transaction so a transient error
        rolls back only that chunk; re-invocation resumes from where
        the previous run stopped because every guard is `IS NULL`.
        """
        phase_total = 0
        while True:
            async with self._driver.session(database=self._config.database) as session:
                rows = await session.execute_write(_run_write_returning, cypher, params)
            modified = int(rows[0].get("modified", 0)) if rows else 0
            if modified == 0:
                return phase_total
            phase_total += modified

    # ------------------------------------------------------------------
    # Retention worker support (ADR-0009 §3, issue #135)
    # ------------------------------------------------------------------
    #
    # Every method below pins ``workspace_id`` at the parameter level —
    # the worker iterates per-workspace and the adapter never sees a
    # cross-workspace batch.  This is the structural ACL invariant from
    # ADR-0009 §Consequences-security #1.

    async def count_hot_to_warm_eligible(
        self,
        *,
        workspace_id: uuid.UUID,
        hot_cutoff: datetime,
    ) -> tuple[int, int]:
        """Return (entity_state_count, edge_count) eligible for hot->warm.

        ADR-0009 §2 per-version semantics: only :EntityState rows with
        ``valid_to IS NOT NULL`` are eligible; the still-current version
        of every identity is preserved in hot regardless of age.  Edges
        are eligible when end-dated AND old; pure age does not evict
        live edges (ADR-0009 §2 — edge end-dating does NOT trigger
        eviction by itself, only end-dated-and-old does).
        """
        params: dict[str, Any] = {
            _WORKSPACE_PARAM: str(workspace_id),
            "hot_cutoff": hot_cutoff.isoformat(),
        }
        async with self._driver.session(database=self._config.database) as session:
            es_rows = await session.execute_read(
                _run_read_stmt, _COUNT_HOT_TO_WARM_ENTITY_STATE_ELIGIBLE, params
            )
            edge_rows = await session.execute_read(
                _run_read_stmt, _COUNT_HOT_TO_WARM_EDGE_ELIGIBLE, params
            )
        es_count = int(es_rows[0].get("eligible", 0)) if es_rows else 0
        edge_count = int(edge_rows[0].get("eligible", 0)) if edge_rows else 0
        return es_count, edge_count

    async def sample_hot_to_warm_eligible(
        self,
        *,
        workspace_id: uuid.UUID,
        hot_cutoff: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` eligible :EntityState rows (id-only payload)."""
        if limit <= 0:
            return []
        params: dict[str, Any] = {
            _WORKSPACE_PARAM: str(workspace_id),
            "hot_cutoff": hot_cutoff.isoformat(),
            "limit": int(limit),
        }
        async with self._driver.session(database=self._config.database) as session:
            rows = await session.execute_read(
                _run_read_stmt, _SAMPLE_HOT_TO_WARM_ENTITY_STATE, params
            )
        # Coerce Neo4j temporal types to ISO strings for JSON serialisation.
        sampled: list[dict[str, Any]] = []
        for row in rows:
            sampled.append(
                {
                    "id": str(row.get("id")) if row.get("id") is not None else None,
                    "valid_from": (
                        str(row.get("valid_from")) if row.get("valid_from") is not None else None
                    ),
                    "recorded_at": (
                        str(row.get("recorded_at")) if row.get("recorded_at") is not None else None
                    ),
                }
            )
        return sampled

    async def oldest_hot_to_warm_recorded_at(
        self,
        *,
        workspace_id: uuid.UUID,
        hot_cutoff: datetime,
    ) -> datetime | None:
        """Return the oldest eligible ``recorded_at`` or ``None``.

        Used by the worker to compute the lag SLO gauge per ADR-0009 §8.
        """
        params: dict[str, Any] = {
            _WORKSPACE_PARAM: str(workspace_id),
            "hot_cutoff": hot_cutoff.isoformat(),
        }
        async with self._driver.session(database=self._config.database) as session:
            rows = await session.execute_read(
                _run_read_stmt, _OLDEST_ELIGIBLE_HOT_RECORDED_AT, params
            )
        if not rows:
            return None
        oldest = rows[0].get("oldest")
        if oldest is None:
            return None
        return _coerce_to_datetime(oldest)

    async def mark_hot_to_warm(
        self,
        *,
        workspace_id: uuid.UUID,
        hot_cutoff: datetime,
        batch_size: int,
    ) -> tuple[int, int]:
        """Tag eligible :EntityState rows + edges with ``tier_pending='warm'``.

        Returns (entity_states_marked, edges_marked).  Loops batches
        until a pass marks zero rows — bounded by the workspace's
        eligible cohort.  Each batch is its own transaction so a crash
        leaves a re-runnable state.
        """
        params: dict[str, Any] = {
            _WORKSPACE_PARAM: str(workspace_id),
            "hot_cutoff": hot_cutoff.isoformat(),
            "batch_size": int(batch_size),
        }
        es_total = await self._loop_count_query(_MARK_HOT_TO_WARM_ENTITY_STATE, params, "marked")
        edge_total = await self._loop_count_query(_MARK_HOT_TO_WARM_EDGE, params, "marked")
        return es_total, edge_total

    async def move_hot_to_warm(
        self,
        *,
        workspace_id: uuid.UUID,
        batch_size: int,
    ) -> tuple[int, int]:
        """Project marked rows to :EntitySnapshot:Daily / :RelationshipSnapshot:Daily.

        Returns (entity_states_moved, edges_moved).  The MERGE on
        (workspace_id, id, snapshot_date) makes intra-day collapsing
        idempotent — multiple eligible versions on the same UTC day fold
        into one snapshot row, with the latest ``valid_from`` winning
        (ADR-0009 §1: as-of-end-of-day projection).  Each batch deletes
        the source :EntityState in the same transaction so partial
        progress never leaves a row visible in two tiers.
        """
        params: dict[str, Any] = {
            _WORKSPACE_PARAM: str(workspace_id),
            "batch_size": int(batch_size),
        }
        es_total = await self._loop_count_query(_MOVE_HOT_TO_WARM_ENTITY_STATE, params, "moved")
        edge_total = await self._loop_count_query(_MOVE_HOT_TO_WARM_EDGE, params, "moved")
        return es_total, edge_total

    async def _loop_count_query(
        self,
        cypher: str,
        params: dict[str, Any],
        return_key: str,
    ) -> int:
        """Run ``cypher`` repeatedly in fresh tx until a pass returns 0."""
        total = 0
        while True:
            async with self._driver.session(database=self._config.database) as session:
                rows = await session.execute_write(_run_write_returning, cypher, params)
            count = int(rows[0].get(return_key, 0)) if rows else 0
            if count == 0:
                return total
            total += count

    async def count_warm_to_archive_eligible(
        self,
        *,
        workspace_id: uuid.UUID,
        warm_cutoff_date: date,
    ) -> int:
        """Count :EntitySnapshot:Daily rows older than ``warm_cutoff_date``."""
        params: dict[str, Any] = {
            _WORKSPACE_PARAM: str(workspace_id),
            "warm_cutoff_date": warm_cutoff_date.isoformat(),
        }
        async with self._driver.session(database=self._config.database) as session:
            rows = await session.execute_read(
                _run_read_stmt, _COUNT_WARM_TO_ARCHIVE_ELIGIBLE, params
            )
        return int(rows[0].get("eligible", 0)) if rows else 0

    async def list_warm_to_archive_dates(
        self,
        *,
        workspace_id: uuid.UUID,
        warm_cutoff_date: date,
        limit: int,
    ) -> list[date]:
        """Distinct snapshot dates eligible for warm->archive, ASC."""
        if limit <= 0:
            return []
        params: dict[str, Any] = {
            _WORKSPACE_PARAM: str(workspace_id),
            "warm_cutoff_date": warm_cutoff_date.isoformat(),
            "limit": int(limit),
        }
        async with self._driver.session(database=self._config.database) as session:
            rows = await session.execute_read(_run_read_stmt, _LIST_WARM_TO_ARCHIVE_DATES, params)
        out: list[date] = []
        for row in rows:
            raw = row.get("snapshot_date")
            if raw is None:
                continue
            out.append(_coerce_to_date(raw))
        return out

    async def fetch_warm_snapshot_rows(
        self,
        *,
        workspace_id: uuid.UUID,
        snapshot_date: date,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return (entity_rows, edge_rows) for one (workspace_id, date) snapshot.

        Used by the archive writer to build the parquet payload.  Both
        lists carry primitive types only (str, dict[str, Any]) so the
        parquet serialiser does not need to convert Neo4j temporal types.
        """
        params: dict[str, Any] = {
            _WORKSPACE_PARAM: str(workspace_id),
            "snapshot_date": snapshot_date.isoformat(),
        }
        async with self._driver.session(database=self._config.database) as session:
            entity_rows = await session.execute_read(
                _run_read_stmt, _FETCH_WARM_ENTITY_SNAPSHOT_ROWS, params
            )
            edge_rows = await session.execute_read(
                _run_read_stmt, _FETCH_WARM_RELATIONSHIP_SNAPSHOT_ROWS, params
            )
        return list(entity_rows), list(edge_rows)

    async def delete_warm_snapshot(
        self,
        *,
        workspace_id: uuid.UUID,
        snapshot_date: date,
    ) -> tuple[int, int]:
        """Hard-delete warm rows for (workspace_id, snapshot_date).

        Run AFTER the parquet write to S3 succeeded.  Returns
        (entity_rows_deleted, edge_rows_deleted).
        """
        params: dict[str, Any] = {
            _WORKSPACE_PARAM: str(workspace_id),
            "snapshot_date": snapshot_date.isoformat(),
        }
        async with self._driver.session(database=self._config.database) as session:
            es_rows = await session.execute_write(
                _run_write_returning, _DELETE_WARM_ENTITY_SNAPSHOT, params
            )
            edge_rows = await session.execute_write(
                _run_write_returning, _DELETE_WARM_RELATIONSHIP_SNAPSHOT, params
            )
        es_deleted = int(es_rows[0].get("deleted", 0)) if es_rows else 0
        edge_deleted = int(edge_rows[0].get("deleted", 0)) if edge_rows else 0
        return es_deleted, edge_deleted

    async def count_records_by_tier(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[str, int]:
        """Return {'hot': int, 'warm': int} record counts for the workspace.

        Used to populate the ``omniscience_graph_records_total`` gauge
        per ADR-0009 §8.
        """
        params: dict[str, Any] = {_WORKSPACE_PARAM: str(workspace_id)}
        async with self._driver.session(database=self._config.database) as session:
            hot_rows = await session.execute_read(_run_read_stmt, _COUNT_HOT_ENTITY_STATES, params)
            warm_rows = await session.execute_read(
                _run_read_stmt, _COUNT_WARM_ENTITY_SNAPSHOTS, params
            )
        return {
            "hot": int(hot_rows[0].get("total", 0)) if hot_rows else 0,
            "warm": int(warm_rows[0].get("total", 0)) if warm_rows else 0,
        }

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    async def get_entity(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
    ) -> EntityNodeView | None:
        """Resolve an entity by fully-qualified name, within workspace."""
        params: dict[str, Any] = {
            _WORKSPACE_PARAM: str(workspace_id),
            "entity_name": entity_name,
        }
        async with self._driver.session(database=self._config.database) as session:
            rows = await session.execute_read(_run_read_stmt, _GET_ENTITY_BY_NAME_CYPHER, params)
        if not rows:
            return None
        return _entity_record_to_view(rows[0])

    async def find_related(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        """BFS traversal from a seed, scoped to ``workspace_id``.

        Raises ``ValueError("entity_not_found:<name>")`` when the seed
        does not exist in the caller's workspace — matches the pgvector
        contract exactly so callers do not branch on backend.
        """
        clamped = _clamp_depth(max_depth, _MAX_DEPTH_CEILING)
        validated_types = _validate_edge_types(edge_types)
        cypher = _build_traverse_cypher(clamped, validated_types)
        params: dict[str, Any] = {
            _WORKSPACE_PARAM: str(workspace_id),
            "entity_name": entity_name,
        }
        async with self._driver.session(database=self._config.database) as session:
            rows = await session.execute_read(_run_read_stmt, cypher, params)

        if not rows:
            # Seed absent — indistinguishable from cross-workspace.
            raise ValueError(f"entity_not_found:{entity_name}")
        return _rows_to_graph_result(rows[0])

    async def traverse(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        """Alias of :meth:`find_related` per the protocol contract."""
        return await self.find_related(
            entity_name=entity_name,
            workspace_id=workspace_id,
            max_depth=max_depth,
            edge_types=edge_types,
        )

    # ------------------------------------------------------------------
    # Stats API (issue #111) — workspace-scoped
    # ------------------------------------------------------------------

    async def count_entities(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> int:
        """Count all entities visible in ``workspace_id``."""
        params: dict[str, Any] = {_WORKSPACE_PARAM: str(workspace_id)}
        async with self._driver.session(database=self._config.database) as session:
            rows = await session.execute_read(_run_read_stmt, _COUNT_ENTITIES_CYPHER, params)
        if not rows:
            return 0
        return int(rows[0].get("total", 0))

    async def count_entities_by_kind(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[str, int]:
        """Histogram of entity kinds within ``workspace_id``."""
        params: dict[str, Any] = {_WORKSPACE_PARAM: str(workspace_id)}
        async with self._driver.session(database=self._config.database) as session:
            rows = await session.execute_read(
                _run_read_stmt, _COUNT_ENTITIES_BY_KIND_CYPHER, params
            )
        return {str(r["kind"]): int(r["total"]) for r in rows if r.get("kind") is not None}

    async def count_edges_by_type(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[str, int]:
        """Histogram of edge types within ``workspace_id``."""
        params: dict[str, Any] = {_WORKSPACE_PARAM: str(workspace_id)}
        async with self._driver.session(database=self._config.database) as session:
            rows = await session.execute_read(_run_read_stmt, _COUNT_EDGES_BY_TYPE_CYPHER, params)
        return {
            str(r["edge_type"]): int(r["total"]) for r in rows if r.get("edge_type") is not None
        }

    async def count_entities_by_source(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[str, int]:
        """Per-source entity histogram within ``workspace_id``."""
        params: dict[str, Any] = {_WORKSPACE_PARAM: str(workspace_id)}
        async with self._driver.session(database=self._config.database) as session:
            rows = await session.execute_read(
                _run_read_stmt, _COUNT_ENTITIES_BY_SOURCE_CYPHER, params
            )
        return {
            str(r["source_id"]): int(r["total"]) for r in rows if r.get("source_id") is not None
        }


# ---------------------------------------------------------------------------
# Transaction-runner helpers (module-level to keep Neo4jGraphStore short)
# ---------------------------------------------------------------------------


async def _run_write_stmt(
    tx: AsyncManagedTransaction,
    cypher: str,
    params: dict[str, Any],
) -> None:
    """Run a single write statement inside a managed transaction."""
    await tx.run(cypher, params)


async def _run_write_returning(
    tx: AsyncManagedTransaction,
    cypher: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run a write statement and materialise its result rows."""
    result = await tx.run(cypher, params)
    return [record.data() async for record in result]


async def _run_read_stmt(
    tx: AsyncManagedTransaction,
    cypher: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run a read statement and materialise its result rows."""
    result = await tx.run(cypher, params)
    return [record.data() async for record in result]


def _entity_to_params(
    ext_ent: Any,
    source_id: uuid.UUID,
    workspace_id: uuid.UUID,
    now: str,
) -> dict[str, Any]:
    """Build the parameter map for :data:`_UPSERT_ENTITY_CYPHER`.

    Duck-types over :class:`EntityUpsert` **and** the parser's
    ``ExtractedEntity`` shape — both are accepted via ``upsert_graph``.
    """
    entity_id = getattr(ext_ent, "id", None)
    if entity_id is None:
        raise ValueError("entity_missing_id")
    chunk_id = getattr(ext_ent, "chunk_id", None)
    return {
        _WRITE_WORKSPACE_PARAM: str(workspace_id),
        "id": str(entity_id),
        "source_id": str(source_id),
        "entity_type": str(getattr(ext_ent, "entity_type", "")),
        "name": str(getattr(ext_ent, "name", "")),
        "display_name": str(getattr(ext_ent, "display_name", "")),
        "chunk_id": str(chunk_id) if chunk_id else None,
        "metadata": _coerce_metadata(getattr(ext_ent, "metadata", None)),
        "now": now,
    }


def _edge_to_params(
    ext_edge: Any,
    source_id: uuid.UUID,
    workspace_id: uuid.UUID,
    name_to_id: dict[str, uuid.UUID],
    now: str,
) -> dict[str, Any] | None:
    """Build the parameter map for :data:`_UPSERT_EDGE_CYPHER_TEMPLATE`.

    Returns ``None`` when the edge's target cannot be resolved in the
    current batch — mirrors :meth:`IndexWriter.upsert_graph`, which
    skips unresolvable cross-file edges for later passes.
    """
    source_entity_id = getattr(ext_edge, "source_entity_id", None)
    target_name = getattr(ext_edge, "target_name", None)
    target_entity_id = getattr(ext_edge, "target_entity_id", None)

    if target_entity_id is None and target_name is not None:
        target_entity_id = name_to_id.get(str(target_name))

    if source_entity_id is None or target_entity_id is None:
        return None
    if source_entity_id == target_entity_id:
        return None  # skip self-loops (parity with pgvector writer)

    edge_type = str(getattr(ext_edge, "edge_type", ""))
    if not _EDGE_TYPE_REGEX.match(edge_type):
        raise ValueError(f"invalid_edge_type:{edge_type}")

    return {
        _WRITE_WORKSPACE_PARAM: str(workspace_id),
        "source_id_ent": str(source_entity_id),
        "target_id_ent": str(target_entity_id),
        "source_id": str(source_id),
        "edge_type": edge_type,
        "metadata": _coerce_metadata(getattr(ext_edge, "metadata", None)),
        "now": now,
    }


def _rows_to_graph_result(row: dict[str, Any]) -> GraphResultView:
    """Assemble a :class:`GraphResultView` from a single traversal row."""
    seed = EntityNodeView(
        name=str(row["seed_name"]),
        kind=str(row["seed_kind"]),
        source=str(row["seed_source_id"]),
        chunk_text=row.get("seed_chunk_text"),
        depth=0,
        edge_type=None,
    )
    related_raw = row.get("neighbours") or []
    related = [_neighbour_to_view(n) for n in related_raw if n and n.get("name")]
    edges_raw = row.get("edges") or []
    edges = [_edge_tuple_to_view(e) for e in edges_raw if e and len(e) == 3 and e[0] and e[1]]
    return GraphResultView(seed=seed, related=related, edges=edges)


__all__ = [
    "BACKFILL_DEFAULT_BATCH_SIZE",
    "Neo4jGraphStore",
    "Neo4jStoreConfig",
]
