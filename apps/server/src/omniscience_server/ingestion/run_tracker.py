"""Ingestion run tracking.

:class:`RunTracker` creates an :class:`~omniscience_core.db.models.IngestionRun`
row at the start of a sync and atomically increments its counters as each
document is processed.  On completion it stamps ``finished_at`` and sets the
final status.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from omniscience_core.db.models import IngestionRun, IngestionRunStatus
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


class RunTracker:
    """Create and update IngestionRun records during a sync.

    All counter updates use atomic SQL (no read-modify-write) so concurrent
    workers for the same run do not race.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def start(self, source_id: uuid.UUID) -> uuid.UUID:
        """Insert a new IngestionRun row with status=running.

        Returns the new run's primary key.
        """
        run_id = uuid.uuid4()
        run = IngestionRun(
            id=run_id,
            source_id=source_id,
            started_at=datetime.now(UTC),
            status=IngestionRunStatus.running,
        )
        async with self._session_factory() as session, session.begin():
            session.add(run)
        log.info("ingestion_run_started", source_id=str(source_id), run_id=str(run_id))
        return run_id

    async def record_new(self, run_id: uuid.UUID) -> None:
        """Increment docs_new by 1."""
        await self._increment(run_id, "docs_new")

    async def record_updated(self, run_id: uuid.UUID) -> None:
        """Increment docs_updated by 1."""
        await self._increment(run_id, "docs_updated")

    async def record_removed(self, run_id: uuid.UUID) -> None:
        """Increment docs_removed by 1."""
        await self._increment(run_id, "docs_removed")

    async def record_error(self, run_id: uuid.UUID, external_id: str, error: str) -> None:
        """Append an error entry to the run_errors JSONB column."""
        async with self._session_factory() as session, session.begin():
            run = await self._get_run(session, run_id)
            if run is None:
                return
            errors: dict[str, Any] = dict(run.run_errors)
            errors[external_id] = error
            run.run_errors = errors

    async def finish(self, run_id: uuid.UUID, *, had_errors: bool) -> None:
        """Stamp finished_at and set the terminal status."""
        status = IngestionRunStatus.partial if had_errors else IngestionRunStatus.ok
        async with self._session_factory() as session, session.begin():
            run = await self._get_run(session, run_id)
            if run is None:
                log.warning("ingestion_run_not_found_on_finish", run_id=str(run_id))
                return
            run.finished_at = datetime.now(UTC)
            run.status = status
        log.info("ingestion_run_finished", run_id=str(run_id), status=status)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _increment(self, run_id: uuid.UUID, column: str) -> None:
        """Add 1 to *column* on the IngestionRun row."""
        async with self._session_factory() as session, session.begin():
            run = await self._get_run(session, run_id)
            if run is None:
                return
            current = int(getattr(run, column, 0))
            setattr(run, column, current + 1)

    @staticmethod
    async def _get_run(session: AsyncSession, run_id: uuid.UUID) -> IngestionRun | None:
        stmt = select(IngestionRun).where(IngestionRun.id == run_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


__all__ = ["RunTracker"]
