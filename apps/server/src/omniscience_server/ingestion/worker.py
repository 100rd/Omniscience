"""Ingestion worker: NATS consumer that drives the per-document pipeline.

:class:`IngestionWorker` consumes ``DocumentChangeEvent`` messages from the
``INGEST_CHANGES`` stream, passes each through :class:`IngestionPipeline`,
updates :class:`RunTracker` counters, and acks/naks the broker accordingly.

Responsibilities (issue #126)
-----------------------------

The worker is the single place where ``Source.tenant_id`` is converted
into the ``workspace_id`` that every Neo4j / Qdrant adapter call must
carry.  Doing this BEFORE the pipeline runs means:

1. The pipeline never has to handle a ``None`` workspace.
2. A tenant-less source is rejected with a clear, structured-log error
   (``workspace_resolution_failed``) and surfaced as a hard failure so
   the broker NAKs the message and (after ``max_deliver``) routes it
   to the DLQ via the existing failure path.
3. Adapter-side ``MissingWorkspaceError``-equivalents bubble up as
   pipeline errors and are treated identically to other adapter
   failures — never swallowed.

Design decisions:
- A ``nak()`` is issued on pipeline errors so the broker can redeliver
  up to ``max_deliver`` times.  After ``max_deliver`` the queue
  framework routes the message to the DLQ transparently.
- ``stop()`` signals the consumer iterator to drain the current batch
  and exit; the worker coroutine completes cleanly.
- One ``IngestionRun`` row covers the entire worker lifetime so
  counters aggregate across all processed documents.
- Secrets are resolved at message-processing time via
  :class:`~omniscience_core.secrets.SecretsResolver`.  Resolution
  failures (missing env vars, unreadable files) produce an error
  result so the broker can redeliver or route to the DLQ.
"""

from __future__ import annotations

import os
import uuid

import structlog
from omniscience_connectors.registry import ConnectorRegistry
from omniscience_core.db.models import Source
from omniscience_core.queue.consumer import QueueConsumer
from omniscience_core.secrets import SecretsResolver
from omniscience_embeddings.base import EmbeddingProvider
from omniscience_index.workspace import MissingWorkspaceError, resolve_source_workspace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from omniscience_server.ingestion.dedup import (
    DedupAction,
    DedupConfig,
    DedupGate,
    config_from_env,
)
from omniscience_server.ingestion.events import DocumentChangeEvent, ProcessResult
from omniscience_server.ingestion.metrics import INGESTION_DOCUMENTS_PROCESSED_TOTAL
from omniscience_server.ingestion.pipeline import IndexWriterProtocol, IngestionPipeline
from omniscience_server.ingestion.run_tracker import RunTracker

# Environment variables that drive the dedup gate. Read once at worker
# construction; not re-read per event.  See ``ingestion/dedup.py`` for
# the full semantics.
_DEDUP_ENABLED_ENV: str = "OMNISCIENCE_DEDUP_ENABLED"
_DEDUP_TTL_HOURS_ENV: str = "OMNISCIENCE_INGEST_DEDUP_TTL_HOURS"
# Issue #216 / ADR-0011: when set to "false", k8s-agentic events are
# dropped at the dedup gate boundary.  Default unset => True in v0.3;
# v0.4 will ship "false" as the operator-shipped default.
_AGENTIC_ALLOWED_ENV: str = "OMNISCIENCE_K8S_AGENTIC_ALLOWED"

# Action label emitted when the dedup gate drops an event.  Distinct
# from the standard pipeline outcomes so dashboards can chart "events
# accepted by ingestion but dropped by dedup" without confusion with
# "no content change".
_DEDUP_DROP_ACTION: str = "dedup_dropped"

log = structlog.get_logger(__name__)


class IngestionWorker:
    """Consumes document change events and runs the ingestion pipeline.

    Args:
        queue_consumer: Typed consumer for ``DocumentChangeEvent`` messages.
        connector_registry: Registry used to look up connectors by source type.
        embedding_provider: Backend used to generate embedding vectors.
        index_writer: Writer for the Postgres operational metadata.
        graph_store: Neo4j adapter used by the pipeline's graph stage.
        vector_store: Qdrant adapter used by the pipeline's vector stage.
        session_factory: SQLAlchemy async session factory for run tracking
            and workspace resolution.
        secrets_resolver: Resolver for ``secrets_ref`` strings.  When ``None``
            a default :class:`~omniscience_core.secrets.SecretsResolver` is
            created automatically.
    """

    def __init__(
        self,
        queue_consumer: QueueConsumer[DocumentChangeEvent],
        connector_registry: ConnectorRegistry,
        embedding_provider: EmbeddingProvider,
        index_writer: IndexWriterProtocol,
        session_factory: async_sessionmaker[AsyncSession],
        secrets_resolver: SecretsResolver | None = None,
        dedup_gate: DedupGate | None = None,
    ) -> None:
        self._consumer = queue_consumer
        self._connector_registry = connector_registry
        self._embedding_provider = embedding_provider
        self._index_writer = index_writer
        self._session_factory = session_factory
        self._run_tracker = RunTracker(session_factory)
        self._secrets_resolver = secrets_resolver or SecretsResolver()
        self._run_id: uuid.UUID | None = None
        self._error_count = 0

        # Dedup gate: env-driven by default, injectable for tests.  The
        # gate is intentionally constructed up front rather than lazily
        # so a misconfigured env var fails fast at worker startup
        # instead of on the first message.
        if dedup_gate is None:
            config: DedupConfig = config_from_env(
                enabled_env=os.environ.get(_DEDUP_ENABLED_ENV),
                ttl_hours_env=os.environ.get(_DEDUP_TTL_HOURS_ENV),
                agentic_allowed_env=os.environ.get(_AGENTIC_ALLOWED_ENV),
            )
            self._dedup_gate: DedupGate = DedupGate(
                session_factory=session_factory,
                config=config,
            )
        else:
            self._dedup_gate = dedup_gate

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
        """Fetch, parse, embed, and index a single document change event.

        Workflow per document:

        1. Resolve ``workspace_id`` from the source row's ``tenant_id``.
           A null tenant produces an error result (structured log
           ``workspace_resolution_failed``) and the broker NAKs.
        2. Resolve secrets from the event's ``secrets_ref`` (when set).
        3. Run the per-document :class:`IngestionPipeline` with the
           resolved workspace.

        Resolution failures and adapter ``MissingWorkspaceError`` raises
        surface as ``action="error"`` so the broker redelivers /
        routes to the DLQ.
        """
        try:
            workspace_id = await self._resolve_workspace(event.source_id)
        except MissingWorkspaceError as exc:
            log.error(
                "workspace_resolution_failed",
                source_id=str(event.source_id),
                source_name=exc.source_name,
                external_id=event.external_id,
            )
            return ProcessResult(
                source_id=event.source_id,
                external_id=event.external_id,
                action="error",
                duration_ms=0.0,
                error=str(exc),
            )

        # Dedup gate (issue #164) — runs BEFORE the per-document pipeline so
        # rejected events never touch the index/vector/graph adapters.
        # ``workspace_id`` was just resolved from ``Source.tenant_id`` —
        # it is server-derived, never event-payload-derived (ACL invariant).
        # Delete events are exempt: tombstoning is idempotent on the
        # adapter side and does not write new content into the graph;
        # blocking deletes by emitter would otherwise leave orphaned
        # rows when the operator's coverage shrinks.
        if event.action != "deleted":
            decision = await self._dedup_gate.evaluate(
                workspace_id=workspace_id,
                event=event,
            )
            if decision.action == DedupAction.drop:
                log.debug(
                    "ingestion_event_dropped_by_dedup",
                    source_id=str(event.source_id),
                    external_id=event.external_id,
                    source_type=event.source_type,
                    authority_emitter=decision.authority_emitter,
                    reason=decision.reason,
                )
                INGESTION_DOCUMENTS_PROCESSED_TOTAL.labels(
                    source_type=event.source_type,
                    action=_DEDUP_DROP_ACTION,
                ).inc()
                return ProcessResult(
                    source_id=event.source_id,
                    external_id=event.external_id,
                    action=_DEDUP_DROP_ACTION,
                    duration_ms=0.0,
                )

        connector = self._connector_registry.get(event.source_type)

        # Resolve secrets for this source. The secrets_ref attribute may be
        # added to DocumentChangeEvent in a future extension; for now we
        # gracefully fall back to None (empty secrets) if not present.
        secrets_ref: str | None = getattr(event, "secrets_ref", None)
        secrets = self._secrets_resolver.resolve(secrets_ref)

        pipeline = IngestionPipeline(
            connector=connector,
            embedding_provider=self._embedding_provider,
            index_writer=self._index_writer,
        )
        result = await pipeline.run(
            event=event,
            config=None,
            secrets=secrets,
            workspace_id=workspace_id,
            ingestion_run_id=self._run_id,
        )
        INGESTION_DOCUMENTS_PROCESSED_TOTAL.labels(
            source_type=event.source_type,
            action=result.action,
        ).inc()
        return result

    # ------------------------------------------------------------------
    # Workspace resolution
    # ------------------------------------------------------------------

    async def _resolve_workspace(self, source_id: uuid.UUID) -> uuid.UUID:
        """Look up the ``Source`` and resolve its ``workspace_id``.

        Reuses :func:`omniscience_index.workspace.resolve_source_workspace`
        — the same helper the migration runner uses — so live ingestion
        and the legacy backfill follow identical ACL rules.

        Raises
        ------
        MissingWorkspaceError
            When the source row is missing OR its ``tenant_id`` is null.
            The worker treats both as the same hard failure: a document
            has no business being indexed without a workspace anchor.
        """
        async with self._session_factory() as session:
            source = await self._fetch_source(session, source_id)
        if source is None:
            # Treat a missing source row as the same fail-closed outcome
            # as a tenant-less source — the worker has no workspace to
            # write under either way.  We construct an explicit
            # MissingWorkspaceError so the caller's error handling path
            # is identical to the canonical case.
            raise MissingWorkspaceError(
                source_id=source_id,
                source_name="<source-not-found>",
            )
        return resolve_source_workspace(source)

    @staticmethod
    async def _fetch_source(session: AsyncSession, source_id: uuid.UUID) -> Source | None:
        """Fetch a single ``Source`` row by id.  Read-only query."""
        stmt = select(Source).where(Source.id == source_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

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
        # ``dedup_dropped`` (#164) and ``unchanged`` are deliberately
        # silent on the run tracker: neither outcome represents work the
        # tracker should count.  The metric on
        # ``INGESTION_DOCUMENTS_PROCESSED_TOTAL`` already records both.


__all__ = ["IngestionWorker"]
