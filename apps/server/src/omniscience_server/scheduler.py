"""Scheduler worker: automatic stale-source re-sync triggers.

:class:`SchedulerWorker` runs as a persistent asyncio task for the lifetime
of the server process.  On each tick it:

1. Loads all active sources from the database.
2. For each source, determines the effective TTL:
   - If the source has ``freshness_sla_seconds`` set, that value is used.
   - Otherwise the per-type default from :data:`SchedulerWorker.DEFAULT_TTLS` applies.
   - Sources with no explicit SLA **and** no per-type default are skipped.
3. Compares ``last_sync_at`` against the effective TTL.  A source is *stale*
   when ``now - last_sync_at > ttl``, or when it has never been synced.
4. For every stale source, publishes a
   :class:`~omniscience_server.ingestion.events.DocumentChangeEvent`
   to ``ingest.changes.{source_type}`` via NATS JetStream — the same
   subject used by the manual ``POST /api/v1/sources/{id}/sync`` endpoint.
5. Updates Prometheus counters and emits structured logs.

Usage in ``app.py`` lifespan::

    from omniscience_server.scheduler import SchedulerWorker

    scheduler = SchedulerWorker(
        session_factory=session_factory,
        nats_conn=nats_conn,
        interval=settings.scheduler_interval_seconds,
    )
    scheduler_task = asyncio.create_task(scheduler.start())
    app.state.scheduler = scheduler
    ...
    yield
    ...
    await scheduler.stop()
    scheduler_task.cancel()

Design notes
------------
- The worker skips a cycle gracefully when NATS is unavailable rather than
  crashing.  A counter tracks publish failures so operators can alert on
  ``omniscience_scheduler_syncs_triggered_total`` not growing as expected.
- ``last_sync_at`` is ``None`` (never synced) → always triggers.
- Paused sources are skipped (``status != active``).
- A single DB read per cycle keeps the implementation O(n) without
  per-source queries.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from typing import ClassVar

import structlog
from omniscience_core.db.models import Source, SourceStatus
from omniscience_core.queue.producer import QueueProducer
from omniscience_core.telemetry.metrics import (
    SCHEDULER_CHECK_DURATION_SECONDS,
    SCHEDULER_SYNCS_TRIGGERED_TOTAL,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from omniscience_server.ingestion.events import DocumentChangeEvent

log = structlog.get_logger(__name__)


class SchedulerWorker:
    """Background task that triggers re-syncs for stale sources.

    Runs on a configurable interval (default 5 min).  For each active source,
    compares ``last_sync_at`` against the source's ``freshness_sla_seconds``.
    When no explicit SLA is configured the per-type default from
    :attr:`DEFAULT_TTLS` is used instead.  If a source is stale a full-sync
    trigger is published to NATS.

    Args:
        session_factory: SQLAlchemy async session factory.
        nats_conn: Active NATS connection object that exposes a ``jetstream``
                   attribute.  When ``None`` the worker will log a warning on
                   each cycle and skip publishing.
        interval: Tick interval in seconds.  Defaults to 300 (5 minutes).
    """

    # Per-type default TTLs (seconds) applied when a source has no explicit SLA.
    DEFAULT_TTLS: ClassVar[dict[str, int]] = {
        "git": 300,  # 5 min — webhook covers most; this is a safety net
        "fs": 60,  # 1 min
        "s3": 900,  # 15 min
        "aws": 3600,  # 1 hour
        "confluence": 900,  # 15 min
        "notion": 900,  # 15 min
        "slack": 300,  # 5 min
        "jira": 600,  # 10 min
        "database": 3600,  # 1 hour
        "k8s": 1800,  # 30 min — agentic connector; launchd/agent outage safety net
    }

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        nats_conn: object | None,
        interval: int = 300,
    ) -> None:
        self._session_factory = session_factory
        self._nats_conn = nats_conn
        self._interval = interval
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Run the scheduler loop until :meth:`stop` is called."""
        self._running = True
        log.info("scheduler_worker_started", interval_seconds=self._interval)

        while self._running:
            try:
                triggered = await self._check_and_trigger()
                log.debug("scheduler_tick_complete", triggered=triggered)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                # Never crash the process due to a single failed cycle.
                log.error("scheduler_worker_error", error=str(exc))

            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

        log.info("scheduler_worker_stopped")

    async def stop(self) -> None:
        """Signal the worker to exit after the current tick completes."""
        self._running = False

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    async def _check_and_trigger(self) -> int:
        """Evaluate all active sources and publish triggers for stale ones.

        Returns:
            The number of sync triggers successfully published in this cycle.
        """
        start = time.monotonic()
        triggered = 0

        try:
            sources = await self._load_active_sources()
            now = datetime.now(tz=UTC)

            for source in sources:
                ttl = self._effective_ttl(source)
                if ttl is None:
                    # No SLA and no per-type default — skip.
                    continue

                if self._is_stale(source, now, ttl):
                    published = await self._publish_trigger(source)
                    if published:
                        triggered += 1
                        source_type = (
                            str(source.type.value)
                            if hasattr(source.type, "value")
                            else str(source.type)
                        )
                        SCHEDULER_SYNCS_TRIGGERED_TOTAL.labels(source_type=source_type).inc()
                        log.info(
                            "scheduler_sync_triggered",
                            source_id=str(source.id),
                            source_name=source.name,
                            source_type=source_type,
                            ttl_seconds=ttl,
                        )
        finally:
            elapsed = time.monotonic() - start
            SCHEDULER_CHECK_DURATION_SECONDS.observe(elapsed)

        return triggered

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_active_sources(self) -> list[Source]:
        """Fetch all active sources from the database in a single query."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(Source).where(Source.status == SourceStatus.active)
            )
            return list(result.scalars().all())

    def _effective_ttl(self, source: Source) -> int | None:
        """Return the TTL (seconds) to use for *source*.

        Priority:
        1. ``source.freshness_sla_seconds`` when explicitly set.
        2. :attr:`DEFAULT_TTLS` entry for the source type.
        3. ``None`` — no TTL applies, source is never auto-triggered.
        """
        if source.freshness_sla_seconds is not None:
            return source.freshness_sla_seconds

        source_type = str(source.type.value) if hasattr(source.type, "value") else str(source.type)
        return self.DEFAULT_TTLS.get(source_type)

    @staticmethod
    def _is_stale(source: Source, now: datetime, ttl: int) -> bool:
        """Return True when *source* has exceeded its *ttl*.

        A source that has never been synced (``last_sync_at is None``) is
        always considered stale when a TTL is configured.
        """
        if source.last_sync_at is None:
            return True

        last_sync = source.last_sync_at
        sync_aware = last_sync if last_sync.tzinfo else last_sync.replace(tzinfo=UTC)
        age_seconds = (now - sync_aware).total_seconds()
        return age_seconds > ttl

    async def _publish_trigger(self, source: Source) -> bool:
        """Publish a full-sync trigger event to NATS for *source*.

        Returns:
            True if the event was successfully published, False otherwise.
        """
        nats_conn = self._nats_conn
        if nats_conn is None:
            log.warning("scheduler_nats_unavailable", source_id=str(source.id))
            return False

        js = getattr(nats_conn, "jetstream", None)
        if js is None:
            log.warning("scheduler_nats_no_jetstream", source_id=str(source.id))
            return False

        source_type = str(source.type.value) if hasattr(source.type, "value") else str(source.type)
        event = DocumentChangeEvent(
            source_id=source.id,
            source_type=source_type,
            external_id="*",
            uri=f"sync://{source.id}",
            action="updated",
        )
        subject = f"ingest.changes.{source_type}"
        producer = QueueProducer(js)

        try:
            await producer.publish(subject=subject, payload=event)
            log.debug(
                "scheduler_event_published",
                subject=subject,
                source_id=str(source.id),
            )
            return True
        except Exception as exc:
            log.error(
                "scheduler_publish_error",
                source_id=str(source.id),
                source_type=source_type,
                error=str(exc),
            )
            return False

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def _make_run_id(self) -> uuid.UUID:
        """Generate a fresh UUID for an auto-triggered sync run."""
        return uuid.uuid4()


__all__ = ["SchedulerWorker"]
