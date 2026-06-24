"""Neo4j-backed adapter for the ``GraphStore`` protocol (issue #104).

Implements ``omniscience_core.storage.GraphStore`` against the official
``neo4j`` Python driver (async variant), per ADR-0005.

This module contains only the config dataclass and the store class.
All Cypher templates live in ``._cypher``, all row converters /
helpers live in ``.mappers``, and transaction runners live in ``._tx``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import structlog
from neo4j import AsyncDriver, AsyncGraphDatabase
from omniscience_core.storage.graph import (
    EdgeUpsert,
    EntityNodeView,
    EntityUpsert,
    GraphResultView,
)
from omniscience_core.telemetry.metrics import GRAPH_END_DATED_TOTAL

from omniscience_index.stores.neo4j._cypher import (
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
    _EDGE_TYPE_REGEX,
    _END_DATE_BY_SOURCE_CYPHER,
    _END_DATE_TOMBSTONED_CYPHER,
    _ENTITY_LABEL,
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
    _RESOLVE_STUBS_CYPHER,
    _SAMPLE_HOT_TO_WARM_ENTITY_STATE,
    _UPSERT_EDGE_BITEMPORAL_CYPHER_TEMPLATE,
    _UPSERT_EDGE_BY_NAME_CYPHER_TEMPLATE,
    _UPSERT_EDGE_CYPHER_TEMPLATE,
    _UPSERT_ENTITY_BITEMPORAL_CAS_CYPHER,
    _UPSERT_ENTITY_BITEMPORAL_CYPHER,
    _UPSERT_ENTITY_CAS_CYPHER,
    _UPSERT_ENTITY_CYPHER,
    _WORKSPACE_PARAM,
    _WRITE_WORKSPACE_PARAM,
    BACKFILL_DEFAULT_BATCH_SIZE,
)
from omniscience_index.stores.neo4j._tx import (
    _run_read_stmt,
    _run_write_returning,
    _run_write_stmt,
)
from omniscience_index.stores.neo4j.mappers import (
    _as_of_to_param,
    _build_list_entities_cypher,
    _build_traverse_cypher,
    _clamp_depth,
    _cluster_from_metadata,
    _coerce_metadata,
    _coerce_to_date,
    _coerce_to_datetime,
    _edge_index_name,
    _edge_state_fingerprint,
    _edge_to_params,
    _entity_record_to_view,
    _entity_state_fingerprint,
    _entity_to_params,
    _rows_to_graph_result,
    _serialise_metadata_param,
    _validate_edge_types,
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
    # ADR-0008 §8 — bitemporal write-path rollout flag.  Read once at adapter
    # `__init__` and pinned onto the instance per ADR-0008's "consistency over
    # flexibility" rollout rule (issue #131).  Production derives this from
    # ``Settings.graph_bitemporal`` via :meth:`from_settings`, which defaults
    # to ``enabled`` since #317.  The dataclass default stays ``False`` so
    # direct construction (low-level adapter unit tests) keeps PR #104's
    # legacy writer unless a caller explicitly opts in.
    bitemporal_enabled: bool = False

    @classmethod
    def from_settings(cls, settings: Any) -> Neo4jStoreConfig:
        """Build a config from the canonical ``Settings`` object.

        Typed as ``Any`` because importing ``Settings`` here would create
        an import cycle (``omniscience_core`` -> ``omniscience_index``).
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
            bitemporal_enabled=(str(settings.graph_bitemporal) == "enabled"),
        )


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
        self._bitemporal_enabled: bool = config.bitemporal_enabled

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
            rel_type_rows = await session.execute_read(
                _run_read_stmt, _LIST_RELATIONSHIP_TYPES_CYPHER, {}
            )
            for row in rel_type_rows:
                rel_type = str(row["relationshipType"])
                if not _EDGE_TYPE_REGEX.match(rel_type):
                    log.warning(
                        "neo4j_skipping_index_for_invalid_rel_type",
                        rel_type=rel_type,
                    )
                    continue
                for index_prefix, template in _EDGE_INDEX_TEMPLATES:
                    index_name = _edge_index_name(index_prefix, rel_type)
                    stmt = template.format(name=index_name, rel_type=rel_type)
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
        workspace_id: uuid.UUID | None = None,
        snapshot_at: datetime | None = None,
        version: int | None = None,
        epoch: int | None = None,
        forced_replay: bool = False,
    ) -> None:
        """Persist a batch of entities+edges for one document (idempotent)."""
        if workspace_id is None:
            workspace_id = self._workspace_from_entities(entities)
        snap_iso = snapshot_at.isoformat() if snapshot_at is not None else None
        async with self._driver.session(database=self._config.database) as session:
            counts = await session.execute_write(
                self._run_upsert_graph,
                workspace_id,
                source_id,
                entities,
                edges,
                self._bitemporal_enabled,
                snap_iso,
                version,
                epoch,
                forced_replay,
            )
        entities_end_dated, edges_end_dated = counts if counts is not None else (0, 0)
        if entities_end_dated:
            GRAPH_END_DATED_TOTAL.labels(kind="entity", reason="snapshot").inc(entities_end_dated)
        if edges_end_dated:
            GRAPH_END_DATED_TOTAL.labels(kind="edge", reason="snapshot").inc(edges_end_dated)
        log.info(
            "neo4j_upsert_graph",
            source_id=str(source_id),
            document_id=str(document_id),
            entities=len(entities),
            edges=len(edges),
            entities_end_dated=entities_end_dated,
            edges_end_dated=edges_end_dated,
            bitemporal_enabled=self._bitemporal_enabled,
            snapshot_at=snap_iso,
        )

    @staticmethod
    def _workspace_from_entities(entities: list[Any]) -> uuid.UUID:
        """Pull workspace_id off the first entity; reject an empty batch."""
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
        tx: Any,
        workspace_id: uuid.UUID,
        source_id: uuid.UUID,
        entities: list[Any],
        edges: list[Any],
        bitemporal_enabled: bool,
        snapshot_at_iso: str | None,
        version: int | None,
        epoch: int | None = None,
        forced_replay: bool = False,
    ) -> tuple[int, int]:
        """Transaction body for :meth:`upsert_graph` (idempotent replace)."""
        now = datetime.now(UTC).isoformat()

        if version is not None:
            res = await tx.run(
                (
                    "MATCH (c:StoreCheckpoint"
                    " {workspace_id: $workspace_id, source_id: $source_id})"
                    " RETURN c.version AS version, c.epoch AS epoch"
                ),
                {"workspace_id": str(workspace_id), "source_id": str(source_id)},
            )
            record = await res.single()

            existing_version = record["version"] if record is not None else None
            existing_epoch = record["epoch"] if record is not None else None

            should_skip = False
            if existing_version is not None and existing_version >= version:
                should_skip = True

            if epoch is not None and existing_epoch is not None and epoch > existing_epoch:
                should_skip = False

            if forced_replay:
                should_skip = False

            if should_skip:
                return 0, 0

            await tx.run(
                (
                    "MERGE (c:StoreCheckpoint"
                    " {workspace_id: $workspace_id, source_id: $source_id})"
                    " SET c.version = $version, c.epoch = $epoch,"
                    " c.updated_at = datetime($now)"
                ),
                {
                    "workspace_id": str(workspace_id),
                    "source_id": str(source_id),
                    "version": version,
                    "epoch": epoch,
                    "now": now,
                },
            )

        entities_end_dated = 0
        edges_end_dated = 0
        if not bitemporal_enabled:
            await tx.run(
                _DELETE_BY_SOURCE_CYPHER,
                {_WRITE_WORKSPACE_PARAM: str(workspace_id), "source_id": str(source_id)},
            )
        elif snapshot_at_iso is not None:
            # Snapshot-replace: end-date only entities that are NOT in the
            # incoming batch (i.e. genuinely removed).  Entities present in the
            # batch are versioned by the per-entity upsert below, so excluding
            # them here keeps a re-ingest of the same snapshot idempotent
            # (otherwise they would be closed and never re-opened).
            batch_entity_ids = [str(e.id) for e in entities]
            ed_result = await tx.run(
                _END_DATE_BY_SOURCE_CYPHER,
                {
                    _WRITE_WORKSPACE_PARAM: str(workspace_id),
                    "source_id": str(source_id),
                    "now": snapshot_at_iso,
                    "batch_entity_ids": batch_entity_ids,
                },
            )
            ed_record = await ed_result.single()
            if ed_record is not None:
                entities_end_dated = int(ed_record.get("entities_end_dated", 0) or 0)
                edges_end_dated = int(ed_record.get("edges_end_dated", 0) or 0)

        entity_cypher = (
            _UPSERT_ENTITY_BITEMPORAL_CYPHER if bitemporal_enabled else _UPSERT_ENTITY_CYPHER
        )

        name_to_id: dict[str, uuid.UUID] = {}
        for ext_ent in entities:
            params = _entity_to_params(ext_ent, source_id, workspace_id, now)
            if bitemporal_enabled:
                params["state_fingerprint"] = _entity_state_fingerprint(params)
            _serialise_metadata_param(params)
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
                target_name = getattr(ext_edge, "target_name", None)
                if not target_name:
                    continue
                edge_type = str(getattr(ext_edge, "edge_type", "calls"))
                stub_params = {
                    "workspace_id": str(workspace_id),
                    "source_id_ent": str(ext_edge.source_entity_id),  # duck-typed
                    "target_name": str(target_name),
                    "generated_id": str(uuid.uuid4()),
                    "source_id": str(source_id),
                    "edge_type": edge_type,
                    "metadata": _coerce_metadata(getattr(ext_edge, "metadata", None)),
                    "now": now,
                }
                _serialise_metadata_param(stub_params)
                rendered = _UPSERT_EDGE_BY_NAME_CYPHER_TEMPLATE.replace("{edge_type}", edge_type)
                await tx.run(rendered, stub_params)
                continue

            if bitemporal_enabled:
                edge_params["state_fingerprint"] = _edge_state_fingerprint(edge_params)
            _serialise_metadata_param(edge_params)
            rendered = edge_template.replace("{edge_type}", str(edge_params["edge_type"]))
            await tx.run(rendered, edge_params)

        return entities_end_dated, edges_end_dated

    async def upsert_entity(
        self,
        *,
        entity: EntityUpsert,
        workspace_id: uuid.UUID,
    ) -> None:
        """Upsert a single entity within ``workspace_id`` (idempotent)."""
        now = datetime.now(UTC).isoformat()
        metadata = _coerce_metadata(entity.metadata)
        params: dict[str, Any] = {
            _WRITE_WORKSPACE_PARAM: str(workspace_id),
            "id": str(entity.id),
            "source_id": str(entity.source_id),
            "entity_type": entity.entity_type,
            "name": entity.name,
            "display_name": entity.display_name,
            "chunk_id": (str(entity.chunk_id) if entity.chunk_id else None),
            # First-class indexed cluster property (Gap A) promoted from
            # metadata so list_entities matches by cluster via index seek.
            "cluster": _cluster_from_metadata(metadata),
            "metadata": metadata,
            # AP2 — per-entity anti-entropy hash projected to Neo4j node property.
            "content_hash": (
                str(entity.content_hash) if entity.content_hash is not None else None
            ),
            "now": now,
        }
        cypher = self._select_entity_upsert_cypher(params)

        async def _run(tx: Any) -> None:
            entity_version = getattr(entity, "version", None)
            forced_replay = getattr(entity, "forced_replay", False)
            if entity_version is not None:
                # ----------------------------------------------------------------
                # Source-level checkpoint fast-path (coarse guard).
                # Skip if the source checkpoint is already at or beyond this version
                # (unless epoch supersedes or forced_replay bypasses).
                # ----------------------------------------------------------------
                res = await tx.run(
                    (
                        "MATCH (c:StoreCheckpoint"
                        " {workspace_id: $workspace_id, source_id: $source_id})"
                        " RETURN c.version AS version, c.epoch AS epoch"
                    ),
                    {"workspace_id": str(workspace_id), "source_id": str(entity.source_id)},
                )
                record = await res.single()

                existing_version = record["version"] if record is not None else None
                existing_epoch = record["epoch"] if record is not None else None

                should_skip = False
                if existing_version is not None and existing_version >= entity_version:
                    should_skip = True

                ep = getattr(entity, "epoch", None)
                if ep is not None and existing_epoch is not None and ep > existing_epoch:
                    should_skip = False

                if forced_replay:
                    should_skip = False

                if should_skip:
                    return
                await tx.run(
                    (
                        "MERGE (c:StoreCheckpoint"
                        " {workspace_id: $workspace_id, source_id: $source_id})"
                        " SET c.version = $version, c.epoch = $epoch,"
                        " c.updated_at = datetime($now)"
                    ),
                    {
                        "workspace_id": str(workspace_id),
                        "source_id": str(entity.source_id),
                        "version": entity_version,
                        "epoch": ep,
                        "now": now,
                    },
                )

                # ----------------------------------------------------------------
                # Per-entity CAS Cypher (AP1 — authoritative fine-grained guard).
                # Runs AFTER the checkpoint is advanced so a concurrent writer
                # that races past the source checkpoint still hits this guard.
                # $forced=True lets an admin forced-replay overwrite a higher node
                # version (matches the source-level bypass above).
                # ----------------------------------------------------------------
                cas_cypher = (
                    _UPSERT_ENTITY_BITEMPORAL_CAS_CYPHER
                    if self._bitemporal_enabled
                    else _UPSERT_ENTITY_CAS_CYPHER
                )
                cas_params = dict(params)
                cas_params["incoming_version"] = entity_version
                cas_params["forced"] = forced_replay
                if self._bitemporal_enabled and "state_fingerprint" not in cas_params:
                    cas_params["state_fingerprint"] = _entity_state_fingerprint(cas_params)
                await tx.run(cas_cypher, cas_params)
                return

            # version=None → unconditional legacy/test path (no CAS guard).
            await tx.run(cypher, params)

        async with self._driver.session(database=self._config.database) as session:
            await session.execute_write(_run)

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
        rendered = self._select_edge_upsert_cypher(params, edge_type)

        async def _run(tx: Any) -> None:
            if getattr(edge, "version", None) is not None and edge.metadata.get("source_id"):
                source_id = str(edge.metadata["source_id"])
                res = await tx.run(
                    (
                        "MATCH (c:StoreCheckpoint"
                        " {workspace_id: $workspace_id, source_id: $source_id})"
                        " RETURN c.version AS version, c.epoch AS epoch"
                    ),
                    {"workspace_id": str(workspace_id), "source_id": source_id},
                )
                record = await res.single()

                existing_version = record["version"] if record is not None else None
                existing_epoch = record["epoch"] if record is not None else None

                should_skip = False
                if existing_version is not None and existing_version >= edge.version:
                    should_skip = True

                ep = getattr(edge, "epoch", None)
                if ep is not None and existing_epoch is not None and ep > existing_epoch:
                    should_skip = False

                fr = getattr(edge, "forced_replay", False)
                if fr:
                    should_skip = False

                if should_skip:
                    return
                await tx.run(
                    (
                        "MERGE (c:StoreCheckpoint"
                        " {workspace_id: $workspace_id, source_id: $source_id})"
                        " SET c.version = $version, c.epoch = $epoch,"
                        " c.updated_at = datetime($now)"
                    ),
                    {
                        "workspace_id": str(workspace_id),
                        "source_id": source_id,
                        "version": edge.version,
                        "epoch": ep,
                        "now": now,
                    },
                )
            await tx.run(rendered, params)

        async with self._driver.session(database=self._config.database) as session:
            await session.execute_write(_run)

    async def upsert_edge_by_name(
        self,
        *,
        source_entity_id: uuid.UUID,
        target_name: str,
        edge_type: str,
        workspace_id: uuid.UUID,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Create an edge to a target identified by name (creates a stub if missing)."""
        now = datetime.now(UTC).isoformat()
        params: dict[str, Any] = {
            "workspace_id": str(workspace_id),
            "source_id_ent": str(source_entity_id),
            "target_name": target_name,
            "generated_id": str(uuid.uuid4()),
            "source_id": (metadata.get("source_id") if metadata else ""),
            "edge_type": edge_type,
            "metadata": _coerce_metadata(metadata),
            "now": now,
        }
        _serialise_metadata_param(params)
        rendered = _UPSERT_EDGE_BY_NAME_CYPHER_TEMPLATE.replace("{edge_type}", edge_type)
        async with self._driver.session(database=self._config.database) as session:
            await session.execute_write(_run_write_stmt, rendered, params)

    def _select_entity_upsert_cypher(self, params: dict[str, Any]) -> str:
        """Pick legacy or bitemporal entity upsert Cypher; mutate params if needed."""
        if not self._bitemporal_enabled:
            _serialise_metadata_param(params)
            return _UPSERT_ENTITY_CYPHER
        params["state_fingerprint"] = _entity_state_fingerprint(params)
        _serialise_metadata_param(params)
        return _UPSERT_ENTITY_BITEMPORAL_CYPHER

    def _select_edge_upsert_cypher(self, params: dict[str, Any], edge_type: str) -> str:
        """Pick legacy or bitemporal edge upsert Cypher; render edge-type slot."""
        if not self._bitemporal_enabled:
            _serialise_metadata_param(params)
            return _UPSERT_EDGE_CYPHER_TEMPLATE.replace("{edge_type}", edge_type)
        params["state_fingerprint"] = _edge_state_fingerprint(params)
        _serialise_metadata_param(params)
        return _UPSERT_EDGE_BITEMPORAL_CYPHER_TEMPLATE.replace("{edge_type}", edge_type)

    async def delete_tombstoned(self, *, workspace_id: uuid.UUID | None = None) -> int:
        """Process tombstoned entities; behaviour depends on the bitemporal flag."""
        if not self._bitemporal_enabled:
            async with self._driver.session(database=self._config.database) as session:
                rows = await session.execute_write(
                    _run_write_returning, _DELETE_TOMBSTONED_CYPHER, {}
                )
            if not rows:
                return 0
            return int(rows[0].get("deleted", 0))
        if workspace_id is None:
            raise ValueError(
                "Neo4jGraphStore.delete_tombstoned requires workspace_id when "
                "bitemporal_enabled=True; cross-workspace tombstone end-dating "
                "is forbidden (ADR-0008 §Consequences-security #1)."
            )
        now_iso = datetime.now(UTC).isoformat()
        params: dict[str, Any] = {
            _WORKSPACE_PARAM: str(workspace_id),
            "now": now_iso,
        }
        async with self._driver.session(database=self._config.database) as session:
            rows = await session.execute_write(
                _run_write_returning, _END_DATE_TOMBSTONED_CYPHER, params
            )
        entities_end_dated = int(rows[0].get("entities_end_dated", 0)) if rows else 0
        edges_end_dated = int(rows[0].get("edges_end_dated", 0)) if rows else 0
        if entities_end_dated:
            GRAPH_END_DATED_TOTAL.labels(kind="entity", reason="tombstone").inc(entities_end_dated)
        if edges_end_dated:
            GRAPH_END_DATED_TOTAL.labels(kind="edge", reason="tombstone").inc(edges_end_dated)
        log.info(
            "neo4j_end_date_tombstoned",
            workspace_id=str(workspace_id),
            entities_end_dated=entities_end_dated,
            edges_end_dated=edges_end_dated,
        )
        return entities_end_dated

    async def delete_tombstoned_graph(self, *, workspace_id: uuid.UUID | None = None) -> int:
        """Protocol alias for :meth: (AP1 rename).

        delete_tombstoned_graph is the canonical name on the GraphStore
        protocol to avoid collision with VectorStore.delete_tombstoned.
        Delegates to the existing implementation unchanged.
        """
        return await self.delete_tombstoned(workspace_id=workspace_id)

    # ------------------------------------------------------------------
    # Bitemporal backfill (ADR-0008 §8 phase 1, issue #130)
    # ------------------------------------------------------------------

    async def backfill_bitemporal(
        self,
        *,
        workspace_id: uuid.UUID,
        batch_size: int = BACKFILL_DEFAULT_BATCH_SIZE,
    ) -> int:
        """Populate the bitemporal triple on legacy nodes/edges in one workspace."""
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
        """Loop one phase's Cypher until a chunk modifies zero rows."""
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

    async def count_hot_to_warm_eligible(
        self,
        *,
        workspace_id: uuid.UUID,
        hot_cutoff: datetime,
    ) -> tuple[int, int]:
        """Return (entity_state_count, edge_count) eligible for hot->warm."""
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
        """Return the oldest eligible ``recorded_at`` or ``None``."""
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
        """Tag eligible :EntityState rows + edges with ``tier_pending='warm'``."""
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
        """Project marked rows to :EntitySnapshot:Daily / :RelationshipSnapshot:Daily."""
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
        """Return (entity_rows, edge_rows) for one (workspace_id, date) snapshot."""
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
        """Hard-delete warm rows for (workspace_id, snapshot_date)."""
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

    async def resolve_pending_stubs(self, *, workspace_id: uuid.UUID) -> int:
        """Merge stub nodes with real entities in the same workspace."""
        params = {
            "workspace_id": str(workspace_id),
            "batch_size": 100,
        }
        total_resolved = 0
        async with self._driver.session(database=self._config.database) as session:
            while True:
                rows = await session.execute_write(
                    _run_write_returning, _RESOLVE_STUBS_CYPHER, params
                )
                count = int(rows[0].get("resolved", 0)) if rows else 0
                if count == 0:
                    break
                total_resolved += count
        return total_resolved

    async def count_records_by_tier(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[str, int]:
        """Return {'hot': int, 'warm': int} record counts for the workspace."""
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
        as_of: datetime | None = None,
    ) -> EntityNodeView | None:
        """Resolve an entity by fully-qualified name, within workspace."""
        params: dict[str, Any] = {
            _WORKSPACE_PARAM: str(workspace_id),
            "entity_name": entity_name,
        }
        if as_of is None:
            cypher = _GET_ENTITY_BY_NAME_CYPHER
        else:
            cypher = _GET_ENTITY_BY_NAME_AS_OF_CYPHER
            params["as_of"] = _as_of_to_param(as_of)
        async with self._driver.session(database=self._config.database) as session:
            rows = await session.execute_read(_run_read_stmt, cypher, params)
        if not rows:
            return None
        return _entity_record_to_view(rows[0])

    async def list_entities(
        self,
        *,
        workspace_id: uuid.UUID,
        kind: str,
        cluster: str | None = None,
        name: str | None = None,
        as_of: datetime | None = None,
    ) -> list[EntityNodeView]:
        """List entities of ``kind`` within ``workspace_id`` (Gap B, #310).

        Filters by the first-class, indexed ``cluster`` property (Gap A)
        and/or exact ``name`` when supplied — both optional.  Answers
        "which StorageClasses exist in cluster X" and "does a StorageClass
        named 'gold' exist in cluster X" with an index seek on
        ``(workspace_id, kind, cluster)``, never a ``metadata CONTAINS``
        substring scan.

        ``as_of`` (ADR-0008 §5) returns the ``:EntityState`` version valid
        at T; ``None`` reads the still-current ``:Entity`` mirror.  The
        workspace predicate always leads (ADR-0008 §Consequences-security
        #1) and the bitemporal predicate composes on top — never replaces
        it.
        """
        cypher = _build_list_entities_cypher(
            has_cluster=cluster is not None,
            has_name=name is not None,
            as_of=as_of is not None,
        )
        params: dict[str, Any] = {
            _WORKSPACE_PARAM: str(workspace_id),
            "kind": kind,
        }
        if cluster is not None:
            params["cluster"] = cluster
        if name is not None:
            params["name"] = name
        if as_of is not None:
            params["as_of"] = _as_of_to_param(as_of)
        async with self._driver.session(database=self._config.database) as session:
            rows = await session.execute_read(_run_read_stmt, cypher, params)
        return [_entity_record_to_view(r) for r in rows]

    async def find_related(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        """BFS traversal from a seed, scoped to ``workspace_id``."""
        clamped = _clamp_depth(max_depth, _MAX_DEPTH_CEILING)
        validated_types = _validate_edge_types(edge_types)
        cypher = _build_traverse_cypher(clamped, validated_types, as_of=as_of is not None)
        params: dict[str, Any] = {
            _WORKSPACE_PARAM: str(workspace_id),
            "entity_name": entity_name,
        }
        if as_of is not None:
            params["as_of"] = _as_of_to_param(as_of)
        async with self._driver.session(database=self._config.database) as session:
            rows = await session.execute_read(_run_read_stmt, cypher, params)

        if not rows:
            raise ValueError(f"entity_not_found:{entity_name}")
        return _rows_to_graph_result(rows[0])

    async def traverse(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        """Alias of :meth:`find_related` per the protocol contract."""
        return await self.find_related(
            entity_name=entity_name,
            workspace_id=workspace_id,
            as_of=as_of,
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

    async def find_entities_by_metadata(
        self,
        *,
        workspace_id: uuid.UUID,
        key: str,
        value: Any,
    ) -> list[EntityNodeView]:
        """Find entities where metadata[key] == value within workspace."""
        cypher = f"""
        MATCH (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id}})
        WHERE n.metadata CONTAINS $query_part
        RETURN n.name AS name,
               n.kind AS kind,
               n.source_id AS source_id,
               n.chunk_text AS chunk_text,
               n.valid_from AS valid_from,
               n.valid_to AS valid_to,
               n.recorded_at AS recorded_at
        """
        params = {
            "workspace_id": str(workspace_id),
            "query_part": f'"{key}":"{value}"',
        }
        async with self._driver.session(database=self._config.database) as session:
            rows = await session.execute_read(_run_read_stmt, cypher, params)
        return [_entity_record_to_view(r) for r in rows]

    async def get_all_entities(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> list[EntityNodeView]:
        """Return all entities for a workspace (use with caution)."""
        cypher = f"""
        MATCH (n:{_ENTITY_LABEL} {{workspace_id: $workspace_id}})
        RETURN n.id AS id,
               n.name AS name,
               n.kind AS kind,
               n.source_id AS source_id,
               n.chunk_text AS chunk_text,
               n.valid_from AS valid_from,
               n.valid_to AS valid_to,
               n.recorded_at AS recorded_at,
               n.metadata AS metadata
        """
        params = {"workspace_id": str(workspace_id)}
        async with self._driver.session(database=self._config.database) as session:
            rows = await session.execute_read(_run_read_stmt, cypher, params)
        return [_entity_record_to_view(r) for r in rows]

    async def merge_nodes(
        self,
        *,
        workspace_id: uuid.UUID,
        source_id: uuid.UUID,
        target_id: uuid.UUID,
    ) -> bool:
        """Merge a source node into a target node reversibly."""
        params = {
            "workspace_id": str(workspace_id),
            "source_id": str(source_id),
            "target_id": str(target_id),
        }

        async with self._driver.session(database=self._config.database) as session:
            exist_check_cypher = """
            MATCH (a:`Entity` {workspace_id: $workspace_id, id: $source_id})
            MATCH (b:`Entity` {workspace_id: $workspace_id, id: $target_id})
            RETURN a.id AS aid, b.id AS bid
            """
            rows = await session.execute_read(_run_read_stmt, exist_check_cypher, params)
            if not rows:
                return False

            async def _merge_tx(tx: Any) -> bool:
                merge_rel_cypher = """
                MATCH (a:`Entity` {workspace_id: $workspace_id, id: $source_id})
                MATCH (b:`Entity` {workspace_id: $workspace_id, id: $target_id})
                MERGE (a)-[r:MERGED_INTO {workspace_id: $workspace_id}]->(b)
                SET a.is_merged = true, a.merged_into = $target_id
                """
                await tx.run(merge_rel_cypher, params)

                inc_cypher = """
                MATCH (x)-[r]->(a:`Entity` {workspace_id: $workspace_id, id: $source_id})
                WHERE r.workspace_id = $workspace_id
                  AND type(r) <> 'MERGED_INTO'
                  AND type(r) <> 'HAD_STATE'
                RETURN id(r) AS rid, type(r) AS rtype, properties(r) AS rprops, x.id AS other_id
                """
                inc_res = await tx.run(inc_cypher, params)
                inc_records = [rec async for rec in inc_res]

                out_cypher = """
                MATCH (a:`Entity` {workspace_id: $workspace_id, id: $source_id})-[r]->(y)
                WHERE r.workspace_id = $workspace_id
                  AND type(r) <> 'MERGED_INTO'
                  AND type(r) <> 'HAD_STATE'
                RETURN id(r) AS rid, type(r) AS rtype, properties(r) AS rprops, y.id AS other_id
                """
                out_res = await tx.run(out_cypher, params)
                out_records = [rec async for rec in out_res]

                for rec in inc_records:
                    rtype = rec["rtype"]
                    other_id = rec["other_id"]
                    rprops = dict(rec["rprops"])
                    rprops["original_node_id"] = str(source_id)
                    redirect_inc_cypher = f"""
                    MATCH (x {{workspace_id: $workspace_id, id: $other_id}})
                    MATCH (b:`Entity` {{workspace_id: $workspace_id, id: $target_id}})
                    CREATE (x)-[r2:`{rtype}` {{workspace_id: $workspace_id}}]->(b)
                    SET r2 = $rprops
                    """
                    await tx.run(
                        redirect_inc_cypher,
                        {
                            "workspace_id": str(workspace_id),
                            "other_id": str(other_id),
                            "target_id": str(target_id),
                            "rprops": rprops,
                        },
                    )

                for rec in out_records:
                    rtype = rec["rtype"]
                    other_id = rec["other_id"]
                    rprops = dict(rec["rprops"])
                    rprops["original_node_id"] = str(source_id)
                    redirect_out_cypher = f"""
                    MATCH (b:`Entity` {{workspace_id: $workspace_id, id: $target_id}})
                    MATCH (y {{workspace_id: $workspace_id, id: $other_id}})
                    CREATE (b)-[r2:`{rtype}` {{workspace_id: $workspace_id}}]->(y)
                    SET r2 = $rprops
                    """
                    await tx.run(
                        redirect_out_cypher,
                        {
                            "workspace_id": str(workspace_id),
                            "other_id": str(other_id),
                            "target_id": str(target_id),
                            "rprops": rprops,
                        },
                    )

                del_cypher = """
                MATCH (a:`Entity` {workspace_id: $workspace_id, id: $source_id})-[r]-()
                WHERE r.workspace_id = $workspace_id
                  AND type(r) <> 'MERGED_INTO'
                  AND type(r) <> 'HAD_STATE'
                DELETE r
                """
                await tx.run(del_cypher, params)
                return True

            await session.execute_write(_merge_tx)
            return True

    async def unmerge_node(
        self,
        *,
        workspace_id: uuid.UUID,
        merged_node_id: uuid.UUID,
    ) -> bool:
        """Split/unmerge a previously merged node back to its original identity."""
        params = {
            "workspace_id": str(workspace_id),
            "merged_node_id": str(merged_node_id),
        }
        async with self._driver.session(database=self._config.database) as session:
            check_cypher = """
            MATCH (a:`Entity` {workspace_id: $workspace_id, id: $merged_node_id})
            WHERE a.is_merged = true
            RETURN a.merged_into AS target_id
            """
            rows = await session.execute_read(_run_read_stmt, check_cypher, params)
            if not rows or not rows[0].get("target_id"):
                return False

            target_id = rows[0]["target_id"]
            params["target_id"] = str(target_id)

            async def _unmerge_tx(tx: Any) -> bool:
                inc_cypher = """
                MATCH (x)-[r]->(b:`Entity` {workspace_id: $workspace_id, id: $target_id})
                WHERE r.workspace_id = $workspace_id AND r.original_node_id = $merged_node_id
                RETURN id(r) AS rid, type(r) AS rtype, properties(r) AS rprops, x.id AS other_id
                """
                inc_res = await tx.run(inc_cypher, params)
                inc_records = [rec async for rec in inc_res]

                out_cypher = """
                MATCH (b:`Entity` {workspace_id: $workspace_id, id: $target_id})-[r]->(y)
                WHERE r.workspace_id = $workspace_id AND r.original_node_id = $merged_node_id
                RETURN id(r) AS rid, type(r) AS rtype, properties(r) AS rprops, y.id AS other_id
                """
                out_res = await tx.run(out_cypher, params)
                out_records = [rec async for rec in out_res]

                for rec in inc_records:
                    rtype = rec["rtype"]
                    other_id = rec["other_id"]
                    rprops = dict(rec["rprops"])
                    rprops.pop("original_node_id", None)
                    restore_inc_cypher = f"""
                    MATCH (x {{workspace_id: $workspace_id, id: $other_id}})
                    MATCH (a:`Entity` {{workspace_id: $workspace_id, id: $merged_node_id}})
                    CREATE (x)-[r2:`{rtype}` {{workspace_id: $workspace_id}}]->(a)
                    SET r2 = $rprops
                    """
                    await tx.run(
                        restore_inc_cypher,
                        {
                            "workspace_id": str(workspace_id),
                            "other_id": str(other_id),
                            "merged_node_id": str(merged_node_id),
                            "rprops": rprops,
                        },
                    )

                for rec in out_records:
                    rtype = rec["rtype"]
                    other_id = rec["other_id"]
                    rprops = dict(rec["rprops"])
                    rprops.pop("original_node_id", None)
                    restore_out_cypher = f"""
                    MATCH (a:`Entity` {{workspace_id: $workspace_id, id: $merged_node_id}})
                    MATCH (y {{workspace_id: $workspace_id, id: $other_id}})
                    CREATE (a)-[r2:`{rtype}` {{workspace_id: $workspace_id}}]->(y)
                    SET r2 = $rprops
                    """
                    await tx.run(
                        restore_out_cypher,
                        {
                            "workspace_id": str(workspace_id),
                            "other_id": str(other_id),
                            "merged_node_id": str(merged_node_id),
                            "rprops": rprops,
                        },
                    )

                del_cypher = """
                MATCH (b:`Entity` {workspace_id: $workspace_id, id: $target_id})-[r]-()
                WHERE r.workspace_id = $workspace_id AND r.original_node_id = $merged_node_id
                DELETE r
                """
                await tx.run(del_cypher, params)

                del_merge_rel = """
                MATCH (a:`Entity` {workspace_id: $workspace_id, id: $merged_node_id})
                      -[r:MERGED_INTO]->
                      (b:`Entity` {workspace_id: $workspace_id, id: $target_id})
                DELETE r
                """
                await tx.run(del_merge_rel, params)

                reset_props = """
                MATCH (a:`Entity` {workspace_id: $workspace_id, id: $merged_node_id})
                REMOVE a.is_merged, a.merged_into
                """
                await tx.run(reset_props, params)
                return True

            await session.execute_write(_unmerge_tx)
            return True

    async def get_entity_versions(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[uuid.UUID, int]:
        """Return {entity_id: version} for all entities in this workspace.

        Used by the reconcile worker (AP2) for per-entity version drift
        detection.  Entities absent from the result have version 0 in the
        graph and must be re-upserted.
        """
        cypher = (
            "MATCH (e:Entity {workspace_id: $workspace_id}) "
            "WHERE e.version IS NOT NULL "
            "RETURN e.id AS entity_id, e.version AS version"
        )
        params = {"workspace_id": str(workspace_id)}
        async with self._driver.session(database=self._config.database) as session:
            rows = await session.execute_read(_run_read_stmt, cypher, params)
        result: dict[uuid.UUID, int] = {}
        for row in rows:
            raw_id = row.get("entity_id")
            raw_ver = row.get("version")
            if raw_id is not None and raw_ver is not None:
                result[uuid.UUID(str(raw_id))] = int(raw_ver)
        return result

    async def get_entity_content_hashes(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[uuid.UUID, str]:
        """Return {entity_id: content_hash} for all entities with a hash in this workspace.

        AP2 — per-entity anti-entropy: used by the reconcile worker to detect
        same-version content drift (e.g. a graph node whose content_hash was
        corrupted or manually edited).  Entities with no ``content_hash``
        property are omitted; the reconcile worker treats them as "not yet
        hashed" and skips hash comparison for those rows.
        """
        cypher = (
            "MATCH (e:Entity {workspace_id: $workspace_id}) "
            "WHERE e.content_hash IS NOT NULL "
            "RETURN e.id AS entity_id, e.content_hash AS content_hash"
        )
        params = {"workspace_id": str(workspace_id)}
        async with self._driver.session(database=self._config.database) as session:
            rows = await session.execute_read(_run_read_stmt, cypher, params)
        result: dict[uuid.UUID, str] = {}
        for row in rows:
            raw_id = row.get("entity_id")
            raw_hash = row.get("content_hash")
            if raw_id is not None and raw_hash is not None:
                result[uuid.UUID(str(raw_id))] = str(raw_hash)
        return result
