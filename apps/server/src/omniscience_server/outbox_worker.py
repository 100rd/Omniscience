"""Outbox polling background worker."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import structlog
from omniscience_core.db.models import OutboxEvent
from omniscience_core.queue import NatsConnection, QueueProducer
from omniscience_core.queue.messages import (
    EdgeUpsertEvent,
    EntityMergeEvent,
    EntityUpsertEvent,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

# Nanosecond offset so same-millisecond events have stable relative ordering.
_NS_PER_TICK = 1_000


class OutboxWorker:
    """Polls the outbox table and publishes events to NATS JetStream."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        nats_conn: NatsConnection,
        interval_seconds: float = 1.0,
        batch_size: int = 50,
    ) -> None:
        self._session_factory = session_factory
        self._nats_conn = nats_conn
        self._interval = interval_seconds
        self._batch_size = batch_size
        self._producer: QueueProducer | None = None
        self._running = False

    async def start(self) -> None:
        """Run the worker loop until stop() is called."""
        self._running = True
        log.info("outbox_worker_started", interval_seconds=self._interval)

        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("outbox_worker_error", error=str(exc))

            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

        log.info("outbox_worker_stopped")

    def stop(self) -> None:
        """Signal the worker to exit."""
        self._running = False

    async def _tick(self) -> None:
        """Process one batch of outbox events."""
        if not self._nats_conn or not self._nats_conn.is_connected:
            log.debug("outbox_worker_nats_not_connected")
            return

        if self._producer is None:
            self._producer = QueueProducer(self._nats_conn.jetstream)

        async with self._session_factory() as session, session.begin():
            # Select oldest unprocessed events
            stmt = (
                select(OutboxEvent)
                .where(OutboxEvent.processed == False)  # noqa: E712
                .order_by(OutboxEvent.created_at.asc())
                .limit(self._batch_size)
                .with_for_update(skip_locked=True)
            )
            result = await session.execute(stmt)
            events = result.scalars().all()

            if not events:
                return

            log.debug("outbox_polling_found_events", count=len(events))

            base_time = time.time_ns()
            for _i, event in enumerate(events):
                try:
                    subject: str
                    payload: EntityUpsertEvent | EdgeUpsertEvent | EntityMergeEvent
                    if event.event_type == "entity.upsert":
                        payload = EntityUpsertEvent.model_validate(event.payload)
                        subject = "outbox.entity.upsert"
                    elif event.event_type == "edge.upsert":
                        payload = EdgeUpsertEvent.model_validate(event.payload)
                        subject = "outbox.edge.upsert"
                    elif event.event_type in ("entity.merge", "entity.unmerge"):
                        payload = EntityMergeEvent.model_validate(event.payload)
                        subject = f"outbox.{event.event_type}"
                    else:
                        log.warning("outbox_unknown_event_type", event_type=event.event_type)
                        event.processed = True
                        event.processed_at = datetime.now(UTC)
                        continue

                    # Publish to JetStream
                    await self._producer.publish(subject, payload)

                    # Mark as processed
                    event.processed = True
                    event.processed_at = datetime.now(UTC)
                except Exception as exc:
                    log.error(
                        "outbox_event_publish_failed",
                        event_id=str(event.id),
                        event_type=event.event_type,
                        error=str(exc),
                    )
                    # We don't mark as processed so it will be retried on next tick
            _ = base_time  # kept for future tracing (i removed as unused)
