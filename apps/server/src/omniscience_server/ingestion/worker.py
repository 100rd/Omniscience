"""Ingestion worker: NATS consumer that drives the per-document pipeline.

:class:`IngestionWorker` consumes ``DocumentChangeEvent`` messages from the
``INGEST_CHANGES`` stream, passes each through :class:`IngestionPipeline`,
updates :class:`RunTracker` counters, and acks/naks the broker accordingly.

Design decisions:
- A ``nak()`` is issued on pipeline errors so the broker can redeliver up to
  ``max_deliver`` times.  After ``max_deliver`` the queue framework routes the
  message to the DLQ transparently.
- ``stop()`` signals the consumer iterator to drain the current batch and exit;
  the worker coroutine completes cleanly.
- One ``IngestionRun`` row covers the entire worker lifetime so counters
  aggregate across all processed documents.
"""

from __future__ import annotations

import uuid

import structlog
from omniscience_connectors.registry import ConnectorRegistry
from omniscience_core.queue.consumer import QueueConsumer
from omniscience_embeddings.base import EmbeddingProvider
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from omniscience_server.ingestion.events import DocumentChangeEvent, ProcessResult
from omniscience_server.ingestion.metrics import INGESTION_DOCUMENTS_PROCESSED_TOTAL
from omniscience_server.ingestion.pipeline import IndexWriterProtocol, IngestionPipeline
from omniscience_server.ingestion.run_tracker import RunTracker

log = structlog.get_logger(__name__)


class IngestionWorker:
    """Consumes document change events and runs the ingestion pipeline.

    Args:
        queue_consumer: Typed consumer for ``DocumentChangeEvent`` messages.
        connector_registry: Registry used to look up connectors by source type.
        embedding_provider: Backend used to generate embedding vectors.
        index_writer: Writer for the document/chunk index.
        session_factory: SQLAlchemy async session factory for run tracking.
    """

    def __init__(
        self,
        queue_consumer: QueueConsumer[DocumentChangeEvent],
        connector_registry: ConnectorRegistry,
        embedding_provider: EmbeddingProvider,
        index_writer: IndexWriterProtocol,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._consumer = queue_consumer
        self._connector_registry = connector_registry
        self._embedding_provider = embedding_provider
        self._index_writer = index_writer
        self._run_tracker = RunTracker(session_factory)
        self._run_id: uuid.UUID | None = None
        self._error_count = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start consuming messages and processing documents."""
        log.info("ingestion_worker_starting")
        async for msg in self._consumer:
            event = msg.payload
            try:
                result = await self.process_document(event)
            except Exception as exc:
                log.error(
                    "ingestion_worker_unhandled_error",
                    source_id=str(event.source_id),
                    external_id=event.external_id,
                    error=str(exc),
                )
                await msg.nak()
                continue

            await self._update_run(result)
            if result.action == "error":
                await msg.nak()
            else:
                await msg.ack()

        log.info("ingestion_worker_stopped")

    async def stop(self) -> None:
        """Gracefully stop the worker after the current batch completes."""
        log.info("ingestion_worker_stop_requested")
        self._consumer.stop()
        if self._run_id is not None:
            await self._run_tracker.finish(self._run_id, had_errors=self._error_count > 0)

    # ------------------------------------------------------------------
    # Per-document processing
    # ------------------------------------------------------------------

    async def process_document(self, event: DocumentChangeEvent) -> ProcessResult:
        """Fetch, parse, embed, and index a single document change event."""
        connector = self._connector_registry.get(event.source_type)
        pipeline = IngestionPipeline(
            connector=connector,
            embedding_provider=self._embedding_provider,
            index_writer=self._index_writer,
        )
        # Config and secrets are empty for now; real resolution wired in Wave 7.
        result = await pipeline.run(
            event=event,
            config=None,
            secrets={},
            ingestion_run_id=self._run_id,
        )
        INGESTION_DOCUMENTS_PROCESSED_TOTAL.labels(
            source_type=event.source_type,
            action=result.action,
        ).inc()
        return result

    # ------------------------------------------------------------------
    # Run tracking helpers
    # ------------------------------------------------------------------

    async def _ensure_run(self, source_id: uuid.UUID) -> None:
        """Lazily create the IngestionRun row on first processed document."""
        if self._run_id is None:
            self._run_id = await self._run_tracker.start(source_id)

    async def _update_run(self, result: ProcessResult) -> None:
        """Update run counters and error log from a pipeline result."""
        await self._ensure_run(result.source_id)
        run_id = self._run_id
        if run_id is None:  # pragma: no cover
            raise RuntimeError("run_id unexpectedly None after _ensure_run")

        if result.action == "created":
            await self._run_tracker.record_new(run_id)
        elif result.action == "updated":
            await self._run_tracker.record_updated(run_id)
        elif result.action == "deleted":
            await self._run_tracker.record_removed(run_id)
        elif result.action == "error":
            self._error_count += 1
            error_msg = result.error or "unknown error"
            await self._run_tracker.record_error(run_id, result.external_id, error_msg)


__all__ = ["IngestionWorker"]
