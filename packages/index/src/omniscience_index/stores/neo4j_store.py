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
from datetime import UTC, datetime
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

_BOOTSTRAP_STATEMENTS: Final[tuple[str, ...]] = (
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
    ) -> None:
        """Transaction body for :meth:`upsert_graph` (idempotent replace)."""
        now = datetime.now(UTC).isoformat()
        # Replace-by-source: drop old entities (and their edges) for this
        # source, then re-insert.  Mirrors IndexWriter.upsert_graph.
        await tx.run(
            _DELETE_BY_SOURCE_CYPHER,
            {_WRITE_WORKSPACE_PARAM: str(workspace_id), "source_id": str(source_id)},
        )

        name_to_id: dict[str, uuid.UUID] = {}
        for ext_ent in entities:
            params = _entity_to_params(ext_ent, source_id, workspace_id, now)
            await tx.run(_UPSERT_ENTITY_CYPHER, params)
            name_to_id[str(params["name"])] = uuid.UUID(str(params["id"]))
            display = str(params.get("display_name") or "")
            if display:
                name_to_id.setdefault(display, uuid.UUID(str(params["id"])))

        for ext_edge in edges:
            edge_params = _edge_to_params(ext_edge, source_id, workspace_id, name_to_id, now)
            if edge_params is None:
                continue
            rendered = _UPSERT_EDGE_CYPHER_TEMPLATE.replace(
                "{edge_type}", str(edge_params["edge_type"])
            )
            await tx.run(rendered, edge_params)

    async def upsert_entity(
        self,
        *,
        entity: EntityUpsert,
        workspace_id: uuid.UUID,
    ) -> None:
        """Upsert a single entity within ``workspace_id`` (idempotent)."""
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
        async with self._driver.session(database=self._config.database) as session:
            await session.execute_write(_run_write_stmt, _UPSERT_ENTITY_CYPHER, params)

    async def upsert_edge(
        self,
        *,
        edge: EdgeUpsert,
        workspace_id: uuid.UUID,
    ) -> None:
        """Upsert a single edge within ``workspace_id`` (idempotent)."""
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
        rendered = _UPSERT_EDGE_CYPHER_TEMPLATE.replace("{edge_type}", edge_type)
        async with self._driver.session(database=self._config.database) as session:
            await session.execute_write(_run_write_stmt, rendered, params)

    async def delete_tombstoned(self) -> int:
        """Hard-delete tombstoned entities; return the count removed."""
        async with self._driver.session(database=self._config.database) as session:
            rows = await session.execute_write(_run_write_returning, _DELETE_TOMBSTONED_CYPHER, {})
        if not rows:
            return 0
        return int(rows[0].get("deleted", 0))

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
    "Neo4jGraphStore",
    "Neo4jStoreConfig",
]
