"""Tests for the Outbox flow.

Covers: OtlpIngester -> OutboxEvent -> OutboxWorker -> JetStream -> OutboxConsumerWorker.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_connectors.otel import ParsedSpan, ParsedTrace, ParsedTraces
from omniscience_core.db.models import Edge, Entity, OutboxEvent, Source, SourceType
from omniscience_core.queue.messages import EdgeUpsertEvent, EntityUpsertEvent
from omniscience_core.storage.graph import GraphStore
from omniscience_core.storage.vector import VectorStore
from omniscience_embeddings.base import EmbeddingProvider
from omniscience_server.outbox_consumer import OutboxConsumerWorker
from omniscience_server.outbox_worker import OutboxWorker
from omniscience_server.rest.otlp_ingester import (
    CROSS_REF_EDGE_TYPE,
    OtlpIngester,
)
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_otlp_ingester_writes_to_outbox() -> None:
    """OtlpIngester.ingest creates entities/edges and writes OutboxEvent rows in the same txn."""
    ws_id = uuid.uuid4()
    src = MagicMock(spec=Source)
    src.id = uuid.uuid4()
    src.name = f"otel-receiver:{ws_id}"
    src.type = SourceType.otel
    src.tenant_id = ws_id

    added: list[Any] = []
    call_count = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.scalars.return_value.first.return_value = src
        else:
            result.scalars.return_value.first.return_value = None
        return result

    session = AsyncMock(spec=AsyncSession)
    session.execute = _execute
    session.add = MagicMock(side_effect=added.append)

    async def _flush() -> None:
        for obj in added:
            if isinstance(obj, Entity) and not hasattr(obj, "_flushed"):
                obj.id = uuid.uuid4()
                obj._flushed = True
            if isinstance(obj, Edge) and not hasattr(obj, "_flushed"):
                obj.id = uuid.uuid4()
                obj._flushed = True

    session.flush = AsyncMock(side_effect=_flush)
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock()
    factory.return_value = session
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    ingester = OtlpIngester(factory)

    # 1 span trace
    span = ParsedSpan(
        trace_id_hex="aa" * 16,
        span_id_hex="bb" * 8,
        name="GET /test",
        service_name="checkout",
        service_namespace="default",
        pod_name="checkout-pod",
        start_unix_nano=1700000000000000000,
        end_unix_nano=1700000000050000000,
    )
    trace = ParsedTrace(
        trace_id_hex="aa" * 16,
        spans=[span],
        service_names={"checkout"},
        pod_names={"checkout-pod"},
    )
    parsed = ParsedTraces(traces={"aa" * 16: trace})

    result = await ingester.ingest(workspace_id=ws_id, parsed=parsed)
    assert result == 1

    # Verify entities and edges were added
    entities = [o for o in added if isinstance(o, Entity)]
    edges = [o for o in added if isinstance(o, Edge)]
    outbox_events = [o for o in added if isinstance(o, OutboxEvent)]

    assert len(entities) == 3
    assert len(edges) == 2
    # Expecting 3 entity upsert outbox events + 2 edge upsert outbox events = 5 outbox events
    assert len(outbox_events) == 5

    entity_event_types = [oe.event_type for oe in outbox_events]
    assert entity_event_types.count("entity.upsert") == 3
    assert entity_event_types.count("edge.upsert") == 2


@pytest.mark.asyncio
async def test_outbox_worker_publishes_to_nats() -> None:
    """OutboxWorker polls unprocessed OutboxEvent rows and publishes to NATS."""
    event1 = OutboxEvent(
        id=uuid.uuid4(),
        event_type="entity.upsert",
        payload={
            "workspace_id": str(uuid.uuid4()),
            "id": str(uuid.uuid4()),
            "source_id": str(uuid.uuid4()),
            "entity_type": "otel_service",
            "name": "service://checkout",
            "display_name": "checkout",
            "metadata": {},
        },
        processed=False,
    )

    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalars.return_value.all.return_value = [event1]
    session.execute = AsyncMock(return_value=result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock()
    factory.return_value = session
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    nats_conn = MagicMock()
    nats_conn.is_connected = True
    nats_conn.jetstream = AsyncMock()

    worker = OutboxWorker(factory, nats_conn, interval_seconds=0.1)
    worker._producer = AsyncMock()

    await worker._tick()

    # Verify worker published to NATS and marked event as processed
    worker._producer.publish.assert_called_once()
    subject, payload = worker._producer.publish.call_args[0]
    assert subject == "outbox.entity.upsert"
    assert isinstance(payload, EntityUpsertEvent)
    assert payload.name == "service://checkout"
    assert event1.processed is True
    assert event1.processed_at is not None


@pytest.mark.asyncio
async def test_outbox_consumer_worker_updates_neo4j_and_qdrant() -> None:
    """OutboxConsumerWorker consumes entity/edge events and updates stores."""
    nats_conn = MagicMock()
    nats_conn.jetstream = AsyncMock()

    graph_store = AsyncMock(spec=GraphStore)
    vector_store = AsyncMock(spec=VectorStore)
    embedding_provider = AsyncMock(spec=EmbeddingProvider)
    embedding_provider.model_name = "test-model"
    embedding_provider.provider_name = "test-provider"
    embedding_provider.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3]])

    consumer_worker = OutboxConsumerWorker(
        nats_conn=nats_conn,
        graph_store=graph_store,
        vector_store=vector_store,
        embedding_provider=embedding_provider,
    )

    # Mock raw messages
    entity_msg = AsyncMock()
    ws_id = str(uuid.uuid4())
    ent_id = str(uuid.uuid4())
    src_id = str(uuid.uuid4())
    entity_msg.payload = EntityUpsertEvent(
        workspace_id=ws_id,
        id=ent_id,
        source_id=src_id,
        entity_type="otel_service",
        name="service://checkout",
        display_name="checkout",
        metadata={"service_name": "checkout"},
    )

    edge_msg = AsyncMock()
    src_ent_id = str(uuid.uuid4())
    tgt_ent_id = str(uuid.uuid4())
    edge_msg.payload = EdgeUpsertEvent(
        workspace_id=ws_id,
        id=str(uuid.uuid4()),
        source_entity_id=src_ent_id,
        target_entity_id=tgt_ent_id,
        edge_type=CROSS_REF_EDGE_TYPE,
        metadata={"strategy": "otel_trace"},
    )

    # Set up consumers
    consumer_worker._entity_consumer = AsyncMock()
    consumer_worker._edge_consumer = AsyncMock()

    # Simulate single loops
    async def fake_entity_iter():
        yield entity_msg

    async def fake_edge_iter():
        yield edge_msg

    consumer_worker._entity_consumer.__aiter__ = lambda self: fake_entity_iter()
    consumer_worker._edge_consumer.__aiter__ = lambda self: fake_edge_iter()

    # Execute consumer functions once
    await consumer_worker._consume_entities()
    await consumer_worker._consume_edges()

    # Verify GraphStore entity upsert
    graph_store.upsert_entity.assert_called_once()
    kwargs = graph_store.upsert_entity.call_args[1]
    assert kwargs["workspace_id"] == uuid.UUID(ws_id)
    assert kwargs["entity"].id == uuid.UUID(ent_id)
    assert kwargs["entity"].name == "service://checkout"

    # Verify VectorStore chunk upsert (split-brain prevention)
    vector_store.upsert_chunks.assert_called_once()
    v_kwargs = vector_store.upsert_chunks.call_args[1]
    assert v_kwargs["source_id"] == uuid.UUID(src_id)
    assert v_kwargs["external_id"] == f"entity:{ent_id}"
    assert v_kwargs["uri"] == "entity://otel_service/service://checkout"
    assert v_kwargs["metadata"] == {"workspace_id": ws_id}
    assert len(v_kwargs["chunks"]) == 1
    assert v_kwargs["chunks"][0]["embedding"] == [0.1, 0.2, 0.3]

    # Verify GraphStore edge upsert
    graph_store.upsert_edge.assert_called_once()
    e_kwargs = graph_store.upsert_edge.call_args[1]
    assert e_kwargs["workspace_id"] == uuid.UUID(ws_id)
    assert e_kwargs["edge"].source_entity_id == uuid.UUID(src_ent_id)
    assert e_kwargs["edge"].target_entity_id == uuid.UUID(tgt_ent_id)

    # Verify acks
    entity_msg.ack.assert_called_once()
    edge_msg.ack.assert_called_once()


@pytest.mark.asyncio
async def test_outbox_consumer_worker_park_the_entity() -> None:
    """OutboxConsumerWorker routes to DLQ and parks entities on failure."""
    nats_conn = MagicMock()
    nats_conn.jetstream = AsyncMock()

    graph_store = AsyncMock(spec=GraphStore)
    vector_store = AsyncMock(spec=VectorStore)
    embedding_provider = AsyncMock(spec=EmbeddingProvider)

    # Make graph_store raise an exception on first call, succeed on second
    graph_store.upsert_entity.side_effect = Exception("DB connection lost")

    consumer_worker = OutboxConsumerWorker(
        nats_conn=nats_conn,
        graph_store=graph_store,
        vector_store=vector_store,
        embedding_provider=embedding_provider,
    )
    consumer_worker._dlq_producer = AsyncMock()

    # Mock raw messages
    entity_msg1 = AsyncMock()
    entity_msg2 = AsyncMock()

    ws_id = str(uuid.uuid4())
    ent_id = str(uuid.uuid4())
    src_id = str(uuid.uuid4())

    payload1 = EntityUpsertEvent(
        workspace_id=ws_id,
        id=ent_id,
        source_id=src_id,
        entity_type="otel_service",
        name="service://checkout",
        display_name="checkout",
        metadata={},
    )
    payload2 = EntityUpsertEvent(
        workspace_id=ws_id,
        id=ent_id,
        source_id=src_id,
        entity_type="otel_service",
        name="service://checkout-updated",
        display_name="checkout updated",
        metadata={},
    )

    entity_msg1.payload = payload1
    entity_msg1.subject = "outbox.entity.upsert"
    entity_msg2.payload = payload2
    entity_msg2.subject = "outbox.entity.upsert"

    consumer_worker._entity_consumer = AsyncMock()

    # Simulate iterating over messages (4 times for msg1 to exceed 3 retries, then 1 time for msg2)
    async def fake_entity_iter():
        for _ in range(4):
            yield entity_msg1
        yield entity_msg2

    consumer_worker._entity_consumer.__aiter__ = lambda self: fake_entity_iter()

    # Execute consumer function once (processes all yielded messages)
    await consumer_worker._consume_entities()

    # Verify message 1 was retried 3 times (nak with delay)
    assert entity_msg1.nak.call_count == 3
    assert entity_msg1.nak.call_args_list[0][1] == {"delay": 2}
    assert entity_msg1.nak.call_args_list[1][1] == {"delay": 4}
    assert entity_msg1.nak.call_args_list[2][1] == {"delay": 8}

    # Verify fourth failure parked the entity
    assert uuid.UUID(ent_id) in consumer_worker._parked_entities

    # DLQ producer called twice: once for final failure of msg1, once for parked skip of msg2
    assert consumer_worker._dlq_producer.publish.call_count == 2

    # First DLQ call
    subject1, dlq_msg1 = consumer_worker._dlq_producer.publish.call_args_list[0][0]
    assert subject1 == "ingest.dlq.outbox_entity"
    assert "Processing failed: DB connection lost" in dlq_msg1.error

    # Second DLQ call (parked skip)
    subject2, dlq_msg2 = consumer_worker._dlq_producer.publish.call_args_list[1][0]
    assert subject2 == "ingest.dlq.outbox_entity"
    assert "is parked due to previous failure" in dlq_msg2.error

    # Verify both messages were termed when DLQ'd
    assert entity_msg1.term.call_count == 1
    assert entity_msg2.term.call_count == 1

    # Verify neither was acked
    entity_msg1.ack.assert_not_called()
    entity_msg2.ack.assert_not_called()
