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

Discovery fan-out (spec: discovery-sync-worker.md)
--------------------------------------------------

A sync marker event (``external_id == "*"`` / ``uri.startswith("sync://")``)
signals a full re-sync of the source.  For connectors that implement
``discover()``, the worker calls it to obtain a stream of
:class:`~omniscience_connectors.base.DocumentRef` objects, then runs the
pipeline on each ref concurrently (bounded by :data:`_DISCOVERY_CONCURRENCY`).

Config validation is **fail-closed**: invalid ``Source.config`` →
``action="error"``, structured log, NAK — never silently falls back to
``config=None``.  Secrets are resolved from ``source.secrets_ref`` (server-
side), never from the event payload.

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

import asyncio
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from omniscience_connectors.base import Connector, DocumentRef
from omniscience_connectors.registry import ConnectorRegistry
from omniscience_core.db.models import Source
from omniscience_core.queue.consumer import QueueConsumer
from omniscience_core.secrets import SecretsResolver
from omniscience_embeddings.base import EmbeddingProvider
from omniscience_index.workspace import MissingWorkspaceError, resolve_source_workspace
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from omniscience_server.ingestion.dedup import (
    DedupAction,
    DedupConfig,
    DedupGate,
    config_from_env,
)
from omniscience_server.ingestion.events import DocumentChangeEvent, ProcessResult
from omniscience_server.ingestion.metrics import INGESTION_DOCUMENTS_PROCESSED_TOTAL
from omniscience_server.ingestion.operator_graph import (
    OperatorEvent,
    OperatorEventEdge,
    route_operator_event_to_graph,
)
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

# Maximum number of discovered refs processed concurrently per sync event.
# Limits simultaneous cluster-API / embedder pressure during discovery fan-out.
_DISCOVERY_CONCURRENCY: int = 10

# source_type the in-cluster Go operator stamps on its events
# (``operator/internal/entity/entity.go``).  Only events from this emitter
# carry the rich ``metadata``/``topology_edges`` payload the graph bridge
# (Gap A) consumes; everything else flows through the vector path only.
_OPERATOR_SOURCE_TYPE: str = "k8s-operator"

log = structlog.get_logger(__name__)


class IngestionWorker:
    """Consumes document change events and runs the ingestion pipeline.

    Args:
        queue_consumer: Typed consumer for ``DocumentChangeEvent`` messages.
        connector_registry: Registry used to look up connectors by source type.
        embedding_provider: Backend used to generate embedding vectors.
        index_writer: Writer for the Postgres operational metadata.
        session_factory: SQLAlchemy async session factory for run tracking
            and workspace resolution.
        secrets_resolver: Resolver for ``secrets_ref`` strings.  When ``None``
            a default :class:`~omniscience_core.secrets.SecretsResolver` is
            created automatically.
        graph_store: Optional ``GraphStore`` used to route operator-sourced
            events (``source_type == "k8s-operator"``) into the Neo4j graph
            (Gap A bridge).  When ``None`` the operator->graph routing is
            disabled and only the vector pipeline runs — the previous
            behaviour, preserved verbatim for non-operator deployments.
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
        graph_store: Any | None = None,
    ) -> None:
        self._consumer = queue_consumer
        self._connector_registry = connector_registry
        self._embedding_provider = embedding_provider
        self._index_writer = index_writer
        self._session_factory = session_factory
        self._run_tracker = RunTracker(session_factory)
        self._secrets_resolver = secrets_resolver or SecretsResolver()
        # Optional GraphStore for the operator->graph bridge (Gap A).  Kept
        # separate from the orchestrated ``index_writer`` so the additive
        # operator-event routing never perturbs the symbol-graph write path
        # the pipeline already drives through ``index_writer.upsert_graph``.
        self._graph_store = graph_store
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
                await self._mark_source_synced(result.source_id)
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

        1. Load ``Source`` row and resolve ``workspace_id`` in one DB round-trip.
        2. Validate ``Source.config`` via ``connector.config_schema``.
           Invalid config → ``action="error"`` (fail-closed; never ``config=None``).
        3. Resolve secrets from ``source.secrets_ref`` (server-side only).
        4a. Sync marker (``external_id == "*"``): call ``connector.discover()``
            and fan out per-ref with bounded concurrency.
        4b. Single-doc event: run ``pipeline.run_ref`` with the validated config.

        ACL invariant: ``workspace_id`` always from ``Source.tenant_id``, never
        from event payload.
        """
        source, workspace_id = await self._load_source_and_workspace(event)
        if source is None or workspace_id is None:
            # Error already logged inside _load_source_and_workspace.
            return ProcessResult(
                source_id=event.source_id,
                external_id=event.external_id,
                action="error",
                duration_ms=0.0,
                error="workspace_resolution_failed: tenant_id is null or source not found",
            )

        # Dedup gate (issue #164) — runs BEFORE the per-document pipeline so
        # rejected events never touch the index/vector/graph adapters.
        # ``workspace_id`` was just resolved from ``Source.tenant_id`` —
        # it is server-derived, never event-payload-derived (ACL invariant).
        # Delete events are exempt: tombstoning is idempotent on the
        # adapter side and does not write new content into the graph;
        # blocking deletes by emitter would otherwise leave orphaned
        # rows when the operator's coverage shrinks.
        #
        # Sync marker events (external_id=="*") are also exempt from the
        # dedup gate — they are control messages, not documents.  The
        # per-ref pipeline.run_ref calls that result from the fan-out each
        # hit the dedup gate individually.
        if event.action != "deleted" and not _is_sync_marker(event):
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

        # Validate config fail-closed: never fall back to config=None.
        validated_config = self._validate_config(connector, source, event)
        if validated_config is None:
            return ProcessResult(
                source_id=event.source_id,
                external_id=event.external_id,
                action="error",
                duration_ms=0.0,
                error="source_config_invalid",
            )

        secrets = self._secrets_resolver.resolve(source.secrets_ref)

        pipeline = IngestionPipeline(
            connector=connector,
            embedding_provider=self._embedding_provider,
            index_writer=self._index_writer,
        )

        if _is_sync_marker(event):
            return await self._process_sync_event(
                event, connector, validated_config, secrets, workspace_id, pipeline
            )

        ref = DocumentRef(external_id=event.external_id, uri=event.uri)
        result = await pipeline.run_ref(
            event=event,
            ref=ref,
            config=validated_config,
            secrets=secrets,
            workspace_id=workspace_id,
            ingestion_run_id=self._run_id,
        )

        # Gap A bridge: operator-sourced events ALSO upsert into Neo4j.  This
        # runs AFTER the vector pipeline so the additive graph write never
        # changes the existing vector path's outcome.  Only created/updated
        # operator events carrying the rich payload are routed; a graph-store
        # adapter failure converts the result to ``error`` so the broker
        # redelivers (the upsert is idempotent — replay is safe).  The
        # workspace is the server-resolved one (ACL invariant) — NEVER taken
        # from the event payload.
        result = await self._maybe_route_operator_graph(event, result, workspace_id)

        INGESTION_DOCUMENTS_PROCESSED_TOTAL.labels(
            source_type=event.source_type,
            action=result.action,
        ).inc()
        return result

    # ------------------------------------------------------------------
    # Operator -> Neo4j graph bridge (Gap A)
    # ------------------------------------------------------------------

    async def _maybe_route_operator_graph(
        self,
        event: DocumentChangeEvent,
        result: ProcessResult,
        workspace_id: uuid.UUID,
    ) -> ProcessResult:
        """Route an operator event into the Neo4j graph, if applicable.

        No-op (returns ``result`` unchanged) unless ALL of:
          * a ``graph_store`` is configured on the worker,
          * the event came from the operator emitter,
          * the vector pipeline did not error and produced new content
            (``created``/``updated`` — never ``unchanged``/``deleted``), and
          * the event carries the rich operator ``metadata`` payload.

        Deletes are deliberately excluded — tombstoning is owned by the
        existing delete path, not this bridge (matching
        :func:`route_operator_event_to_graph`'s contract).

        On a graph adapter error the result is converted to ``action="error"``
        so the broker NAKs and redelivers; the operator upsert is idempotent
        (deterministic node ids), so re-running the vector path on redelivery
        is safe.
        """
        if self._graph_store is None:
            return result
        if event.source_type != _OPERATOR_SOURCE_TYPE:
            return result
        if result.action not in ("created", "updated"):
            return result
        if not event.metadata:
            return result

        operator_event = _to_operator_event(event, workspace_id)
        try:
            entities_written, edges_written = await route_operator_event_to_graph(
                graph_store=self._graph_store,
                event=operator_event,
                session_factory=self._session_factory,
            )
        except Exception as exc:
            log.error(
                "operator_graph_routing_failed",
                source_id=str(event.source_id),
                external_id=event.external_id,
                error=str(exc),
            )
            return result.model_copy(update={"action": "error", "error": str(exc)})

        log.debug(
            "operator_graph_routed",
            source_id=str(event.source_id),
            external_id=event.external_id,
            entities=entities_written,
            edges=edges_written,
        )
        return result

    # ------------------------------------------------------------------
    # Discovery fan-out
    # ------------------------------------------------------------------

    async def _process_sync_event(
        self,
        event: DocumentChangeEvent,
        connector: Connector,
        config: Any,
        secrets: dict[str, str],
        workspace_id: uuid.UUID,
        pipeline: IngestionPipeline,
    ) -> ProcessResult:
        """Fan out pipeline.run_ref over every ref yielded by connector.discover().

        Falls back to single-doc behaviour when ``connector.discover`` raises
        :class:`NotImplementedError` (single-doc connectors without discovery).

        Concurrency is bounded by :data:`_DISCOVERY_CONCURRENCY`.
        """
        log.info(
            "discovery_sync_starting",
            source_id=str(event.source_id),
            source_type=event.source_type,
        )

        try:
            refs = await _collect_refs(connector, config, secrets)
        except NotImplementedError:
            log.info(
                "discovery_not_implemented_fallback",
                source_id=str(event.source_id),
                source_type=event.source_type,
            )
            ref = DocumentRef(
                external_id=event.external_id,
                uri=event.uri,
            )
            result = await pipeline.run_ref(
                event=event,
                ref=ref,
                config=config,
                secrets=secrets,
                workspace_id=workspace_id,
                ingestion_run_id=self._run_id,
            )
            INGESTION_DOCUMENTS_PROCESSED_TOTAL.labels(
                source_type=event.source_type,
                action=result.action,
            ).inc()
            return result

        results = await _fan_out_refs(
            refs=refs,
            event=event,
            pipeline=pipeline,
            config=config,
            secrets=secrets,
            workspace_id=workspace_id,
            run_id=self._run_id,
            concurrency=_DISCOVERY_CONCURRENCY,
        )

        for r in results:
            INGESTION_DOCUMENTS_PROCESSED_TOTAL.labels(
                source_type=event.source_type,
                action=r.action,
            ).inc()

        error_count = sum(1 for r in results if r.action == "error")
        action = "error" if error_count == len(results) and results else "updated"
        log.info(
            "discovery_sync_complete",
            source_id=str(event.source_id),
            total=len(results),
            errors=error_count,
        )
        return ProcessResult(
            source_id=event.source_id,
            external_id=event.external_id,
            action=action,
            duration_ms=0.0,
        )

    # ------------------------------------------------------------------
    # Config validation
    # ------------------------------------------------------------------

    def _validate_config(
        self,
        connector: Connector,
        source: Source,
        event: DocumentChangeEvent,
    ) -> Any:
        """Validate ``source.config`` against ``connector.config_schema``.

        Returns the validated config model on success, or ``None`` on failure.
        Logs ``source_config_invalid`` without including the raw config values
        (which may contain sensitive fields) in the log.
        """
        try:
            return connector.config_schema.model_validate(source.config or {})
        except ValidationError as exc:
            log.error(
                "source_config_invalid",
                source_id=str(event.source_id),
                source_type=event.source_type,
                error_count=exc.error_count(),
            )
            return None

    # ------------------------------------------------------------------
    # Source / workspace loading (single DB round-trip)
    # ------------------------------------------------------------------

    async def _load_source_and_workspace(
        self,
        event: DocumentChangeEvent,
    ) -> tuple[Source, uuid.UUID] | tuple[None, None]:
        """Fetch the ``Source`` row and derive ``workspace_id`` in one query.

        Returns ``(source, workspace_id)`` on success, or ``(None, None)``
        on failure (missing row or null ``tenant_id``).  Error is logged
        before returning ``(None, None)``.

        Reuses :func:`omniscience_index.workspace.resolve_source_workspace`
        so live ingestion and the legacy backfill follow identical ACL rules.
        """
        async with self._session_factory() as session:
            source = await self._fetch_source(session, event.source_id)

        if source is None:
            log.error(
                "workspace_resolution_failed",
                source_id=str(event.source_id),
                source_name="<source-not-found>",
                external_id=event.external_id,
            )
            return None, None

        try:
            workspace_id = resolve_source_workspace(source)
        except MissingWorkspaceError as exc:
            log.error(
                "workspace_resolution_failed",
                source_id=str(event.source_id),
                source_name=exc.source_name,
                external_id=event.external_id,
                error=str(exc),
            )
            return None, None

        return source, workspace_id

    @staticmethod
    async def _fetch_source(session: AsyncSession, source_id: uuid.UUID) -> Source | None:
        """Fetch a single ``Source`` row by id.  Read-only query."""
        stmt = select(Source).where(Source.id == source_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _mark_source_synced(self, source_id: uuid.UUID) -> None:
        """Stamp ``Source.last_sync_at`` after a document is successfully synced.

        Runs in the broker callback's success path so the source's freshness
        reflects real ingestion progress (the scheduler and admin UI read this).
        Uses an atomic UPDATE — no read-modify-write — and never blocks the ack.
        """
        async with self._session_factory() as session:
            await session.execute(
                update(Source).where(Source.id == source_id).values(last_sync_at=datetime.now(UTC))
            )
            await session.commit()

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


# ---------------------------------------------------------------------------
# Module-level helpers (pure / small — keep under 30 lines each)
# ---------------------------------------------------------------------------


def _to_operator_event(event: DocumentChangeEvent, workspace_id: uuid.UUID) -> OperatorEvent:
    """Project a ``DocumentChangeEvent`` onto the bridge's ``OperatorEvent``.

    ``workspace_id`` is the **server-resolved** workspace (from
    ``Source.tenant_id``), NOT any field on the wire payload — the ACL
    invariant.  ``metadata``/``topology_edges`` are carried through verbatim;
    callers must only invoke this for operator events that actually carry a
    ``metadata`` payload (the worker gates on that).
    """
    edges = [
        OperatorEventEdge(kind=e.kind, target_external_id=e.target_external_id)
        for e in (event.topology_edges or [])
    ]
    return OperatorEvent(
        source_id=event.source_id,
        source_type=event.source_type,
        external_id=event.external_id,
        uri=event.uri,
        action=event.action,
        workspace_id=workspace_id,
        metadata=dict(event.metadata or {}),
        topology_edges=edges,
    )


def _is_sync_marker(event: DocumentChangeEvent) -> bool:
    """Return True when *event* is a discovery sync marker.

    A sync marker signals "re-sync the whole source" rather than a single
    document change.  The sentinel values are set by ``trigger_sync`` in
    ``rest/sources.py``.
    """
    return event.external_id == "*" or event.uri.startswith("sync://")


async def _collect_refs(
    connector: Connector,
    config: Any,
    secrets: dict[str, str],
) -> list[DocumentRef]:
    """Drain ``connector.discover()`` into a list.

    Raises :class:`NotImplementedError` when the connector does not support
    discovery (propagated from ``Connector.discover`` base implementation).
    """
    refs: list[DocumentRef] = []
    async for ref in connector.discover(config, secrets):
        refs.append(ref)
    return refs


async def _fan_out_refs(
    refs: list[DocumentRef],
    event: DocumentChangeEvent,
    pipeline: IngestionPipeline,
    config: Any,
    secrets: dict[str, str],
    workspace_id: uuid.UUID,
    run_id: uuid.UUID | None,
    concurrency: int,
) -> list[ProcessResult]:
    """Run ``pipeline.run_ref`` for every ref with bounded concurrency."""
    semaphore = asyncio.Semaphore(concurrency)

    async def _run_one(ref: DocumentRef) -> ProcessResult:
        async with semaphore:
            return await pipeline.run_ref(
                event=event,
                ref=ref,
                config=config,
                secrets=secrets,
                workspace_id=workspace_id,
                ingestion_run_id=run_id,
            )

    return list(await asyncio.gather(*(_run_one(r) for r in refs)))


__all__ = ["IngestionWorker"]
