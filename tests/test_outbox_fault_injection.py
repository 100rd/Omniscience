"""Test fault-injection and recovery of Outbox + Reconciler."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_core.db.models import Entity
from omniscience_core.queue.messages import EntityUpsertEvent
from omniscience_core.storage.graph import GraphStore
from omniscience_core.storage.vector import VectorStore
from omniscience_embeddings.base import EmbeddingProvider
from omniscience_server.outbox_consumer import OutboxConsumerWorker
from omniscience_server.reconcile_worker import ReconcileWorker


@pytest.mark.asyncio
async def test_qdrant_crash_healed_by_outbox_and_reconciler() -> None:
    ws_id = str(uuid.uuid4())
    ent_id = str(uuid.uuid4())
    src_id = str(uuid.uuid4())

    nats_conn = MagicMock()
    nats_conn.jetstream = AsyncMock()

    # GraphStore works fine
    graph_store = AsyncMock(spec=GraphStore)
    graph_store.get_entity_versions = AsyncMock(return_value={uuid.UUID(ent_id): 5})

    # VectorStore is drifting
    vector_store = AsyncMock(spec=VectorStore)
    vector_store.get_entity_versions = AsyncMock(return_value={uuid.UUID(ent_id): 0})

    # FAULT INJECTION: Qdrant fails during write
    vector_store.upsert_chunks.side_effect = Exception("Qdrant crashed!")

    embedding_provider = AsyncMock(spec=EmbeddingProvider)
    embedding_provider.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3]])

    # Setup OutboxConsumerWorker
    consumer = OutboxConsumerWorker(
        nats_conn=nats_conn,
        graph_store=graph_store,
        vector_store=vector_store,
        embedding_provider=embedding_provider,
    )
    consumer._dlq_producer = AsyncMock()

    # Message for Outbox Consumer
    entity_msg = AsyncMock()
    entity_msg.payload = EntityUpsertEvent(
        workspace_id=ws_id,
        id=ent_id,
        source_id=src_id,
        entity_type="test",
        name="test_entity",
        display_name="Test",
        metadata={},
        version=5,
    )
    entity_msg.subject = "outbox.entity.upsert"

    async def fake_entity_iter():
        for _ in range(4):  # 3 retries + 1 initial
            yield entity_msg

    consumer._entity_consumer = AsyncMock()
    consumer._entity_consumer.__aiter__ = lambda self: fake_entity_iter()

    # STEP 1: Consumer processes message, hits the Qdrant crash, fails max_retries, and parks entity
    await consumer._consume_entities()

    assert uuid.UUID(ent_id) in consumer._parked_entities
    assert vector_store.upsert_chunks.call_count == 4
    assert consumer._dlq_producer.publish.call_count == 1

    # STEP 2: Reconciler runs and detects drift
    ent_mock = MagicMock(spec=Entity)
    ent_mock.id = uuid.UUID(ent_id)
    ent_mock.source_id = uuid.UUID(src_id)
    ent_mock.entity_type = "test"
    ent_mock.name = "test_entity"
    ent_mock.display_name = "Test"
    ent_mock.entity_metadata = {}
    ent_mock.version = 5

    result_mock = MagicMock()
    result_mock.scalars().all.return_value = [ent_mock]

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result_mock)
    added_events = []
    session.add = MagicMock(side_effect=lambda x: added_events.append(x))

    tx = AsyncMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=tx)

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)

    reconciler = ReconcileWorker(
        session_factory=factory,
        vector_store=vector_store,
        graph_store=graph_store,
        settings=MagicMock(reconcile_tick_seconds=60, reconcile_orphan_delete_limit=200),
    )

    await reconciler._check_entity_drift(workspace_id=uuid.UUID(ws_id))

    # Reconciler should publish an OutboxEvent backfill
    assert len(added_events) == 1
    outbox_event = added_events[0]
    assert outbox_event.event_type == "entity.upsert"
    assert outbox_event.payload["is_backfill"] is True

    # STEP 3: Consumer receives backfill with Qdrant recovered
    vector_store.upsert_chunks.side_effect = None
    vector_store.upsert_chunks.reset_mock()
    consumer._parked_entities.clear()  # Backfill un-parks the entity

    backfill_msg = AsyncMock()
    backfill_msg.payload = EntityUpsertEvent(**outbox_event.payload)
    backfill_msg.subject = "outbox.entity.upsert"

    async def fake_backfill_iter():
        yield backfill_msg

    consumer._entity_consumer.__aiter__ = lambda self: fake_backfill_iter()

    await consumer._consume_entities()

    # Vector store gets upserted properly this time
    assert vector_store.upsert_chunks.call_count == 1
    backfill_msg.ack.assert_called_once()
