"""Outbox event consumer for Neo4j and Qdrant synchronization."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, cast

import structlog
from omniscience_core.audit.fingerprint import entity_content_hash
from omniscience_core.db.models import Edge, Entity, Source
from omniscience_core.queue import NatsConnection, QueueConsumer, QueueProducer
from omniscience_core.queue.messages import (
    DLQMessage,
    EdgeUpsertEvent,
    EntityMergeEvent,
    EntityUpsertEvent,
)
from omniscience_core.storage.graph import EdgeUpsert, EntityUpsert, GraphStore
from omniscience_core.storage.vector import ChunkPayload, VectorStore
from omniscience_embeddings.base import EmbeddingProvider
from prometheus_client import Gauge
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

log = structlog.get_logger(__name__)

PARKED_ENTITIES_COUNT = Gauge("omniscience_outbox_parked_entities_total", "Total parked entities")
PARKED_ENTITIES_OLDEST = Gauge(
    "omniscience_outbox_parked_entities_oldest_age_seconds", "Oldest parked entity age"
)
PARKED_EDGES_COUNT = Gauge("omniscience_outbox_parked_edges_total", "Total parked edges")
PARKED_EDGES_OLDEST = Gauge(
    "omniscience_outbox_parked_edges_oldest_age_seconds", "Oldest parked edge age"
)

MAX_PARKED_ITEMS = 10000
MAX_RETRIES_BEFORE_PARK = 3


class OutboxConsumerWorker:
    """Consumes outbox events and updates Neo4j and Qdrant to prevent split-brain."""

    def __init__(
        self,
        nats_conn: NatsConnection,
        graph_store: GraphStore,
        vector_store: VectorStore,
        embedding_provider: EmbeddingProvider,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._nats_conn = nats_conn
        self._graph_store = graph_store
        self._vector_store = vector_store
        self._embedding_provider = embedding_provider
        self._session_factory = session_factory
        self._running = False
        self._entity_consumer: QueueConsumer[EntityUpsertEvent] | None = None
        self._edge_consumer: QueueConsumer[EdgeUpsertEvent] | None = None
        self._merge_consumer: QueueConsumer[EntityMergeEvent] | None = None
        self._entity_task: asyncio.Task[None] | None = None
        self._edge_task: asyncio.Task[None] | None = None
        self._merge_task: asyncio.Task[None] | None = None
        self._unpark_task: asyncio.Task[None] | None = None
        self._dlq_producer: QueueProducer | None = None

        # Park-the-entity tracking
        self._parked_entities: dict[uuid.UUID, float] = {}
        self._parked_edges: dict[uuid.UUID, float] = {}
        self._edge_deps: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]] = {}
        self._unpark_timeout_seconds = 300.0  # 5 minutes
        self._entity_attempts: dict[uuid.UUID, int] = {}
        self._edge_attempts: dict[uuid.UUID, int] = {}

    def _update_metrics(self) -> None:
        """Update Prometheus metrics for parked entities and edges."""
        now = time.time()
        PARKED_ENTITIES_COUNT.set(len(self._parked_entities))
        if self._parked_entities:
            oldest = min(self._parked_entities.values())
            PARKED_ENTITIES_OLDEST.set(now - oldest)
        else:
            PARKED_ENTITIES_OLDEST.set(0)

        PARKED_EDGES_COUNT.set(len(self._parked_edges))
        if self._parked_edges:
            oldest = min(self._parked_edges.values())
            PARKED_EDGES_OLDEST.set(now - oldest)
        else:
            PARKED_EDGES_OLDEST.set(0)

    async def start(self) -> None:
        """Start the outbox consumer tasks."""
        self._running = True
        log.info("outbox_consumer_worker_starting")

        self._dlq_producer = QueueProducer(self._nats_conn.jetstream)

        self._entity_consumer = QueueConsumer(
            js=self._nats_conn.jetstream,
            stream="OUTBOX",
            subject="outbox.entity.upsert",
            durable="omniscience-outbox-entity-consumer",
            payload_type=EntityUpsertEvent,
            dlq_subject="ingest.dlq.outbox_entity",
        )

        self._edge_consumer = QueueConsumer(
            js=self._nats_conn.jetstream,
            stream="OUTBOX",
            subject="outbox.edge.upsert",
            durable="omniscience-outbox-edge-consumer",
            payload_type=EdgeUpsertEvent,
            dlq_subject="ingest.dlq.outbox_edge",
        )

        self._merge_consumer = QueueConsumer(
            js=self._nats_conn.jetstream,
            stream="OUTBOX",
            subject="outbox.entity.*",
            durable="omniscience-outbox-merge-consumer",
            payload_type=EntityMergeEvent,
            dlq_subject="ingest.dlq.outbox_merge",
        )

        self._entity_task = asyncio.create_task(self._consume_entities())
        self._edge_task = asyncio.create_task(self._consume_edges())
        self._merge_task = asyncio.create_task(self._consume_merges())
        if self._session_factory:
            self._unpark_task = asyncio.create_task(self._unpark_loop())
        log.info("outbox_consumer_worker_started")

    async def stop(self) -> None:
        """Stop the outbox consumer tasks."""
        self._running = False
        if self._entity_consumer:
            self._entity_consumer.stop()
        if self._edge_consumer:
            self._edge_consumer.stop()
        if self._merge_consumer:
            self._merge_consumer.stop()

        for task in (self._entity_task, self._edge_task, self._merge_task, self._unpark_task):
            if task:
                task.cancel()

        log.info("outbox_consumer_worker_stopped")

    async def _route_to_dlq(self, msg: Any, dlq_subject: str, error_msg: str) -> None:
        """Route a message to the DLQ and term it."""
        if not self._dlq_producer:
            return

        try:
            dlq_msg = DLQMessage(
                original_subject=msg.subject,
                original_payload=(
                    msg.payload.model_dump_json()
                    if hasattr(msg.payload, "model_dump_json")
                    else str(msg.payload)
                ),
                error=error_msg,
                attempt_count=1,
            )
            await self._dlq_producer.publish(dlq_subject, dlq_msg)
            await msg.term()
        except Exception as exc:
            log.error("outbox_dlq_publish_failed", error=str(exc))
            await msg.nak()

    async def _unpark_loop(self) -> None:
        """Periodically check for long-parked entities and backfill from Postgres."""
        if not self._session_factory:
            return

        producer = QueueProducer(self._nats_conn.jetstream)

        while self._running:
            try:
                now = time.monotonic()
                stale_entities = [
                    eid
                    for eid, parked_at in list(self._parked_entities.items())
                    if now - parked_at > self._unpark_timeout_seconds
                ]
                stale_edges = [
                    eid
                    for eid, parked_at in list(self._parked_edges.items())
                    if now - parked_at > self._unpark_timeout_seconds
                ]

                if stale_entities or stale_edges:
                    async with self._session_factory() as session:
                        if stale_entities:
                            stmt = (
                                select(Entity, Source.tenant_id)
                                .join(Source, Entity.source_id == Source.id)
                                .where(Entity.id.in_(stale_entities))
                            )
                            res = await session.execute(stmt)
                            for entity, tenant_id in res:
                                event = EntityUpsertEvent(
                                    id=str(entity.id),
                                    source_id=str(entity.source_id),
                                    workspace_id=str(tenant_id) if tenant_id else None,
                                    entity_type=entity.entity_type,
                                    name=entity.name,
                                    display_name=entity.display_name,
                                    metadata=entity.entity_metadata,
                                    version=entity.version,
                                    is_backfill=True,
                                    content_hash=entity.content_hash,
                                )
                                await producer.publish("outbox.entity.upsert", event)
                                log.info("unpark_backfill_entity", entity_id=str(entity.id))

                        if stale_edges:
                            # Re-validate BOTH edge endpoints against the same
                            # tenant before republishing: target_entity_id is a
                            # bare FK with no DB constraint tying it to the
                            # source entity's workspace, so a corrupted/
                            # cross-tenant edge must not be backfilled into the
                            # wrong tenant's outbox stream on unpark.
                            TargetEntity = aliased(Entity)
                            TargetSource = aliased(Source)
                            stmt_e = (
                                select(Edge, Source.tenant_id)
                                .join(Entity, Edge.source_entity_id == Entity.id)
                                .join(Source, Entity.source_id == Source.id)
                                .join(TargetEntity, Edge.target_entity_id == TargetEntity.id)
                                .join(TargetSource, TargetEntity.source_id == TargetSource.id)
                                .where(Edge.id.in_(stale_edges))
                                .where(TargetSource.tenant_id == Source.tenant_id)
                            )
                            res_e = await session.execute(stmt_e)
                            for edge, tenant_id in res_e:
                                edge_event = EdgeUpsertEvent(
                                    id=str(edge.id),
                                    source_entity_id=str(edge.source_entity_id),
                                    target_entity_id=str(edge.target_entity_id),
                                    workspace_id=str(tenant_id) if tenant_id else None,
                                    edge_type=edge.edge_type,
                                    metadata=edge.edge_metadata,
                                    version=edge.version,
                                    is_backfill=True,
                                )
                                await producer.publish("outbox.edge.upsert", edge_event)
                                log.info("unpark_backfill_edge", edge_id=str(edge.id))

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("unpark_loop_failed", error=str(exc))

            try:
                await asyncio.sleep(60.0)
            except asyncio.CancelledError:
                break

    async def _consume_entities(self) -> None:
        """Loop to consume entity upsert events."""
        if not self._entity_consumer:
            return

        try:
            async for msg in self._entity_consumer:
                try:
                    event: EntityUpsertEvent = msg.payload
                    entity_id = uuid.UUID(event.id)

                    if event.is_backfill:
                        self._parked_entities.pop(entity_id, None)
                        self._entity_attempts.pop(entity_id, None)
                        self._update_metrics()

                    if entity_id in self._parked_entities:
                        log.warning("outbox_entity_parked_skip", entity_id=str(entity_id))
                        await self._route_to_dlq(
                            msg,
                            dlq_subject="ingest.dlq.outbox_entity",
                            error_msg=f"Entity {entity_id} is parked due to previous failure",
                        )
                        continue

                    workspace_id = (
                        uuid.UUID(event.workspace_id)
                        if event.workspace_id
                        else uuid.UUID("00000000-0000-0000-0000-000000000000")
                    )

                    # 1. Update Neo4j
                    # v10-AP3: if this is a reconciler backfill, bypass the
                    # coarse source-checkpoint skip so the per-entity CAS can
                    # still heal a node that is individually stale even when
                    # the source-level checkpoint is already at or beyond the
                    # incoming version.  The per-entity CAS monotonic guard
                    # (incoming_version > node.version) remains active, so
                    # a stale backfill cannot regress a node correctly written
                    # at a higher version.
                    entity_upsert = EntityUpsert(
                        id=entity_id,
                        source_id=uuid.UUID(event.source_id),
                        entity_type=event.entity_type,
                        name=event.name,
                        display_name=event.display_name,
                        chunk_id=None,
                        metadata=event.metadata,
                        version=event.version,
                        bypass_source_checkpoint=event.is_backfill,
                    )
                    await self._graph_store.upsert_entity(
                        entity=entity_upsert,
                        workspace_id=workspace_id,
                    )

                    # 2. Update Qdrant
                    text = (
                        f"Entity: {event.entity_type} {event.name} ({event.display_name}). "
                        f"Metadata: {event.metadata}"
                    )
                    embeddings = await self._embedding_provider.embed([text])
                    embedding = embeddings[0]
                    chunk = cast(
                        "ChunkPayload",
                        {
                            "ord": 0,
                            "text": text,
                            "embedding": embedding,
                            "symbol": event.name,
                            "metadata": {
                                "workspace_id": str(workspace_id),
                                "entity_type": event.entity_type,
                                "name": event.name,
                                "display_name": event.display_name,
                                **event.metadata,
                            },
                            "embedding_model": self._embedding_provider.model_name,
                            "embedding_provider": self._embedding_provider.provider_name,
                            "parser_version": "outbox_v1",
                            "chunker_strategy": "entity_outbox",
                        },
                    )
                    # AP2: use the canonical BLAKE2b-256 entity hash forwarded
                    # from Postgres Entity.content_hash so all three stores
                    # store the SAME value and the reconcile worker can compare
                    # them directly.  Fall back to entity_content_hash() if the
                    # event pre-dates AP2 (content_hash field is None).
                    if event.content_hash is not None:
                        qdrant_content_hash = event.content_hash
                    else:
                        qdrant_content_hash = entity_content_hash(
                            entity_type=event.entity_type,
                            name=event.name,
                            display_name=event.display_name,
                            metadata=event.metadata,
                        )

                    await self._vector_store.upsert_chunks(
                        source_id=uuid.UUID(event.source_id),
                        external_id=f"entity:{event.id}",
                        uri=f"entity://{event.entity_type}/{event.name}",
                        title=event.display_name,
                        content_hash=qdrant_content_hash,
                        metadata={"workspace_id": str(workspace_id)},
                        chunks=[chunk],
                        version=event.version,
                    )

                    self._entity_attempts.pop(entity_id, None)
                    await msg.ack()

                    # Auto-unpark dependent edges
                    if self._session_factory:
                        await self._auto_unpark_edges(entity_id)
                except Exception as exc:
                    log.error("outbox_consume_entity_failed", error=str(exc))
                    if "entity_id" in dir():
                        await self._handle_entity_failure(entity_id, msg, exc)
                    else:
                        await msg.nak()
        except asyncio.CancelledError:
            pass

    async def _auto_unpark_edges(self, resolved_entity_id: uuid.UUID) -> None:
        """Republish edges that were waiting for ``resolved_entity_id`` to land."""
        if not self._session_factory:
            return
        edges_to_unpark = [
            e_id
            for e_id, (src_id, tgt_id) in list(self._edge_deps.items())
            if (src_id == resolved_entity_id or tgt_id == resolved_entity_id)
            and ((tgt_id if src_id == resolved_entity_id else src_id) not in self._parked_entities)
        ]

        if not edges_to_unpark:
            return

        async with self._session_factory() as session:
            stmt = (
                select(Edge, Source.tenant_id)
                .join(Entity, Edge.source_entity_id == Entity.id)
                .join(Source, Entity.source_id == Source.id)
                .where(Edge.id.in_(edges_to_unpark))
            )
            res = await session.execute(stmt)
            producer = QueueProducer(self._nats_conn.jetstream)
            for edge, tenant_id in res:
                backfill_event = EdgeUpsertEvent(
                    id=str(edge.id),
                    source_entity_id=str(edge.source_entity_id),
                    target_entity_id=str(edge.target_entity_id),
                    workspace_id=str(tenant_id) if tenant_id else None,
                    edge_type=edge.edge_type,
                    metadata=edge.edge_metadata,
                    version=edge.version,
                    is_backfill=True,
                )
                await producer.publish("outbox.edge.upsert", backfill_event)
                log.info(
                    "auto_unpark_edge",
                    edge_id=str(edge.id),
                    trigger_entity_id=str(resolved_entity_id),
                )
                self._parked_edges.pop(edge.id, None)
                self._edge_deps.pop(edge.id, None)
        self._update_metrics()

    async def _handle_entity_failure(self, entity_id: uuid.UUID, msg: Any, exc: Exception) -> None:
        """Retry or park an entity after a processing failure."""
        attempt = self._entity_attempts.get(entity_id, 0) + 1
        self._entity_attempts[entity_id] = attempt
        if attempt <= MAX_RETRIES_BEFORE_PARK:
            delay = float(2**attempt)
            log.info(
                "outbox_entity_retry",
                entity_id=str(entity_id),
                attempt=attempt,
                delay=delay,
            )
            await msg.nak(delay=delay)
        else:
            if entity_id not in self._parked_entities:
                self._parked_entities[entity_id] = time.monotonic()
            if len(self._parked_entities) > MAX_PARKED_ITEMS:
                oldest_key = min(
                    self._parked_entities,
                    key=lambda k: self._parked_entities[k],
                )
                del self._parked_entities[oldest_key]
            self._update_metrics()
            await self._route_to_dlq(
                msg,
                dlq_subject="ingest.dlq.outbox_entity",
                error_msg=f"Processing failed: {exc}",
            )

    async def _consume_edges(self) -> None:
        """Loop to consume edge upsert events."""
        if not self._edge_consumer:
            return

        try:
            async for msg in self._edge_consumer:
                try:
                    event: EdgeUpsertEvent = msg.payload
                    edge_id = uuid.UUID(event.id)
                    source_entity_id = uuid.UUID(event.source_entity_id)
                    target_entity_id = uuid.UUID(event.target_entity_id)

                    if event.is_backfill:
                        self._parked_edges.pop(edge_id, None)
                        self._edge_deps.pop(edge_id, None)
                        self._edge_attempts.pop(edge_id, None)
                        self._update_metrics()

                    if edge_id in self._parked_edges:
                        log.warning("outbox_edge_parked_skip", edge_id=str(edge_id))
                        await self._route_to_dlq(
                            msg,
                            dlq_subject="ingest.dlq.outbox_edge",
                            error_msg=f"Edge {edge_id} is parked due to previous failure",
                        )
                        continue

                    if (
                        source_entity_id in self._parked_entities
                        or target_entity_id in self._parked_entities
                    ):
                        log.warning("outbox_edge_skip_parked_entity", edge_id=str(edge_id))
                        if edge_id not in self._parked_edges:
                            self._parked_edges[edge_id] = time.monotonic()
                            self._edge_deps[edge_id] = (source_entity_id, target_entity_id)
                        if len(self._parked_edges) > MAX_PARKED_ITEMS:
                            oldest_key = min(
                                self._parked_edges,
                                key=lambda k: self._parked_edges[k],
                            )
                            del self._parked_edges[oldest_key]
                            self._edge_deps.pop(oldest_key, None)
                        self._update_metrics()
                        await self._route_to_dlq(
                            msg,
                            dlq_subject="ingest.dlq.outbox_edge",
                            error_msg=f"Edge {edge_id} involves parked entities",
                        )
                        continue

                    workspace_id = (
                        uuid.UUID(event.workspace_id)
                        if event.workspace_id
                        else uuid.UUID("00000000-0000-0000-0000-000000000000")
                    )

                    # 1. Update Neo4j
                    # v10-AP3: edge backfill also bypasses source-checkpoint skip.
                    edge_upsert = EdgeUpsert(
                        source_entity_id=source_entity_id,
                        target_entity_id=target_entity_id,
                        edge_type=event.edge_type,
                        metadata=event.metadata,
                        version=event.version,
                        bypass_source_checkpoint=event.is_backfill,
                    )
                    await self._graph_store.upsert_edge(
                        edge=edge_upsert,
                        workspace_id=workspace_id,
                    )

                    self._edge_attempts.pop(edge_id, None)
                    await msg.ack()
                except Exception as exc:
                    log.error("outbox_consume_edge_failed", error=str(exc))
                    if "edge_id" in dir():
                        await self._handle_edge_failure(
                            edge_id, source_entity_id, target_entity_id, msg, exc
                        )
                    else:
                        await msg.nak()
        except asyncio.CancelledError:
            pass

    async def _handle_edge_failure(
        self,
        edge_id: uuid.UUID,
        source_entity_id: uuid.UUID,
        target_entity_id: uuid.UUID,
        msg: Any,
        exc: Exception,
    ) -> None:
        """Retry or park an edge after a processing failure."""
        attempt = self._edge_attempts.get(edge_id, 0) + 1
        self._edge_attempts[edge_id] = attempt
        if attempt <= MAX_RETRIES_BEFORE_PARK:
            delay = float(2**attempt)
            log.info(
                "outbox_edge_retry",
                edge_id=str(edge_id),
                attempt=attempt,
                delay=delay,
            )
            await msg.nak(delay=delay)
        else:
            if edge_id not in self._parked_edges:
                self._parked_edges[edge_id] = time.monotonic()
                self._edge_deps[edge_id] = (source_entity_id, target_entity_id)
            if len(self._parked_edges) > MAX_PARKED_ITEMS:
                oldest_key = min(
                    self._parked_edges,
                    key=lambda k: self._parked_edges[k],
                )
                del self._parked_edges[oldest_key]
                self._edge_deps.pop(oldest_key, None)
            self._update_metrics()
            await self._route_to_dlq(
                msg,
                dlq_subject="ingest.dlq.outbox_edge",
                error_msg=f"Processing failed: {exc}",
            )

    async def _consume_merges(self) -> None:
        """Loop to consume entity.merge and entity.unmerge events (AP1)."""
        if not self._merge_consumer:
            return

        try:
            async for msg in self._merge_consumer:
                try:
                    event: EntityMergeEvent = msg.payload
                    workspace_id = uuid.UUID(event.workspace_id)

                    if msg.subject.endswith("entity.merge"):
                        if event.source_id is None or event.target_id is None:
                            log.warning(
                                "outbox_merge_missing_ids",
                                workspace_id=str(workspace_id),
                            )
                            await msg.term()
                            continue
                        success = await self._graph_store.merge_nodes(
                            workspace_id=workspace_id,
                            source_id=uuid.UUID(event.source_id),
                            target_id=uuid.UUID(event.target_id),
                        )
                        if not success:
                            log.warning(
                                "outbox_merge_nodes_failed",
                                source_id=event.source_id,
                                target_id=event.target_id,
                            )
                    elif msg.subject.endswith("entity.unmerge"):
                        if event.merged_node_id is None:
                            log.warning(
                                "outbox_unmerge_missing_id",
                                workspace_id=str(workspace_id),
                            )
                            await msg.term()
                            continue
                        success = await self._graph_store.unmerge_node(
                            workspace_id=workspace_id,
                            merged_node_id=uuid.UUID(event.merged_node_id),
                        )
                        if not success:
                            log.warning(
                                "outbox_unmerge_node_failed",
                                merged_node_id=event.merged_node_id,
                            )

                    await msg.ack()
                except Exception as exc:
                    log.error("outbox_consume_merge_failed", error=str(exc))
                    await self._route_to_dlq(
                        msg,
                        dlq_subject="ingest.dlq.outbox_merge",
                        error_msg=f"Merge/unmerge processing failed: {exc}",
                    )
        except asyncio.CancelledError:
            pass
