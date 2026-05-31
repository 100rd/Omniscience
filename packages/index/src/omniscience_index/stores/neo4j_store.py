"""Neo4j-backed adapter for the ``GraphStore`` protocol (issue #104).

Implements ``omniscience_core.storage.GraphStore`` against the official
``neo4j`` Python driver (async variant), per ADR-0005
(``docs/decisions/0005-neo4j-as-graph-store.md``).

This module is now a **thin re-export sheet**.  All implementation has
been decomposed into the ``neo4j/`` subpackage:

- ``neo4j/_cypher.py``  — Cypher/DDL constants + import-time ACL guards
- ``neo4j/mappers.py``  — row mappers, metadata coercion, fingerprinting,
                          datetime helpers, traversal builder
- ``neo4j/_tx.py``      — low-level async transaction runner helpers
- ``neo4j/store.py``    — ``Neo4jStoreConfig`` + ``Neo4jGraphStore``

Every public name AND every private name that existing callers (tests,
scripts, app code) import from this path is re-exported here so that no
import site needs to change.

Note on mock.patch compatibility
---------------------------------
Tests patch ``omniscience_index.stores.neo4j_store.AsyncGraphDatabase.driver``
and ``omniscience_index.stores.neo4j_store._run_write_stmt``.  For these
patches to intercept real calls, the patched names must exist in THIS
module's namespace.  ``AsyncGraphDatabase`` is imported here so that
``patch("...neo4j_store.AsyncGraphDatabase.driver")`` mutates the class
object that ``store.py`` also holds a reference to — the same class, so
the patch is visible in both modules.  ``_run_write_stmt`` is re-exported
from ``_tx.py`` and so is already in this namespace.
"""

# ruff: noqa: F401  -- all imports are intentional re-exports for backward compatibility
from __future__ import annotations

from neo4j import AsyncGraphDatabase, AsyncManagedTransaction

from omniscience_index.stores.neo4j._cypher import (
    _BACKFILL_EDGE_PROPS_CYPHER,
    _BACKFILL_ENTITY_PROPS_CYPHER,
    _BACKFILL_ENTITY_STATE_NODES_CYPHER,
    _BITEMPORAL_BACKFILL_STATEMENTS,
    _BOOTSTRAP_STATEMENTS,
    _COUNT_EDGES_BY_TYPE_CYPHER,
    _COUNT_ENTITIES_BY_KIND_CYPHER,
    _COUNT_ENTITIES_BY_SOURCE_CYPHER,
    _COUNT_ENTITIES_CYPHER,
    _COUNT_HOT_ENTITY_STATES,
    _COUNT_HOT_TO_WARM_EDGE_ELIGIBLE,
    _COUNT_HOT_TO_WARM_ENTITY_STATE_ELIGIBLE,
    _COUNT_WARM_ENTITY_SNAPSHOTS,
    _COUNT_WARM_TO_ARCHIVE_ELIGIBLE,
    _DELETE_BY_SOURCE_CYPHER,
    _DELETE_TOMBSTONED_CYPHER,
    _DELETE_WARM_ENTITY_SNAPSHOT,
    _DELETE_WARM_RELATIONSHIP_SNAPSHOT,
    _EDGE_INDEX_TEMPLATES,
    _EDGE_STATE_FINGERPRINT_PROP,
    _EDGE_TYPE_REGEX,
    _END_DATE_BY_SOURCE_CYPHER,
    _END_DATE_TOMBSTONED_CYPHER,
    _ENTITY_LABEL,
    _ENTITY_SNAPSHOT_LABELS,
    _ENTITY_STATE_FINGERPRINT_PROP,
    _ENTITY_STATE_LABEL,
    _FETCH_WARM_ENTITY_SNAPSHOT_ROWS,
    _FETCH_WARM_RELATIONSHIP_SNAPSHOT_ROWS,
    _GET_ENTITY_BY_NAME_AS_OF_CYPHER,
    _GET_ENTITY_BY_NAME_CYPHER,
    _LIST_RELATIONSHIP_TYPES_CYPHER,
    _LIST_WARM_TO_ARCHIVE_DATES,
    _MARK_HOT_TO_WARM_EDGE,
    _MARK_HOT_TO_WARM_ENTITY_STATE,
    _MAX_DEPTH_CEILING,
    _MOVE_HOT_TO_WARM_EDGE,
    _MOVE_HOT_TO_WARM_ENTITY_STATE,
    _OLDEST_ELIGIBLE_HOT_RECORDED_AT,
    _REL_TYPE_KEY,
    _RELATIONSHIP_SNAPSHOT_LABELS,
    _RESOLVE_STUBS_CYPHER,
    _SAMPLE_HOT_TO_WARM_ENTITY_STATE,
    _SEED_ONLY_CYPHER,
    _TRAVERSE_AS_OF_CYPHER_TEMPLATE,
    _TRAVERSE_CYPHER_TEMPLATE,
    _UPSERT_EDGE_BITEMPORAL_CYPHER_TEMPLATE,
    _UPSERT_EDGE_BY_NAME_CYPHER_TEMPLATE,
    _UPSERT_EDGE_CYPHER_TEMPLATE,
    _UPSERT_ENTITY_BITEMPORAL_CYPHER,
    _UPSERT_ENTITY_CYPHER,
    _WORKSPACE_PARAM,
    _WRITE_WORKSPACE_PARAM,
    BACKFILL_DEFAULT_BATCH_SIZE,
    _ensure_workspace_predicate,
)
from omniscience_index.stores.neo4j._tx import (
    _run_read_stmt,
    _run_write_returning,
    _run_write_stmt,
)
from omniscience_index.stores.neo4j.mappers import (
    _FINGERPRINT_DIGEST_BYTES,
    _as_of_to_param,
    _build_traverse_cypher,
    _clamp_depth,
    _coerce_metadata,
    _coerce_to_date,
    _coerce_to_datetime,
    _deserialise_metadata,
    _edge_index_name,
    _edge_state_fingerprint,
    _edge_to_params,
    _edge_tuple_to_view,
    _entity_record_to_view,
    _entity_state_fingerprint,
    _entity_to_params,
    _neighbour_to_view,
    _optional_datetime,
    _rows_to_graph_result,
    _serialise_metadata,
    _serialise_metadata_param,
    _stable_repr,
    _validate_edge_types,
)
from omniscience_index.stores.neo4j.store import Neo4jGraphStore, Neo4jStoreConfig

__all__ = [
    "BACKFILL_DEFAULT_BATCH_SIZE",
    "Neo4jGraphStore",
    "Neo4jStoreConfig",
]
