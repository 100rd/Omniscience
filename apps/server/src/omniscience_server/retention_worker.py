"""Retention worker — hot/warm/archive eviction (issue #135, ADR-0009).

The worker runs as a persistent asyncio task driven by the FastAPI
lifespan (ADR-0009 §3).  On each tick it:

1. Iterates workspaces sequentially (per-tenant iteration; cross-tenant
   batches are forbidden — ADR-0009 §Consequences-security #1).
2. For each workspace, runs the **read-then-mark-then-move** three-phase
   pattern (ADR-0009 §3) over Neo4j:
     - **Read**:  count eligible :EntityState rows + edges with
       ``recorded_at < now - hot_days``; fetch a sample for the
       dry-run report; record the lag SLO.
     - **Mark**:  set ``tier_pending = 'warm'`` on eligible rows in
       bounded batches.  Idempotent — re-runs are no-ops.
     - **Move**:  project marked rows to ``:EntitySnapshot:Daily`` /
       ``:RelationshipSnapshot:Daily`` rows under the snapshot-per-day
       projection (ADR-0009 §1 warm tier) and DETACH DELETE the
       sources in the same transaction.
3. Mirrors the eviction on Qdrant: chunks past the boundary get
   ``tier="warm"`` + ``snapshot_date`` payload (ADR-0009 §5).
4. For warm rows older than ``warm_days``, writes a parquet object per
   ``(workspace_id, snapshot_date)`` to S3 (ADR-0009 §1 archive / §7
   storage layout) and deletes the warm rows + Qdrant warm chunks.
5. Updates Prometheus metrics (ADR-0009 §8) and structured logs.

**Dry-run mode** (``Settings.retention_dry_run=True``) inhibits every
mutation (mark, move, S3 put, delete) and produces a structured report
exposed via ``GET /api/v1/admin/retention/report``.

The worker is idempotent: re-running is a no-op unless wall-clock time
has advanced past the cutoff.  A crash between mark and move leaves a
re-runnable state — the next pass picks up the marked rows and finishes
the move.

The implementation is plain ``asyncio`` rather than APScheduler.  The
codebase already has ``FreshnessWorker`` and ``SchedulerWorker`` on
this shape; adding APScheduler for a single 6-hour periodic tick would
be a heavyweight dependency for a thin wrapper around
``asyncio.sleep``.  ADR-0009 §3 names APScheduler as one acceptable
implementation; the worker contract (in-process FastAPI lifespan,
6-hour default tick, idempotent, dry-run mandatory) is preserved
verbatim.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog
from omniscience_core.config import Settings
from omniscience_core.db.models import Workspace
from omniscience_core.telemetry.metrics import (
    RETENTION_EVICTION_TOTAL,
    RETENTION_GRAPH_RECORDS_TOTAL,
    RETENTION_WORKER_DURATION_SECONDS,
    RETENTION_WORKER_LAG_SECONDS,
)
from omniscience_index.stores.neo4j_store import Neo4jGraphStore
from omniscience_index.stores.qdrant_store import QdrantVectorStore
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from omniscience_server.retention_archive import (
    build_archive_key,
    build_parquet_bytes,
    build_s3_client,
    write_archive_object,
)
from omniscience_server.retention_constants import (
    PHASE_MARK,
    PHASE_MOVE,
    PHASE_READ,
    RETENTION_ARCHIVE_DATES_PER_RUN,
    RETENTION_DRY_RUN_SAMPLE_DEFAULT,
    RETENTION_TICK_SECONDS_DEFAULT,
    STORE_LABEL_NEO4J,
    STORE_LABEL_QDRANT,
    TIER_LABEL_ARCHIVE,
    TIER_LABEL_HOT,
    TIER_LABEL_WARM,
    TRANSITION_HOT_TO_WARM,
    TRANSITION_WARM_TO_ARCHIVE,
)

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WorkspaceRetentionReport:
    """Per-workspace report produced by one retention run.

    Mirrors the ADR-0009 §3 dry-run shape: counts + sampled records,
    no mutation indication.  Used by both the live worker (logged at
    INFO) and the dry-run REST endpoint (returned to the caller).
    """

    workspace_id: uuid.UUID
    eligible_hot_to_warm_entity_states: int
    eligible_hot_to_warm_edges: int
    eligible_hot_to_warm_chunks: int
    eligible_warm_to_archive_entity_snapshots: int
    eligible_warm_to_archive_dates: list[date]
    sampled_eligible: list[dict[str, Any]] = field(default_factory=list)
    oldest_eligible_recorded_at: datetime | None = None
    lag_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class RunReport:
    """Aggregated report across one retention worker tick."""

    started_at: datetime
    finished_at: datetime
    dry_run: bool
    per_workspace: list[WorkspaceRetentionReport]
    duration_seconds: float

    @property
    def lag_seconds(self) -> float:
        """Max per-workspace lag — drives the deployment-wide gauge."""
        if not self.per_workspace:
            return 0.0
        return max(w.lag_seconds for w in self.per_workspace)


class RetentionWorker:
    """Background task that evicts data across hot/warm/archive tiers.

    Args:
        session_factory: SQLAlchemy session factory (used to enumerate
            workspaces).
        graph_store:     Neo4j store, extended with retention methods
            in this PR.
        vector_store:    Qdrant store, extended with retention methods
            in this PR.
        settings:        Application Settings — drives boundaries,
            tick interval, dry-run, archive bucket / KMS.

    Lifecycle (ADR-0009 §3):
        * ``start()`` runs the periodic tick loop.
        * ``stop()`` signals graceful exit on the next iteration.
        * Crash safety: each phase is its own transaction; a crash
          mid-phase leaves a re-runnable state.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        graph_store: Neo4jGraphStore,
        vector_store: QdrantVectorStore,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._graph_store = graph_store
        self._vector_store = vector_store
        self._settings = settings
        self._tick_seconds = max(int(settings.retention_tick_seconds), 60)
        self._batch_size = max(int(settings.retention_batch_size), 1)
        self._sample_size = max(int(settings.retention_sample_size), 0)
        self._dry_run = bool(settings.retention_dry_run)
        self._running = False
        self._last_report: RunReport | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Run the worker loop until :meth:`stop` is called."""
        self._running = True
        log.info(
            "retention_worker_started",
            tick_seconds=self._tick_seconds,
            dry_run=self._dry_run,
            hot_days=self._settings.retention_hot_days,
            warm_days=self._settings.retention_warm_days,
        )
        while self._running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                # Never let one failed run crash the worker.  ADR-0009 §9
                # crash runbook: "the next scheduled run resumes from
                # idempotency".
                log.error("retention_worker_error", error=str(exc))
            try:
                await asyncio.sleep(self._tick_seconds)
            except asyncio.CancelledError:
                break
        log.info("retention_worker_stopped")

    def stop(self) -> None:
        """Signal the worker to exit after the current tick completes."""
        self._running = False

    @property
    def last_report(self) -> RunReport | None:
        """Return the most recent run report, or ``None`` before first run."""
        return self._last_report

    @property
    def dry_run(self) -> bool:
        """Whether the worker is running in dry-run mode."""
        return self._dry_run

    # ------------------------------------------------------------------
    # Public entry point — one full tick across all workspaces
    # ------------------------------------------------------------------

    async def run_once(
        self,
        *,
        workspace_filter: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> RunReport:
        """Execute one full retention tick.

        Args:
            workspace_filter: When set, restricts the run to a single
                workspace — used by the dry-run REST endpoint.
            now: Optional reference time.  Defaults to ``datetime.now(UTC)``;
                tests pin a value to make assertions deterministic.

        Returns:
            A :class:`RunReport` covering all workspaces processed.
        """
        started = datetime.now(UTC) if now is None else now
        wall_start = time.monotonic()
        workspaces = await self._load_workspace_ids()
        if workspace_filter is not None:
            workspaces = [w for w in workspaces if w == workspace_filter]
        per_workspace: list[WorkspaceRetentionReport] = []
        for workspace_id in workspaces:
            report = await self._process_workspace(workspace_id=workspace_id, now=started)
            per_workspace.append(report)
        duration = time.monotonic() - wall_start
        finished = datetime.now(UTC)
        run_report = RunReport(
            started_at=started,
            finished_at=finished,
            dry_run=self._dry_run,
            per_workspace=per_workspace,
            duration_seconds=duration,
        )
        # Lag SLO gauge per ADR-0009 §8.
        RETENTION_WORKER_LAG_SECONDS.set(run_report.lag_seconds)
        self._last_report = run_report
        log.info(
            "retention_run_complete",
            dry_run=self._dry_run,
            workspaces=len(per_workspace),
            duration_s=duration,
            lag_s=run_report.lag_seconds,
        )
        return run_report

    # ------------------------------------------------------------------
    # Per-workspace pipeline
    # ------------------------------------------------------------------

    async def _process_workspace(
        self,
        *,
        workspace_id: uuid.UUID,
        now: datetime,
    ) -> WorkspaceRetentionReport:
        """Run the three-phase pipeline against one workspace.

        ACL invariant: every store call below pins ``workspace_id`` —
        cross-workspace mixing is structurally impossible from this
        method.
        """
        hot_cutoff = now - timedelta(days=self._settings.retention_hot_days)
        warm_cutoff_date = (now - timedelta(days=self._settings.retention_warm_days)).date()

        # --- Read phase --------------------------------------------------
        read_start = time.monotonic()
        es_eligible, edge_eligible = await self._graph_store.count_hot_to_warm_eligible(
            workspace_id=workspace_id,
            hot_cutoff=hot_cutoff,
        )
        chunk_eligible = await self._vector_store.count_retention_eligible(
            workspace_id=workspace_id,
            cutoff=hot_cutoff,
        )
        archive_eligible = await self._graph_store.count_warm_to_archive_eligible(
            workspace_id=workspace_id,
            warm_cutoff_date=warm_cutoff_date,
        )
        archive_dates = await self._graph_store.list_warm_to_archive_dates(
            workspace_id=workspace_id,
            warm_cutoff_date=warm_cutoff_date,
            limit=RETENTION_ARCHIVE_DATES_PER_RUN,
        )
        sampled = await self._graph_store.sample_hot_to_warm_eligible(
            workspace_id=workspace_id,
            hot_cutoff=hot_cutoff,
            limit=self._sample_size or RETENTION_DRY_RUN_SAMPLE_DEFAULT,
        )
        oldest = await self._graph_store.oldest_hot_to_warm_recorded_at(
            workspace_id=workspace_id,
            hot_cutoff=hot_cutoff,
        )
        lag_seconds = 0.0
        if oldest is not None:
            lag_seconds = max((now - oldest).total_seconds() - 0.0, 0.0)
        RETENTION_WORKER_DURATION_SECONDS.labels(
            phase=PHASE_READ, store=STORE_LABEL_NEO4J
        ).observe(time.monotonic() - read_start)

        # Tier-records gauge updates from the read phase counts.  These
        # are workspace-scoped reads; the gauge label set is global so
        # multiple workspaces overwrite the same label cell — operators
        # use ``sum() by (tier, store)`` to roll up.  This is the
        # documented ADR-0009 §8 shape.
        records = await self._graph_store.count_records_by_tier(workspace_id=workspace_id)
        RETENTION_GRAPH_RECORDS_TOTAL.labels(tier=TIER_LABEL_HOT, store=STORE_LABEL_NEO4J).set(
            records.get("hot", 0)
        )
        RETENTION_GRAPH_RECORDS_TOTAL.labels(tier=TIER_LABEL_WARM, store=STORE_LABEL_NEO4J).set(
            records.get("warm", 0)
        )
        chunk_tiers = await self._vector_store.count_chunks_by_tier(workspace_id=workspace_id)
        RETENTION_GRAPH_RECORDS_TOTAL.labels(tier=TIER_LABEL_HOT, store=STORE_LABEL_QDRANT).set(
            chunk_tiers.get("hot", 0)
        )
        RETENTION_GRAPH_RECORDS_TOTAL.labels(tier=TIER_LABEL_WARM, store=STORE_LABEL_QDRANT).set(
            chunk_tiers.get("warm", 0)
        )
        # Archive on Qdrant is always 0 (re-embedding from archive is
        # not supported in v1, ADR-0009 §5).  Set explicitly so the
        # gauge does not stay at the previous value across deploys.
        RETENTION_GRAPH_RECORDS_TOTAL.labels(
            tier=TIER_LABEL_ARCHIVE, store=STORE_LABEL_QDRANT
        ).set(0)

        if self._dry_run:
            return WorkspaceRetentionReport(
                workspace_id=workspace_id,
                eligible_hot_to_warm_entity_states=es_eligible,
                eligible_hot_to_warm_edges=edge_eligible,
                eligible_hot_to_warm_chunks=chunk_eligible,
                eligible_warm_to_archive_entity_snapshots=archive_eligible,
                eligible_warm_to_archive_dates=archive_dates,
                sampled_eligible=sampled,
                oldest_eligible_recorded_at=oldest,
                lag_seconds=lag_seconds,
            )

        # --- Mark phase --------------------------------------------------
        mark_start = time.monotonic()
        es_marked, edge_marked = await self._graph_store.mark_hot_to_warm(
            workspace_id=workspace_id,
            hot_cutoff=hot_cutoff,
            batch_size=self._batch_size,
        )
        chunks_marked = await self._vector_store.mark_retention_warm(
            workspace_id=workspace_id,
            cutoff=hot_cutoff,
        )
        RETENTION_WORKER_DURATION_SECONDS.labels(
            phase=PHASE_MARK, store=STORE_LABEL_NEO4J
        ).observe(time.monotonic() - mark_start)

        # --- Move phase: hot -> warm ------------------------------------
        move_start = time.monotonic()
        es_moved, edges_moved = await self._graph_store.move_hot_to_warm(
            workspace_id=workspace_id,
            batch_size=self._batch_size,
        )
        RETENTION_EVICTION_TOTAL.labels(
            transition=TRANSITION_HOT_TO_WARM, store=STORE_LABEL_NEO4J
        ).inc(es_moved + edges_moved)
        # Qdrant: the mark phase already sets the warm tier payload; no
        # separate move step (snapshot points carry the payload directly,
        # ADR-0009 §5).  Count the transition as the marked total.
        RETENTION_EVICTION_TOTAL.labels(
            transition=TRANSITION_HOT_TO_WARM, store=STORE_LABEL_QDRANT
        ).inc(chunks_marked)
        RETENTION_WORKER_DURATION_SECONDS.labels(
            phase=PHASE_MOVE, store=STORE_LABEL_NEO4J
        ).observe(time.monotonic() - move_start)

        # --- Archive phase: warm -> archive -----------------------------
        await self._archive_dates(
            workspace_id=workspace_id,
            snapshot_dates=archive_dates,
        )

        log.info(
            "retention_workspace_processed",
            workspace_id=str(workspace_id),
            es_marked=es_marked,
            es_moved=es_moved,
            edges_marked=edge_marked,
            edges_moved=edges_moved,
            chunks_marked=chunks_marked,
            archive_dates=len(archive_dates),
            lag_seconds=lag_seconds,
        )

        return WorkspaceRetentionReport(
            workspace_id=workspace_id,
            eligible_hot_to_warm_entity_states=es_eligible,
            eligible_hot_to_warm_edges=edge_eligible,
            eligible_hot_to_warm_chunks=chunk_eligible,
            eligible_warm_to_archive_entity_snapshots=archive_eligible,
            eligible_warm_to_archive_dates=archive_dates,
            sampled_eligible=sampled,
            oldest_eligible_recorded_at=oldest,
            lag_seconds=lag_seconds,
        )

    # ------------------------------------------------------------------
    # Archive (warm -> S3)
    # ------------------------------------------------------------------

    async def _archive_dates(
        self,
        *,
        workspace_id: uuid.UUID,
        snapshot_dates: list[date],
    ) -> None:
        """Write parquet objects for ``snapshot_dates`` and delete warm rows.

        When the archive bucket is not configured (dev / air-gapped),
        the worker logs and skips the archive phase — warm rows stay
        queryable indefinitely until the operator wires a bucket.
        Documented in ADR-0009 §Negative-operational ("Object storage
        dependency is new").
        """
        if not snapshot_dates:
            return
        bucket = self._settings.retention_archive_bucket
        if not bucket:
            log.info(
                "retention_archive_skipped_no_bucket",
                workspace_id=str(workspace_id),
                dates=len(snapshot_dates),
            )
            return
        s3_client = build_s3_client(
            region_name=self._settings.retention_archive_s3_region,
            endpoint_url=self._settings.retention_archive_s3_endpoint_url,
        )
        for snapshot_date in snapshot_dates:
            await self._archive_one_date(
                workspace_id=workspace_id,
                snapshot_date=snapshot_date,
                bucket=bucket,
                s3_client=s3_client,
            )

    async def _archive_one_date(
        self,
        *,
        workspace_id: uuid.UUID,
        snapshot_date: date,
        bucket: str,
        s3_client: Any,
    ) -> None:
        """Write one parquet object and reap the warm rows.

        Failures bubble up the call stack so the next phase's metrics
        record nothing for this date — the next run will retry per
        ADR-0009 §9 object-storage outage runbook.
        """
        entity_rows, edge_rows = await self._graph_store.fetch_warm_snapshot_rows(
            workspace_id=workspace_id,
            snapshot_date=snapshot_date,
        )
        body = build_parquet_bytes(entity_rows=entity_rows, edge_rows=edge_rows)
        key = build_archive_key(workspace_id=workspace_id, snapshot_date=snapshot_date)
        # Synchronous boto3 call — S3 PUT does not benefit from asyncio.
        # Keep it inside an executor to avoid blocking the event loop on
        # large objects.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: write_archive_object(
                s3_client=s3_client,
                bucket=bucket,
                key=key,
                body=body,
                kms_key_arn=self._settings.retention_archive_kms_key_arn,
            ),
        )
        # AFTER the parquet write succeeded, reap warm rows on Neo4j
        # and the corresponding chunks on Qdrant.  Failure between these
        # two leaves the archive in S3 + warm rows still in Neo4j; the
        # next run's idempotent fetch returns those rows again — the
        # MERGE on (workspace_id, id, snapshot_date) collapses the
        # second write into the existing parquet object.  ADR-0009 §9.
        es_deleted, edge_deleted = await self._graph_store.delete_warm_snapshot(
            workspace_id=workspace_id,
            snapshot_date=snapshot_date,
        )
        chunks_deleted = await self._vector_store.delete_retention_archive(
            workspace_id=workspace_id,
            snapshot_date=snapshot_date,
        )
        RETENTION_EVICTION_TOTAL.labels(
            transition=TRANSITION_WARM_TO_ARCHIVE, store=STORE_LABEL_NEO4J
        ).inc(es_deleted + edge_deleted)
        RETENTION_EVICTION_TOTAL.labels(
            transition=TRANSITION_WARM_TO_ARCHIVE, store=STORE_LABEL_QDRANT
        ).inc(chunks_deleted)
        log.info(
            "retention_archive_written",
            workspace_id=str(workspace_id),
            snapshot_date=snapshot_date.isoformat(),
            entities=len(entity_rows),
            edges=len(edge_rows),
            uri=f"s3://{bucket}/{key}",
            es_deleted=es_deleted,
            edge_deleted=edge_deleted,
            chunks_deleted=chunks_deleted,
        )

    # ------------------------------------------------------------------
    # Workspace enumeration
    # ------------------------------------------------------------------

    async def _load_workspace_ids(self) -> list[uuid.UUID]:
        """Return all workspace ids — eviction iterates one at a time."""
        async with self._session_factory() as session:
            result = await session.execute(select(Workspace.id))
            return [row[0] for row in result.all()]


__all__ = [
    "RETENTION_TICK_SECONDS_DEFAULT",
    "RetentionWorker",
    "RunReport",
    "WorkspaceRetentionReport",
]
