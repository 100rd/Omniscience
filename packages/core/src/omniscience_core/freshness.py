"""Freshness SLO checker for Omniscience sources.

:class:`FreshnessChecker` evaluates whether each source is within its
configured ``freshness_sla_seconds`` budget.  A :class:`FreshnessReport` is
produced per source and can be used by:

- The REST API (``GET /api/v1/freshness``)
- The background worker that updates Prometheus metrics and logs warnings
- The MCP tool layer that decorates ``list_sources`` / ``source_stats`` results

Staleness semantics
-------------------
- ``freshness_sla_seconds`` is ``None`` → no SLO configured, never stale
- ``last_sync_at`` is ``None`` and SLO is set → treat as stale (age = infinity)
- ``last_sync_at`` is set and SLO is set →
    ``age_seconds = now - last_sync_at``
    ``is_stale = age_seconds > freshness_sla_seconds``
    ``staleness_margin_seconds = age_seconds - freshness_sla_seconds``
      (positive = past SLA, negative = within SLA)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from omniscience_core.db.models import Source

log = structlog.get_logger(__name__)

# Sentinel value for "age" when last_sync_at is None but an SLO is configured.
_INFINITY_AGE: float = float("inf")


class FreshnessReport(BaseModel):
    """Freshness evaluation result for a single source."""

    source_id: uuid.UUID
    source_name: str
    freshness_sla_seconds: int | None
    last_sync_at: datetime | None
    age_seconds: float
    """Elapsed seconds since last successful sync.  ``inf`` when never synced."""

    is_stale: bool
    """True when age_seconds exceeds the SLO, or when never synced and SLO is set."""

    staleness_margin_seconds: float
    """age_seconds - freshness_sla_seconds.
    Positive  → source is overdue by this many seconds.
    Negative  → source is still within its SLO by this many seconds.
    0.0       → on the exact boundary.
    inf / nan → SLO not configured (no meaningful margin).
    """


def _compute_report(source: Source, now: datetime) -> FreshnessReport:
    """Derive a :class:`FreshnessReport` from a *Source* ORM row."""
    sla = source.freshness_sla_seconds
    last_sync = source.last_sync_at

    if sla is None:
        # No SLO — never considered stale regardless of age.
        if last_sync is not None:
            # Make last_sync timezone-aware for consistent arithmetic.
            sync_aware = last_sync if last_sync.tzinfo else last_sync.replace(tzinfo=UTC)
            age = (now - sync_aware).total_seconds()
        else:
            age = _INFINITY_AGE
        return FreshnessReport(
            source_id=source.id,
            source_name=source.name,
            freshness_sla_seconds=None,
            last_sync_at=last_sync,
            age_seconds=age,
            is_stale=False,
            staleness_margin_seconds=float("nan"),
        )

    if last_sync is None:
        # SLO configured but never synced → immediately stale.
        return FreshnessReport(
            source_id=source.id,
            source_name=source.name,
            freshness_sla_seconds=sla,
            last_sync_at=None,
            age_seconds=_INFINITY_AGE,
            is_stale=True,
            staleness_margin_seconds=_INFINITY_AGE,
        )

    sync_aware = last_sync if last_sync.tzinfo else last_sync.replace(tzinfo=UTC)
    age = (now - sync_aware).total_seconds()
    margin = age - sla
    is_stale = age > sla

    return FreshnessReport(
        source_id=source.id,
        source_name=source.name,
        freshness_sla_seconds=sla,
        last_sync_at=last_sync,
        age_seconds=age,
        is_stale=is_stale,
        staleness_margin_seconds=margin,
    )


class FreshnessChecker:
    """Evaluates freshness SLOs for all configured sources.

    Args:
        session_factory: SQLAlchemy async session factory.  Sessions are
                         acquired per-call so the checker is safe to call
                         from any coroutine, including background tasks.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def check_all(self) -> list[FreshnessReport]:
        """Return a freshness report for every source in the database.

        A consistent ``now`` timestamp is captured once per call so that
        relative ages are comparable across all sources in the same batch.
        """
        now = datetime.now(tz=UTC)
        session: AsyncSession
        async with self._session_factory() as session:
            result = await session.execute(select(Source))
            sources = result.scalars().all()

        reports = [_compute_report(s, now) for s in sources]
        log.debug("freshness_check_all", count=len(reports))
        return reports

    async def check_source(self, source_id: uuid.UUID) -> FreshnessReport:
        """Return the freshness report for a single source.

        Args:
            source_id: The UUID of the source to evaluate.

        Raises:
            KeyError: When no source with the given id exists.
        """
        now = datetime.now(tz=UTC)
        session: AsyncSession
        async with self._session_factory() as session:
            source = await session.get(Source, source_id)

        if source is None:
            raise KeyError(f"Source {source_id} not found")

        report = _compute_report(source, now)
        log.debug(
            "freshness_check_source",
            source_id=str(source_id),
            is_stale=report.is_stale,
            age_seconds=report.age_seconds,
        )
        return report


__all__ = ["FreshnessChecker", "FreshnessReport"]
