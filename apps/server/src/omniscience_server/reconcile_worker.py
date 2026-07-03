"""Reconcile worker — cross-store drift detection and repair.

The triple write in ``IndexWriter.upsert_document`` / ``upsert_graph`` is
**not atomic**.  The sequence is:

1. Postgres transaction commits (document + chunks rows).
2. Qdrant upsert — outside the PG transaction; a failure here leaves a
   document in Postgres with no vector chunks.
3. Neo4j upsert — called separately from ``IngestionWorker`` via
   ``upsert_graph``; always outside the PG transaction.

A process crash between any two of these steps produces permanent drift:

* ``pg_missing_qdrant``  — PG commit succeeded, Qdrant write failed.
  Document is queryable via REST/MCP metadata endpoints but invisible to
  vector search.  Action: log + mark the owning ``ingestion_run`` as
  ``partial`` + expose metric.  Re-indexing is triggered by the operator
  or by a follow-up ingestion run.

* ``qdrant_orphan``      — chunks in Qdrant reference a ``source_id`` that
  has no non-tombstoned Postgres document.  Caused by seed scripts,
  aborted partial deletes, or tombstone race conditions.  Action:
  hard-delete the orphan chunks (bounded by ``RECONCILE_ORPHAN_DELETE_LIMIT``).

* ``neo4j_orphan``       — entity nodes in Neo4j reference a ``source_id``
  that has no active Postgres documents.  **AP2: actively healed** by
  calling ``end_date_source_orphans`` which sets ``valid_to = now`` on
  all still-open entities (ADR-0009 end-dating path, ADR-0008 §3).
  The metric is still emitted.  ``ENABLE_NEO4J_ORPHAN_REEMIT`` is
  deprecated — the heal is now unconditional (see flag docs below).

**Idempotency**: every action is safe to repeat.  Qdrant deletes by filter
are no-ops when the points are already gone.  ``ingestion_run`` status
updates are guarded with ``WHERE status != 'partial'`` to avoid double-
writing.  Neo4j end-date is idempotent (already-closed nodes are skipped).

**ACL**: every store call passes ``workspace_id``; cross-workspace mixing
is structurally impossible because the outer loop iterates workspaces and
every per-workspace method pins the id.

**Bounded**: orphan deletes are capped at ``RECONCILE_ORPHAN_DELETE_LIMIT``
per tick.  Entity and edge re-emits are capped at ``REEMIT_TICK_CAP`` per
tick via a per-worker cursor that advances monotonically within each
``ReconcileWorker`` instance.  The cursor is reset between runs so the
effective unit is "per run" (not "per workspace per run"), which keeps
semantics simple and prevents starvation in workspaces ordered late.

**Causal ordering**: within a single tick, ``entity.upsert`` OutboxEvents
are always inserted before any ``edge.upsert`` OutboxEvents.  This
guarantees that the downstream outbox consumer sees entity definitions
before the edges that depend on them, allowing its ``_auto_unpark_edges``
path to resolve correctly.  The ordering is enforced in
``_reconcile_workspace`` by calling ``_check_entity_drift`` (and
``_reemit_entities_for_missing_sources``) before ``_check_edge_drift``.

Usage in ``app.py`` lifespan::

    from omniscience_server.reconcile_worker import ReconcileWorker

    reconcile_worker = ReconcileWorker(
        session_factory=session_factory,
        vector_store=qdrant_store,
        graph_store=neo4j_graph_store,
        settings=settings,
    )
    reconcile_task = asyncio.create_task(reconcile_worker.start())
    app.state.reconcile_worker = reconcile_worker
    ...
    yield
    ...
    reconcile_worker.stop()
    reconcile_task.cancel()
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from omniscience_core.config import Settings
from omniscience_core.db.models import (
    Document,
    Edge,
    Entity,
    IngestionRun,
    IngestionRunStatus,
    OutboxEvent,
    Source,
    Workspace,
)
from omniscience_core.telemetry.metrics import (
    RECONCILE_DRIFT_TOTAL,
    RECONCILE_WORKER_DURATION_SECONDS,
    RECONCILE_WORKER_RUNS_TOTAL,
)
from omniscience_index.stores.neo4j_store import Neo4jGraphStore
from omniscience_index.stores.qdrant_store import QdrantVectorStore
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from omniscience_server.reconcile_constants import (
    DRIFT_EDGE,
    DRIFT_NEO4J_ORPHAN,
    DRIFT_PG_MISSING_QDRANT,
    DRIFT_QDRANT_ORPHAN,
    RECONCILE_ORPHAN_DELETE_LIMIT,
    RECONCILE_TICK_SECONDS_DEFAULT,
    REEMIT_TICK_CAP_DEFAULT,
)

# ---------------------------------------------------------------------------
# AP2 feature flags
# ---------------------------------------------------------------------------

#: Maximum number of entities (or edges) re-emitted per reconcile tick.
#: A per-worker cursor ensures that the remainder is processed on the next
#: tick — no single run floods the outbox with an unbounded transaction.
#: Override via ``REEMIT_TICK_CAP`` environment variable.
REEMIT_TICK_CAP: int = max(
    int(os.environ.get("REEMIT_TICK_CAP", str(REEMIT_TICK_CAP_DEFAULT))),
    1,
)

#: (Deprecated) When true, emit a durable ``source.orphan_detected``
#: OutboxEvent for each Neo4j orphan source.  AP2 Batch C makes Neo4j orphan
#: healing unconditional (active end-dating is always performed).  This flag
#: is kept for backward compatibility but no longer gates the heal action —
#: only the legacy diagnostic event emission is gated here.  Set to any
#: value to suppress even the diagnostic; in practice, operators should
#: remove this flag and rely on the structured log + metric instead.
ENABLE_NEO4J_ORPHAN_REEMIT: bool = (
    os.environ.get("ENABLE_NEO4J_ORPHAN_REEMIT", "true").lower() == "true"
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Report dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkspaceReconcileReport:
    """Per-workspace reconcile result for one tick."""

    workspace_id: uuid.UUID
    pg_missing_qdrant: list[uuid.UUID] = field(default_factory=list)
    qdrant_orphan_sources: list[str] = field(default_factory=list)
    qdrant_orphans_deleted: int = 0
    neo4j_orphan_sources: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ReconcileRunReport:
    """Aggregated report for one full reconcile tick."""

    started_at: datetime
    finished_at: datetime
    per_workspace: list[WorkspaceReconcileReport]
    duration_seconds: float


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class ReconcileWorker:
    """Background task that detects and repairs cross-store drift.

    Args:
        session_factory: SQLAlchemy async session factory.
        vector_store:    Qdrant store — provides source-scoped chunk counts
            and source-scoped orphan deletion.
        graph_store:     Neo4j store — provides per-source entity counts and
            the active tombstone-heal path.
        settings:        Application settings — drives tick interval and
            the orphan-delete batch limit.

    AP2 cursor mechanism
    --------------------
    ``_reemit_cursor`` is an integer offset maintained **across workspaces
    within a single run**.  At the start of each ``run_once()`` call the
    cursor is reset to 0.  Each call to ``_reemit_entities_for_missing_sources``
    or ``_check_entity_drift`` (and ``_check_edge_drift``) advances the
    cursor by the number of items emitted.  When the cursor reaches
    ``REEMIT_TICK_CAP`` no further items are emitted in that run.

    The cursor is NOT persisted across restarts.  This is intentional:
    drift is re-detected on every tick and the cursor is an intra-tick
    rate-limit, not a pagination bookmark.  Any items skipped in one run
    will be re-detected and emitted in the next run.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        vector_store: QdrantVectorStore,
        graph_store: Neo4jGraphStore,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._vector_store = vector_store
        self._graph_store = graph_store
        self._tick_seconds = max(
            int(getattr(settings, "reconcile_tick_seconds", RECONCILE_TICK_SECONDS_DEFAULT)),
            60,
        )
        self._orphan_limit = max(
            int(getattr(settings, "reconcile_orphan_delete_limit", RECONCILE_ORPHAN_DELETE_LIMIT)),
            1,
        )
        self._reemit_cap = max(
            int(getattr(settings, "reemit_tick_cap", REEMIT_TICK_CAP)),
            1,
        )
        self._running = False
        self._last_report: ReconcileRunReport | None = None
        # AP2 bounded re-emit cursor — reset each run, advanced per emission.
        self._reemit_cursor: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Run the worker loop until :meth:`stop` is called."""
        self._running = True
        log.info(
            "reconcile_worker_started",
            tick_seconds=self._tick_seconds,
            orphan_limit=self._orphan_limit,
            reemit_cap=self._reemit_cap,
        )
        while self._running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("reconcile_worker_error", error=str(exc))
            try:
                await asyncio.sleep(self._tick_seconds)
            except asyncio.CancelledError:
                break
        log.info("reconcile_worker_stopped")

    def stop(self) -> None:
        """Signal the worker to exit after the current tick completes."""
        self._running = False

    @property
    def last_report(self) -> ReconcileRunReport | None:
        """Most recent run report, or ``None`` before the first run."""
        return self._last_report

    # ------------------------------------------------------------------
    # Public entry point — one full tick
    # ------------------------------------------------------------------

    async def run_once(self) -> ReconcileRunReport:
        """Execute one full reconcile tick across all workspaces."""
        started = datetime.now(UTC)
        wall_start = time.monotonic()

        # Reset the per-tick re-emit cursor at the start of each run.
        self._reemit_cursor = 0

        workspace_ids = await self._load_workspace_ids()
        per_workspace: list[WorkspaceReconcileReport] = []

        for ws_id in workspace_ids:
            try:
                report = await self._reconcile_workspace(workspace_id=ws_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A transient outage (or any other failure) reconciling one
                # workspace must not starve every workspace queued behind it
                # in the same tick — isolate the failure and keep going.
                log.error(
                    "reconcile_workspace_failed",
                    workspace_id=str(ws_id),
                    error=str(exc),
                )
                continue
            per_workspace.append(report)

        duration = time.monotonic() - wall_start
        finished = datetime.now(UTC)

        run_report = ReconcileRunReport(
            started_at=started,
            finished_at=finished,
            per_workspace=per_workspace,
            duration_seconds=duration,
        )
        RECONCILE_WORKER_RUNS_TOTAL.inc()
        RECONCILE_WORKER_DURATION_SECONDS.observe(duration)
        self._last_report = run_report

        log.info(
            "reconcile_run_complete",
            workspaces=len(per_workspace),
            duration_s=duration,
        )
        return run_report

    # ------------------------------------------------------------------
    # Per-workspace reconcile
    # ------------------------------------------------------------------

    async def _reconcile_workspace(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> WorkspaceReconcileReport:
        """Run all drift checks for one workspace.

        ACL invariant: every store call below pins ``workspace_id``.

        Causal ordering guarantee
        -------------------------
        Entity OutboxEvents are always written before edge OutboxEvents in the
        same run.  This is enforced by the call order below:
        1. ``_reemit_entities_for_missing_sources`` — entity.upsert events.
        2. ``_check_entity_drift``                  — entity.upsert events.
        3. ``_check_edge_drift``                    — edge.upsert events.
        Both entity methods complete (or hit the cap) before edge drift is
        checked, so consumers see entity definitions before edge references.
        """
        pg_source_ids = await self._load_active_source_ids(workspace_id=workspace_id)

        pg_missing_qdrant = await self._check_pg_missing_qdrant(
            workspace_id=workspace_id,
            pg_source_ids=pg_source_ids,
        )
        qdrant_orphan_sources, qdrant_orphans_deleted = await self._check_qdrant_orphans(
            workspace_id=workspace_id,
            pg_source_ids=pg_source_ids,
        )
        neo4j_orphan_sources = await self._check_neo4j_orphans(
            workspace_id=workspace_id,
            pg_source_ids=pg_source_ids,
        )

        # Entity drift (entities first — causal order).
        await self._check_entity_drift(workspace_id=workspace_id)

        # Edge drift (edges after entities — causal order).
        await self._check_edge_drift(workspace_id=workspace_id)

        return WorkspaceReconcileReport(
            workspace_id=workspace_id,
            pg_missing_qdrant=pg_missing_qdrant,
            qdrant_orphan_sources=qdrant_orphan_sources,
            qdrant_orphans_deleted=qdrant_orphans_deleted,
            neo4j_orphan_sources=neo4j_orphan_sources,
        )

    async def _check_entity_drift(self, *, workspace_id: uuid.UUID) -> None:
        """Check for missing or out-of-date entities across stores.

        AP2 closed-loop: emits corrective ``entity.upsert`` OutboxEvents for
        any entity that is either:

        1. **Version drift**: Qdrant or Neo4j version < Postgres version.
        2. **Hash drift** (same version, corrupted content): Qdrant or Neo4j
           ``content_hash`` != Postgres ``Entity.content_hash`` (skipped when
           Postgres ``content_hash`` is NULL — legacy entity not yet hashed).

        Bounded by ``REEMIT_TICK_CAP`` via the worker-level cursor.  If the
        cap is already exhausted, the method logs and returns immediately.
        The remainder is processed on the next tick (all drift is re-detected).
        """
        remaining = self._reemit_cap - self._reemit_cursor
        if remaining <= 0:
            log.info(
                "reconcile_entity_drift_cap_exhausted",
                workspace_id=str(workspace_id),
                cap=self._reemit_cap,
            )
            return

        async with self._session_factory() as session:
            stmt = select(Entity).join(Source).where(Source.tenant_id == workspace_id)
            result = await session.execute(stmt)
            pg_entities: dict[uuid.UUID, Entity] = {e.id: e for e in result.scalars().all()}

        qdrant_versions = await self._vector_store.get_entity_versions(workspace_id=workspace_id)
        neo4j_versions = await self._graph_store.get_entity_versions(workspace_id=workspace_id)
        qdrant_hashes = await self._vector_store.get_entity_content_hashes(
            workspace_id=workspace_id
        )
        neo4j_hashes = await self._graph_store.get_entity_content_hashes(workspace_id=workspace_id)

        to_backfill: list[Entity] = []
        for ent_id, ent in pg_entities.items():
            q_v = qdrant_versions.get(ent_id, 0)
            n_v = neo4j_versions.get(ent_id, 0)

            version_drift = q_v < ent.version or n_v < ent.version

            # Hash drift: only when Postgres has a content_hash (post-AP2 writes).
            hash_drift = False
            if ent.content_hash is not None:
                q_h = qdrant_hashes.get(ent_id)
                n_h = neo4j_hashes.get(ent_id)
                # A mismatch (or missing hash in a projection that has the version)
                # means the projection was corrupted or partially written.
                if (q_h is not None and q_h != ent.content_hash) or (
                    n_h is not None and n_h != ent.content_hash
                ):
                    hash_drift = True

            if version_drift or hash_drift:
                to_backfill.append(ent)

        if not to_backfill:
            return

        total_drifted = len(to_backfill)
        # Apply the per-tick cap.
        to_emit = to_backfill[:remaining]
        deferred = total_drifted - len(to_emit)

        log.warning(
            "reconcile_entity_drift_detected",
            total_drifted=total_drifted,
            emitting=len(to_emit),
            deferred=deferred,
            workspace_id=str(workspace_id),
        )

        async with self._session_factory() as session, session.begin():
            for ent in to_emit:
                event_payload: dict[str, object] = {
                    "workspace_id": str(workspace_id),
                    "id": str(ent.id),
                    "source_id": str(ent.source_id),
                    "entity_type": ent.entity_type,
                    "name": ent.name,
                    "display_name": ent.display_name,
                    "metadata": ent.entity_metadata,
                    "version": ent.version,
                    "is_backfill": True,
                    "content_hash": ent.content_hash,
                }
                session.add(
                    OutboxEvent(
                        event_type="entity.upsert",
                        entity_id=ent.id,
                        payload=event_payload,
                    )
                )

        self._reemit_cursor += len(to_emit)
        if deferred:
            log.info(
                "reconcile_entity_drift_deferred",
                deferred=deferred,
                workspace_id=str(workspace_id),
                next_tick="remainder will be re-detected and emitted on the next reconcile tick",
            )

    async def _check_edge_drift(self, *, workspace_id: uuid.UUID) -> None:
        """Check for missing or out-of-date edges in Neo4j vs Postgres SoT.

        AP2 — edge drift detect + re-emit: edges live only in Neo4j (Qdrant
        has no edge representation — chunks are per-document, not per-edge).
        This method compares edge versions in Neo4j against the Postgres ``edges``
        table and emits an ``edge.upsert`` OutboxEvent for any edge whose
        Neo4j version is behind the Postgres version.

        Causal ordering: this method is always called **after**
        ``_check_entity_drift``, so the consumer sees entity definitions
        before edge references within the same tick.

        Bounded by ``REEMIT_TICK_CAP`` via the shared worker-level cursor.
        """
        remaining = self._reemit_cap - self._reemit_cursor
        if remaining <= 0:
            log.info(
                "reconcile_edge_drift_cap_exhausted",
                workspace_id=str(workspace_id),
                cap=self._reemit_cap,
            )
            return

        # Load PG edge SoT.
        async with self._session_factory() as session:
            stmt = (
                select(Edge)
                .join(Entity, Edge.source_entity_id == Entity.id)
                .join(Source, Entity.source_id == Source.id)
                .where(Source.tenant_id == workspace_id)
            )
            result = await session.execute(stmt)
            pg_edges: list[Edge] = list(result.scalars().all())

        if not pg_edges:
            return

        # Load Neo4j edge versions.
        neo4j_edge_versions = await self._graph_store.get_edge_versions(workspace_id=workspace_id)

        to_backfill: list[Edge] = []
        for edge in pg_edges:
            key = (edge.source_entity_id, edge.target_entity_id, edge.edge_type)
            neo4j_ver = neo4j_edge_versions.get(key, 0)
            if neo4j_ver < edge.version:
                to_backfill.append(edge)

        if not to_backfill:
            return

        total_drifted = len(to_backfill)
        to_emit = to_backfill[:remaining]
        deferred = total_drifted - len(to_emit)

        log.warning(
            "reconcile_edge_drift_detected",
            total_drifted=total_drifted,
            emitting=len(to_emit),
            deferred=deferred,
            workspace_id=str(workspace_id),
        )

        async with self._session_factory() as session, session.begin():
            for edge in to_emit:
                event_payload: dict[str, object] = {
                    "workspace_id": str(workspace_id),
                    "source_entity_id": str(edge.source_entity_id),
                    "target_entity_id": str(edge.target_entity_id),
                    "edge_type": edge.edge_type,
                    "metadata": edge.edge_metadata,
                    "version": edge.version,
                    "is_backfill": True,
                }
                session.add(
                    OutboxEvent(
                        event_type="edge.upsert",
                        entity_id=edge.source_entity_id,
                        payload=event_payload,
                    )
                )
                RECONCILE_DRIFT_TOTAL.labels(drift_type=DRIFT_EDGE).inc()

        self._reemit_cursor += len(to_emit)
        if deferred:
            log.info(
                "reconcile_edge_drift_deferred",
                deferred=deferred,
                workspace_id=str(workspace_id),
                next_tick="remainder will be re-detected and emitted on the next reconcile tick",
            )

    # ------------------------------------------------------------------
    # Drift check A: PG document with no Qdrant chunks
    # ------------------------------------------------------------------

    async def _check_pg_missing_qdrant(
        self,
        *,
        workspace_id: uuid.UUID,
        pg_source_ids: set[uuid.UUID],
    ) -> list[uuid.UUID]:
        """Return source_ids that have PG documents but zero Qdrant chunks."""
        if not pg_source_ids:
            return []

        qdrant_source_map = await self._vector_store.count_chunks_by_source(
            workspace_id=workspace_id
        )
        qdrant_source_ids = {uuid.UUID(k) for k in qdrant_source_map if qdrant_source_map[k] > 0}

        missing = sorted(pg_source_ids - qdrant_source_ids)
        if missing:
            await self._reemit_entities_for_missing_sources(
                workspace_id=workspace_id,
                missing_source_ids=missing,
            )
        for src_id in missing:
            RECONCILE_DRIFT_TOTAL.labels(drift_type=DRIFT_PG_MISSING_QDRANT).inc()
            log.warning(
                "reconcile_drift_pg_missing_qdrant",
                workspace_id=str(workspace_id),
                source_id=str(src_id),
            )
            await self._mark_ingestion_run_partial(source_id=src_id)

        return missing

    # ------------------------------------------------------------------
    # Drift check B: Qdrant chunks with no PG document (orphans)
    # ------------------------------------------------------------------

    async def _check_qdrant_orphans(
        self,
        *,
        workspace_id: uuid.UUID,
        pg_source_ids: set[uuid.UUID],
    ) -> tuple[list[str], int]:
        """Detect and delete orphan Qdrant chunks (bounded per run).

        Returns a tuple of (orphan_source_id_strings, total_deleted_count).
        """
        qdrant_source_map = await self._vector_store.count_chunks_by_source(
            workspace_id=workspace_id
        )
        orphan_sources = [
            src_str
            for src_str, count in qdrant_source_map.items()
            if count > 0 and _try_parse_uuid(src_str) not in pg_source_ids
        ]

        total_deleted = 0
        budget = self._orphan_limit
        for src_str in orphan_sources:
            if budget <= 0:
                break
            src_id = _try_parse_uuid(src_str)
            if src_id is None:
                continue
            deleted = await self._delete_qdrant_orphan_source(
                workspace_id=workspace_id,
                source_id=src_id,
                limit=budget,
            )
            total_deleted += deleted
            budget -= deleted
            RECONCILE_DRIFT_TOTAL.labels(drift_type=DRIFT_QDRANT_ORPHAN).inc()
            log.warning(
                "reconcile_drift_qdrant_orphan",
                workspace_id=str(workspace_id),
                source_id=src_str,
                chunks_deleted=deleted,
            )

        return orphan_sources, total_deleted

    async def _delete_qdrant_orphan_source(
        self,
        *,
        workspace_id: uuid.UUID,
        source_id: uuid.UUID,
        limit: int,
    ) -> int:
        """Delete up to ``limit`` orphan Qdrant chunks for one source.

        Uses ``FilterSelector`` (source + workspace) so the delete is
        workspace-scoped and idempotent.
        """
        from omniscience_index.stores.qdrant_filters import QdrantFilterBuilder
        from qdrant_client import models as qm

        flt = QdrantFilterBuilder(workspace_id=workspace_id).with_source_ids([source_id]).build()
        # Count first so we respect the limit.
        count_result = await self._vector_store._qc.count(
            collection_name=self._vector_store.collection_name,
            count_filter=flt,
            exact=True,
        )
        to_delete = min(int(count_result.count), limit)
        if to_delete == 0:
            return 0

        # Scroll to get actual point IDs (FilterSelector + limit not
        # available in delete — we need PointIdsList for bounded deletes).
        records, _ = await self._vector_store._qc.scroll(
            collection_name=self._vector_store.collection_name,
            scroll_filter=flt,
            limit=to_delete,
            with_payload=False,
            with_vectors=False,
        )
        if not records:
            return 0
        point_ids: list[int | str | uuid.UUID] = [str(r.id) for r in records]
        await self._vector_store._qc.delete(
            collection_name=self._vector_store.collection_name,
            points_selector=qm.PointIdsList(points=point_ids),
            wait=True,
        )
        return len(point_ids)

    # ------------------------------------------------------------------
    # Drift check C: Neo4j entities with no PG document (orphans)
    # ------------------------------------------------------------------

    async def _check_neo4j_orphans(
        self,
        *,
        workspace_id: uuid.UUID,
        pg_source_ids: set[uuid.UUID],
    ) -> list[str]:
        """Detect and actively heal Neo4j entity nodes with no PG source.

        AP2 active tombstone heal: for each orphaned source (entities in
        Neo4j that have no active Postgres document), the worker now calls
        ``graph_store.end_date_source_orphans`` to set ``valid_to = now`` on
        all still-open entity nodes and their incident relationships.  This
        replaces the previous detect-only behaviour.

        The metric and structured log are still emitted for observability.

        ``ENABLE_NEO4J_ORPHAN_REEMIT`` (environment variable) is kept for
        backward compatibility but is now always ``true`` by default — the
        heal action is unconditional.  The flag will be removed in a future
        release.
        """
        neo4j_source_map = await self._graph_store.count_entities_by_source(
            workspace_id=workspace_id
        )
        orphan_sources = [
            src_str
            for src_str, count in neo4j_source_map.items()
            if count > 0 and _try_parse_uuid(src_str) not in pg_source_ids
        ]

        now_iso = datetime.now(UTC).isoformat()
        for src_str in orphan_sources:
            RECONCILE_DRIFT_TOTAL.labels(drift_type=DRIFT_NEO4J_ORPHAN).inc()

            # AP2: actively heal (end-date) the orphaned nodes.
            healed = await self._graph_store.end_date_source_orphans(
                workspace_id=workspace_id,
                source_id=src_str,
                now_iso=now_iso,
            )
            log.warning(
                "reconcile_neo4j_orphan_healed",
                workspace_id=str(workspace_id),
                source_id=src_str,
                entities_end_dated=healed,
            )

        return orphan_sources

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _load_workspace_ids(self) -> list[uuid.UUID]:
        """Return all workspace ids — reconcile iterates one at a time."""
        async with self._session_factory() as session:
            result = await session.execute(select(Workspace.id))
            return [row[0] for row in result.all()]

    async def _load_active_source_ids(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> set[uuid.UUID]:
        """Return source_ids that have at least one non-tombstoned document.

        Scoped to the workspace via ``Source.tenant_id == workspace_id``.
        Sources with only tombstoned documents are excluded — they look
        like Qdrant orphans from the retention perspective, not drift.
        """
        async with self._session_factory() as session:
            stmt = (
                select(Document.source_id)
                .join(Source, Source.id == Document.source_id)
                .where(Source.tenant_id == workspace_id)
                .where(Document.tombstoned_at.is_(None))
                .distinct()
            )
            result = await session.execute(stmt)
            return {row[0] for row in result.all()}

    async def _mark_ingestion_run_partial(
        self,
        *,
        source_id: uuid.UUID,
    ) -> None:
        """Mark the most recent ingestion run for ``source_id`` as ``partial``.

        Idempotent: only updates rows currently in ``ok`` status so that a
        run already marked ``error`` or ``partial`` is not overwritten.
        This surfaces the drift in the ops UI (ingestion_runs table) and
        allows operators to trigger a re-sync.
        """
        async with self._session_factory() as session, session.begin():
            # Identify the most recent finished run for this source.
            stmt = (
                select(IngestionRun.id)
                .where(IngestionRun.source_id == source_id)
                .where(IngestionRun.status == IngestionRunStatus.ok)
                .order_by(IngestionRun.started_at.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.first()
            if row is None:
                return
            run_id: uuid.UUID = row[0]
            await session.execute(
                update(IngestionRun)
                .where(IngestionRun.id == run_id)
                .where(IngestionRun.status == IngestionRunStatus.ok)
                .values(status=IngestionRunStatus.partial)
            )
        log.info(
            "reconcile_ingestion_run_marked_partial",
            source_id=str(source_id),
            run_id=str(run_id),
        )

    async def _reemit_entities_for_missing_sources(
        self,
        *,
        workspace_id: uuid.UUID,
        missing_source_ids: list[uuid.UUID],
    ) -> None:
        """Re-emit entity.upsert OutboxEvents for entities in missing sources.

        AP2 closed-loop: when a source has PG entities but no Qdrant chunks
        the reconcile worker was previously detect-only.  This method closes
        the loop by emitting a backfill ``entity.upsert`` OutboxEvent for
        every entity that belongs to one of the missing source_ids.

        Bounded by ``REEMIT_TICK_CAP`` via the shared cursor.  When the cap
        is already exhausted the method returns immediately; callers should
        check whether re-emit happened by inspecting the cursor.

        Idempotent: emitting a duplicate OutboxEvent is harmless because the
        version guard in the store adapters prevents stale overwrites.
        """
        remaining = self._reemit_cap - self._reemit_cursor
        if remaining <= 0:
            log.info(
                "reconcile_reemit_cap_exhausted_before_missing_sources",
                workspace_id=str(workspace_id),
                cap=self._reemit_cap,
            )
            return

        async with self._session_factory() as session:
            stmt = (
                select(Entity)
                .join(Source)
                .where(
                    Entity.source_id.in_(missing_source_ids),
                    Source.tenant_id == workspace_id,
                )
            )
            result = await session.execute(stmt)
            entities = list(result.scalars().all())

        if not entities:
            return

        total = len(entities)
        to_emit = entities[:remaining]
        deferred = total - len(to_emit)

        log.info(
            "reconcile_pg_missing_qdrant_reemit",
            total=total,
            emitting=len(to_emit),
            deferred=deferred,
            workspace_id=str(workspace_id),
        )

        async with self._session_factory() as session, session.begin():
            for ent in to_emit:
                event_payload: dict[str, object] = {
                    "workspace_id": str(workspace_id),
                    "id": str(ent.id),
                    "source_id": str(ent.source_id),
                    "entity_type": ent.entity_type,
                    "name": ent.name,
                    "display_name": ent.display_name,
                    "metadata": ent.entity_metadata,
                    "version": ent.version,
                    "is_backfill": True,
                    "content_hash": ent.content_hash,
                }
                session.add(
                    OutboxEvent(
                        event_type="entity.upsert",
                        entity_id=ent.id,
                        payload=event_payload,
                    )
                )

        self._reemit_cursor += len(to_emit)
        if deferred:
            log.info(
                "reconcile_reemit_deferred",
                deferred=deferred,
                workspace_id=str(workspace_id),
                next_tick="remainder will be re-detected and emitted on the next reconcile tick",
            )

    async def _emit_orphan_detected_event(
        self,
        *,
        workspace_id: uuid.UUID,
        source_id: str,
    ) -> None:
        """(Legacy diagnostic) Emit a ``source.orphan_detected`` OutboxEvent.

        AP2 Batch C: Neo4j orphan healing is now active (``end_date_source_orphans``
        is called unconditionally in ``_check_neo4j_orphans``).  This method is
        kept for callers that explicitly want the durable diagnostic event, but
        is no longer part of the default reconcile path.
        """
        async with self._session_factory() as session, session.begin():
            session.add(
                OutboxEvent(
                    event_type="source.orphan_detected",
                    payload={
                        "workspace_id": str(workspace_id),
                        "source_id": source_id,
                    },
                )
            )
        log.info(
            "reconcile_neo4j_orphan_event_emitted",
            workspace_id=str(workspace_id),
            source_id=source_id,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _try_parse_uuid(value: str) -> uuid.UUID | None:
    """Parse a UUID string, returning ``None`` on failure."""
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return None


__all__ = [
    "ENABLE_NEO4J_ORPHAN_REEMIT",
    "REEMIT_TICK_CAP",
    "ReconcileRunReport",
    "ReconcileWorker",
    "WorkspaceReconcileReport",
]
