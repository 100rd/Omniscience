"""Outbox event consumer for Neo4j and Qdrant synchronization."""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import structlog
from omniscience_core.queue import NatsConnection, QueueConsumer
from omniscience_core.queue.messages import EdgeUpsertEvent, EntityUpsertEvent
from omniscience_core.storage.graph import EdgeUpsert, EntityUpsert, GraphStore
from omniscience_core.storage.vector import VectorStore
from omniscience_embeddings.base import EmbeddingProvider

log = structlog.get_logger(__name__)


class OutboxConsumerWorker:
    """Consumes outbox events and updates Neo4j and Qdrant to prevent split-brain."""

    def __init__(
        self,
        nats_conn: NatsConnection,
        graph_store: GraphStore,
        vector_store: VectorStore,
        embedding_provider: EmbeddingProvider,
    ) -> None:
        self._nats_conn = nats_conn
        self._graph_store = graph_store
        self._vector_store = vector_store
        self._embedding_provider = embedding_provider
        self._running = False
        self._entity_consumer: QueueConsumer[EntityUpsertEvent] | None = None
        self._edge_consumer: QueueConsumer[EdgeUpsertEvent] | None = None
        self._entity_task: asyncio.Task[None] | None = None
        self._edge_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the outbox consumer tasks."""
        self._running = True
        log.info("outbox_consumer_worker_starting")

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

        self._entity_task = asyncio.create_task(self._consume_entities())
        self._edge_task = asyncio.create_task(self._consume_edges())
        log.info("outbox_consumer_worker_started")

    async def stop(self) -> None:
        """Stop the outbox consumer tasks."""
        self._running = False
        if self._entity_consumer:
            self._entity_consumer.stop()
        if self._edge_consumer:
            self._edge_consumer.stop()

        if self._entity_task:
            self._entity_task.cancel()
        if self._edge_task:
            self._edge_task.cancel()

        log.info("outbox_consumer_worker_stopped")

    async def _consume_entities(self) -> None:
        """Loop to consume entity upsert events."""
        if not self._entity_consumer:
            return

        try:
            async for msg in self._entity_consumer:
                try:
                    event: EntityUpsertEvent = msg.payload
                    workspace_id = (
                        uuid.UUID(event.workspace_id)
                        if event.workspace_id
                        else uuid.UUID("00000000-0000-0000-0000-000000000000")
                    )

                    # 1. Update Neo4j
                    entity_upsert = EntityUpsert(
                        id=uuid.UUID(event.id),
                        source_id=uuid.UUID(event.source_id),
                        entity_type=event.entity_type,
                        name=event.name,
                        display_name=event.display_name,
                        chunk_id=None,
                        metadata=event.metadata,
                    )
                    await self._graph_store.upsert_entity(
                        entity=entity_upsert,
                        workspace_id=workspace_id,
                    )

                    # 2. Update Qdrant
                    text = f"Entity: {event.entity_type} {event.name} ({event.display_name}). Metadata: {event.metadata}"
                    embedding = await self._embedding_provider.embed_query(text)
                    chunk = {
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
                    }
                    content_hash = hashlib.sha256(text.encode()).hexdigest()

                    await self._vector_store.upsert_chunks(
                        source_id=uuid.UUID(event.source_id),
                        external_id=f"entity:{event.id}",
                        uri=f"entity://{event.entity_type}/{event.name}",
                        title=event.display_name,
                        content_hash=content_hash,
                        metadata={"workspace_id": str(workspace_id)},
                        chunks=[chunk],
                    )

                    await msg.ack()
                except Exception as exc:
                    log.error("outbox_consume_entity_failed", error=str(exc))
                    await msg.nak()
        except asyncio.CancelledError:
            pass

    async def _consume_edges(self) -> None:
        """Loop to consume edge upsert events."""
        if not self._edge_consumer:
            return

        try:
            async for msg in self._edge_consumer:
                try:
                    event: EdgeUpsertEvent = msg.payload
                    workspace_id = (
                        uuid.UUID(event.workspace_id)
                        if event.workspace_id
                        else uuid.UUID("00000000-0000-0000-0000-000000000000")
                    )

                    # 1. Update Neo4j
                    edge_upsert = EdgeUpsert(
                        source_entity_id=uuid.UUID(event.source_entity_id),
                        target_entity_id=uuid.UUID(event.target_entity_id),
                        edge_type=event.edge_type,
                        metadata=event.metadata,
                    )
                    await self._graph_store.upsert_edge(
                        edge=edge_upsert,
                        workspace_id=workspace_id,
                    )

                    await msg.ack()
                except Exception as exc:
                    log.error("outbox_consume_edge_failed", error=str(exc))
                    await msg.nak()
        except asyncio.CancelledError:
            pass
