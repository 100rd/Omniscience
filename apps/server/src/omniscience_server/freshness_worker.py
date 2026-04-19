"""Freshness SLO background worker.

:class:`FreshnessWorker` runs as a persistent asyncio task for the lifetime
of the server process.  On each tick it:

1. Calls :class:`~omniscience_core.freshness.FreshnessChecker` to evaluate
   every source against its ``freshness_sla_seconds`` budget.
2. Updates the two Prometheus gauges:
   - ``omniscience_source_freshness_age_seconds`` — per-source age label set
   - ``omniscience_source_stale_total``            — count of stale sources
3. Emits a ``WARNING`` structured log for every stale source so that log-
   based alerting tools (Loki, CloudWatch, etc.) can trigger without Grafana.

Usage in ``app.py`` lifespan::

    from omniscience_server.freshness_worker import FreshnessWorker

    freshness_worker = FreshnessWorker(session_factory=session_factory)
    freshness_task = asyncio.create_task(freshness_worker.start())
    app.state.freshness_worker = freshness_worker
    ...
    yield
    ...
    freshness_worker.stop()
    freshness_task.cancel()
"""

from __future__ import annotations

import asyncio
import math

import structlog
from omniscience_core.freshness import FreshnessChecker, FreshnessReport
from omniscience_core.telemetry.metrics import FRESHNESS_AGE_SECONDS, FRESHNESS_STALE_TOTAL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

# Sentinel age value written to Prometheus when a source has never been synced.
# Using a concrete large number rather than +inf keeps Grafana panels renderable.
_NEVER_SYNCED_AGE_SENTINEL: float = 1e15


class FreshnessWorker:
    """Periodic freshness SLO checker that drives Prometheus metrics.

    Args:
        session_factory: SQLAlchemy async session factory passed down from the
                         FastAPI app lifespan.
        interval_seconds: How often (in wall-clock seconds) to re-evaluate
                          all sources.  Defaults to 60.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        interval_seconds: float = 60.0,
    ) -> None:
        self._checker = FreshnessChecker(session_factory)
        self._interval = interval_seconds
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Run the worker loop until :meth:`stop` is called."""
        self._running = True
        log.info("freshness_worker_started", interval_seconds=self._interval)

        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                # Never let a single failed check crash the worker.
                log.error("freshness_worker_error", error=str(exc))

            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

        log.info("freshness_worker_stopped")

    def stop(self) -> None:
        """Signal the worker to exit after the current tick completes."""
        self._running = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        """Run one freshness evaluation cycle."""
        reports: list[FreshnessReport] = await self._checker.check_all()

        stale_count = 0
        for report in reports:
            src_id = str(report.source_id)
            src_name = report.source_name

            # Compute a finite age for Prometheus even when never synced.
            prometheus_age = (
                _NEVER_SYNCED_AGE_SENTINEL
                if math.isinf(report.age_seconds)
                else report.age_seconds
            )
            FRESHNESS_AGE_SECONDS.labels(
                source_id=src_id,
                source_name=src_name,
            ).set(prometheus_age)

            if report.is_stale:
                stale_count += 1
                log.warning(
                    "source_stale",
                    source_id=src_id,
                    source_name=src_name,
                    freshness_sla_seconds=report.freshness_sla_seconds,
                    age_seconds=report.age_seconds,
                    staleness_margin_seconds=report.staleness_margin_seconds,
                )

        FRESHNESS_STALE_TOTAL.set(stale_count)
        log.debug(
            "freshness_tick_complete",
            total_sources=len(reports),
            stale_count=stale_count,
        )


__all__ = ["FreshnessWorker"]
