"""AP1 single-writer invariant — unit tests.

Covers:
- OutboxWorker sets entity_id on outbox events (entity.merge / entity.unmerge)
- EntityLinker routes edge writes through outbox when session_factory is provided
- EntityLinker falls back to direct write when session_factory is None
- REST merge_entities writes OutboxEvent and returns queued=True
- REST unmerge_entity writes OutboxEvent and returns queued=True
- REST unmerge_entity requires admin/entities:write scope (AP6)
- OutboxConsumerWorker routes entity.merge to graph_store.merge_nodes
- OutboxConsumerWorker routes entity.unmerge to graph_store.unmerge_node
- Source.tenant_id is used (not the non-existent workspace_id)
"""

from __future__ import annotations

import os
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from omniscience_core.db.models import OutboxEvent
from omniscience_core.queue.messages import EntityMergeEvent
from omniscience_core.storage.graph import GraphStore
from omniscience_core.storage.vector import VectorStore
from omniscience_embeddings.base import EmbeddingProvider
from omniscience_index.linker import EntityLinker
from omniscience_server.outbox_consumer import OutboxConsumerWorker
from omniscience_server.outbox_worker import OutboxWorker
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session_factory(added: list[Any]) -> Any:
    """Return an async_sessionmaker-like mock that collects session.add() calls."""
    session = AsyncMock(spec=AsyncSession)
    session.add = MagicMock(side_effect=added.append)
    session.begin = MagicMock()
    session.begin.return_value.__aenter__ = AsyncMock(return_value=None)
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock()
    factory.return_value = session
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


# ---------------------------------------------------------------------------
# OutboxWorker: entity_id propagation for entity.merge / entity.unmerge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox_worker_sets_entity_id_on_merge_event() -> None:
    """OutboxWorker publishes entity.merge events with the correct subject."""
    source_id = uuid.uuid4()
    target_id = uuid.uuid4()
    workspace_id = uuid.uuid4()

    event = OutboxEvent(
        id=uuid.uuid4(),
        event_type="entity.merge",
        entity_id=source_id,
        payload={
            "workspace_id": str(workspace_id),
            "source_id": str(source_id),
            "target_id": str(target_id),
            "merged_node_id": None,
        },
        processed=False,
    )

    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalars.return_value.all.return_value = [event]
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

    worker._producer.publish.assert_called_once()
    subject, payload = worker._producer.publish.call_args[0]
    assert subject == "outbox.entity.merge"
    assert isinstance(payload, EntityMergeEvent)
    assert payload.workspace_id == str(workspace_id)
    assert payload.source_id == str(source_id)
    assert payload.target_id == str(target_id)
    assert event.processed is True
    assert event.entity_id == source_id


@pytest.mark.asyncio
async def test_outbox_worker_sets_entity_id_on_unmerge_event() -> None:
    """OutboxWorker publishes entity.unmerge events with the correct subject."""
    merged_node_id = uuid.uuid4()
    workspace_id = uuid.uuid4()

    event = OutboxEvent(
        id=uuid.uuid4(),
        event_type="entity.unmerge",
        entity_id=merged_node_id,
        payload={
            "workspace_id": str(workspace_id),
            "source_id": None,
            "target_id": None,
            "merged_node_id": str(merged_node_id),
        },
        processed=False,
    )

    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalars.return_value.all.return_value = [event]
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

    worker._producer.publish.assert_called_once()
    subject, payload = worker._producer.publish.call_args[0]
    assert subject == "outbox.entity.unmerge"
    assert isinstance(payload, EntityMergeEvent)
    assert payload.merged_node_id == str(merged_node_id)
    assert event.processed is True


# ---------------------------------------------------------------------------
# EntityLinker: outbox path vs direct-write fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_linker_emits_outbox_event_when_session_factory_provided() -> None:
    """EntityLinker._emit_edge_via_outbox writes OutboxEvent(edge.upsert) when injected."""
    from omniscience_core.storage.graph import EntityNodeView

    added: list[Any] = []
    factory = _make_session_factory(added)

    graph_store = AsyncMock(spec=GraphStore)
    graph_store.resolve_pending_stubs = AsyncMock(return_value=0)

    ws_id = uuid.uuid4()
    source_id = uuid.uuid4()  # the source whose entities we are linking
    other_source_id = uuid.uuid4()
    src_entity_id = uuid.uuid4()
    tgt_entity_id = uuid.uuid4()

    # Two entities with the same name but different sources → exact_name_match fires
    src_node = EntityNodeView(
        id=src_entity_id,
        name="service://checkout",
        kind="otel_service",
        source=str(source_id),
        chunk_text=None,
    )
    tgt_node = EntityNodeView(
        id=tgt_entity_id,
        name="service://checkout",  # same name → exact_name_match → link created
        kind="otel_service",
        source=str(other_source_id),
        chunk_text=None,
    )
    graph_store.get_all_entities = AsyncMock(return_value=[src_node, tgt_node])

    linker = EntityLinker(graph_store=graph_store, session_factory=factory)
    created = await linker.link_entities(source_id=source_id, workspace_id=ws_id)

    assert created == 1
    # Must NOT call graph_store.upsert_edge (the legacy direct write)
    graph_store.upsert_edge.assert_not_called()
    # Must have written an OutboxEvent
    outbox_rows = [o for o in added if isinstance(o, OutboxEvent)]
    assert len(outbox_rows) == 1
    assert outbox_rows[0].event_type == "edge.upsert"
    assert outbox_rows[0].entity_id == src_entity_id


@pytest.mark.asyncio
async def test_linker_falls_back_to_direct_write_without_session_factory() -> None:
    """EntityLinker calls graph_store.upsert_edge when no session_factory (legacy path)."""
    from omniscience_core.storage.graph import EntityNodeView

    graph_store = AsyncMock(spec=GraphStore)
    graph_store.resolve_pending_stubs = AsyncMock(return_value=0)

    ws_id = uuid.uuid4()
    source_id = uuid.uuid4()
    other_source_id = uuid.uuid4()

    src_node = EntityNodeView(
        id=uuid.uuid4(),
        name="service://inventory",
        kind="otel_service",
        source=str(source_id),
        chunk_text=None,
    )
    tgt_node = EntityNodeView(
        id=uuid.uuid4(),
        name="service://inventory",  # exact_name_match
        kind="otel_service",
        source=str(other_source_id),
        chunk_text=None,
    )
    graph_store.get_all_entities = AsyncMock(return_value=[src_node, tgt_node])

    linker = EntityLinker(graph_store=graph_store, session_factory=None)
    created = await linker.link_entities(source_id=source_id, workspace_id=ws_id)

    assert created == 1
    # Legacy path calls upsert_edge directly
    graph_store.upsert_edge.assert_called_once()


# ---------------------------------------------------------------------------
# REST merge_entities / unmerge_entity: outbox routing + AP6 scope guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_entities_writes_outbox_event() -> None:
    """merge_entities handler inserts OutboxEvent(entity.merge) and returns queued=True."""
    from omniscience_server.rest.entities import merge_entities

    added: list[Any] = []
    factory = _make_session_factory(added)

    ws_id = uuid.uuid4()
    source_id = uuid.uuid4()
    target_id = uuid.uuid4()

    token = MagicMock()
    token.workspace_id = ws_id
    token.scopes = ["search", "admin/entities:write"]

    with patch("omniscience_server.rest.entities.get_workspace_id", return_value=ws_id):
        from omniscience_server.rest.entities import MergeNodesRequest

        req_body = MergeNodesRequest(source_id=source_id, target_id=target_id)

        app_state = MagicMock()
        app_state.db_session_factory = factory
        request = MagicMock()
        request.app.state = app_state

        result = await merge_entities(req=req_body, request=request, token=token)

    assert result["queued"] is True
    assert result["success"] is True

    outbox_rows = [o for o in added if isinstance(o, OutboxEvent)]
    assert len(outbox_rows) == 1
    row = outbox_rows[0]
    assert row.event_type == "entity.merge"
    assert row.entity_id == source_id
    assert row.payload["source_id"] == str(source_id)
    assert row.payload["target_id"] == str(target_id)


@pytest.mark.asyncio
async def test_unmerge_entity_writes_outbox_event() -> None:
    """unmerge_entity handler inserts OutboxEvent(entity.unmerge) and returns queued=True."""
    from omniscience_server.rest.entities import unmerge_entity

    added: list[Any] = []
    factory = _make_session_factory(added)

    ws_id = uuid.uuid4()
    merged_node_id = uuid.uuid4()

    token = MagicMock()
    token.workspace_id = ws_id
    token.scopes = ["search", "admin/entities:write"]

    with patch("omniscience_server.rest.entities.get_workspace_id", return_value=ws_id):
        from omniscience_server.rest.entities import UnmergeNodeRequest

        req_body = UnmergeNodeRequest(merged_node_id=merged_node_id)

        app_state = MagicMock()
        app_state.db_session_factory = factory
        request = MagicMock()
        request.app.state = app_state

        result = await unmerge_entity(req=req_body, request=request, token=token)

    assert result["queued"] is True
    assert result["success"] is True

    outbox_rows = [o for o in added if isinstance(o, OutboxEvent)]
    assert len(outbox_rows) == 1
    row = outbox_rows[0]
    assert row.event_type == "entity.unmerge"
    assert row.entity_id == merged_node_id
    assert row.payload["merged_node_id"] == str(merged_node_id)


@pytest.mark.asyncio
async def test_unmerge_entity_requires_admin_scope_ap6() -> None:
    """AP6: unmerge_entity returns 403 when admin/entities:write scope is missing."""
    from fastapi import HTTPException
    from omniscience_server.rest.entities import unmerge_entity

    ws_id = uuid.uuid4()
    merged_node_id = uuid.uuid4()

    token = MagicMock()
    token.workspace_id = ws_id
    token.scopes = ["search"]  # missing admin/entities:write

    with patch("omniscience_server.rest.entities.get_workspace_id", return_value=ws_id):
        from omniscience_server.rest.entities import UnmergeNodeRequest

        req_body = UnmergeNodeRequest(merged_node_id=merged_node_id)
        request = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            await unmerge_entity(req=req_body, request=request, token=token)

    assert exc_info.value.status_code == 403
    assert "admin/entities:write" in exc_info.value.detail


@pytest.mark.asyncio
async def test_merge_entities_requires_admin_scope() -> None:
    """merge_entities returns 403 when admin/entities:write scope is missing."""
    from fastapi import HTTPException
    from omniscience_server.rest.entities import merge_entities

    ws_id = uuid.uuid4()

    token = MagicMock()
    token.workspace_id = ws_id
    token.scopes = ["search"]  # missing admin/entities:write

    with patch("omniscience_server.rest.entities.get_workspace_id", return_value=ws_id):
        from omniscience_server.rest.entities import MergeNodesRequest

        req_body = MergeNodesRequest(source_id=uuid.uuid4(), target_id=uuid.uuid4())
        request = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            await merge_entities(req=req_body, request=request, token=token)

    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# OutboxConsumerWorker: entity.merge / entity.unmerge routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_routes_entity_merge_to_graph_store() -> None:
    """OutboxConsumerWorker._consume_merges calls graph_store.merge_nodes for entity.merge."""
    nats_conn = MagicMock()
    nats_conn.jetstream = AsyncMock()

    graph_store = AsyncMock(spec=GraphStore)
    graph_store.merge_nodes = AsyncMock(return_value=True)
    vector_store = AsyncMock(spec=VectorStore)
    embedding_provider = AsyncMock(spec=EmbeddingProvider)

    consumer = OutboxConsumerWorker(
        nats_conn=nats_conn,
        graph_store=graph_store,
        vector_store=vector_store,
        embedding_provider=embedding_provider,
    )

    ws_id = str(uuid.uuid4())
    source_id = str(uuid.uuid4())
    target_id = str(uuid.uuid4())

    msg = AsyncMock()
    msg.subject = "outbox.entity.merge"
    msg.payload = EntityMergeEvent(
        workspace_id=ws_id,
        source_id=source_id,
        target_id=target_id,
        merged_node_id=None,
    )

    async def fake_merge_iter() -> Any:
        yield msg

    consumer._merge_consumer = AsyncMock()
    consumer._merge_consumer.__aiter__ = lambda self: fake_merge_iter()

    await consumer._consume_merges()

    graph_store.merge_nodes.assert_called_once()
    kwargs = graph_store.merge_nodes.call_args[1]
    assert kwargs["workspace_id"] == uuid.UUID(ws_id)
    assert kwargs["source_id"] == uuid.UUID(source_id)
    assert kwargs["target_id"] == uuid.UUID(target_id)
    msg.ack.assert_called_once()


@pytest.mark.asyncio
async def test_consumer_routes_entity_unmerge_to_graph_store() -> None:
    """OutboxConsumerWorker._consume_merges calls graph_store.unmerge_node for entity.unmerge."""
    nats_conn = MagicMock()
    nats_conn.jetstream = AsyncMock()

    graph_store = AsyncMock(spec=GraphStore)
    graph_store.unmerge_node = AsyncMock(return_value=True)
    vector_store = AsyncMock(spec=VectorStore)
    embedding_provider = AsyncMock(spec=EmbeddingProvider)

    consumer = OutboxConsumerWorker(
        nats_conn=nats_conn,
        graph_store=graph_store,
        vector_store=vector_store,
        embedding_provider=embedding_provider,
    )

    ws_id = str(uuid.uuid4())
    merged_node_id = str(uuid.uuid4())

    msg = AsyncMock()
    msg.subject = "outbox.entity.unmerge"
    msg.payload = EntityMergeEvent(
        workspace_id=ws_id,
        source_id=None,
        target_id=None,
        merged_node_id=merged_node_id,
    )

    async def fake_merge_iter() -> Any:
        yield msg

    consumer._merge_consumer = AsyncMock()
    consumer._merge_consumer.__aiter__ = lambda self: fake_merge_iter()

    await consumer._consume_merges()

    graph_store.unmerge_node.assert_called_once()
    kwargs = graph_store.unmerge_node.call_args[1]
    assert kwargs["workspace_id"] == uuid.UUID(ws_id)
    assert kwargs["merged_node_id"] == uuid.UUID(merged_node_id)
    msg.ack.assert_called_once()


# ---------------------------------------------------------------------------
# Source.tenant_id correctness (regression guard for the runtime bug fix)
# ---------------------------------------------------------------------------


def test_source_model_has_tenant_id_not_workspace_id() -> None:
    """Source model must have tenant_id; workspace_id does not exist (runtime bug guard)."""
    from omniscience_core.db.models import Source

    # The model must have tenant_id as a mapped column
    mapper = Source.__mapper__
    col_names = {c.key for c in mapper.columns}
    assert "tenant_id" in col_names, "Source must have tenant_id column"
    assert "workspace_id" not in col_names, (
        "Source.workspace_id does not exist — code must use tenant_id"
    )


def test_outbox_event_has_entity_id_column() -> None:
    """Migration 0013: OutboxEvent must have an entity_id column."""
    from omniscience_core.db.models import OutboxEvent

    mapper = OutboxEvent.__mapper__
    col_names = {c.key for c in mapper.columns}
    assert "entity_id" in col_names, "OutboxEvent must have entity_id partition key column"


def test_entity_merge_event_model_fields() -> None:
    """EntityMergeEvent has the correct required and optional fields."""
    ws_id = str(uuid.uuid4())
    src = str(uuid.uuid4())
    tgt = str(uuid.uuid4())

    merge_evt = EntityMergeEvent(workspace_id=ws_id, source_id=src, target_id=tgt)
    assert merge_evt.workspace_id == ws_id
    assert merge_evt.source_id == src
    assert merge_evt.target_id == tgt
    assert merge_evt.merged_node_id is None

    merged_id = str(uuid.uuid4())
    unmerge_evt = EntityMergeEvent(workspace_id=ws_id, merged_node_id=merged_id)
    assert unmerge_evt.merged_node_id == merged_id
    assert unmerge_evt.source_id is None
    assert unmerge_evt.target_id is None


# ---------------------------------------------------------------------------
# AP1 CAS live integration assertion
# (gated by OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1 — same gate as bitemporal
#  contract tests in test_neo4j_store_bitemporal_writer.py)
# ---------------------------------------------------------------------------

_CONTRACT = pytest.mark.skipif(
    not os.environ.get("OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS"),
    reason="set OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1 to run live Neo4j contract tests",
)


@_CONTRACT
@pytest.mark.asyncio
async def test_live_cas_stale_write_does_not_regress_entity_version() -> None:
    """AP1 live contract: write entity v5 to live Neo4j, attempt v3, read back version==5.

    Authoritative end-to-end guard that the CAS Cypher fires correctly
    against a real Neo4j node property.  Uses the URI/password from env
    vars, defaulting to the local docker-compose service.
    """
    import os as _os

    from omniscience_core.storage.graph import EntityUpsert
    from omniscience_index.stores.neo4j.store import Neo4jGraphStore, Neo4jStoreConfig

    neo4j_uri = _os.environ.get("OMNISCIENCE_NEO4J_URI", "bolt://localhost:7687")
    neo4j_pw = _os.environ.get("OMNISCIENCE_NEO4J_PASSWORD", "password")

    config = Neo4jStoreConfig(
        uri=neo4j_uri,
        username="neo4j",
        password=neo4j_pw,
        database="neo4j",
        max_connection_pool_size=5,
        connection_acquisition_timeout_seconds=10.0,
        max_transaction_retry_time_seconds=10.0,
        default_max_depth=3,
        bitemporal_enabled=False,
    )
    store = Neo4jGraphStore(config=config)
    await store.connect()

    ws_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    source_id = uuid.uuid4()

    try:
        # Step 1: write entity at version 5
        e5 = EntityUpsert(
            id=entity_id,
            source_id=source_id,
            entity_type="service",
            name="svc.ap1-live-cas",
            display_name="svc.ap1-live-cas",
            chunk_id=None,
            metadata={},
            version=5,
        )
        await store.upsert_entity(entity=e5, workspace_id=ws_id)

        # Step 2: attempt stale re-delivery at version 3
        e3 = EntityUpsert(
            id=entity_id,
            source_id=source_id,
            entity_type="service",
            name="svc.ap1-live-cas",
            display_name="svc.ap1-live-cas",
            chunk_id=None,
            metadata={},
            version=3,
        )
        await store.upsert_entity(entity=e3, workspace_id=ws_id)

        # Step 3: read back — entity must still be at version 5
        versions = await store.get_entity_versions(workspace_id=ws_id)
        assert entity_id in versions, (
            f"AP1 live: entity {entity_id} not found in versions map for workspace {ws_id}"
        )
        assert versions[entity_id] == 5, (
            f"AP1 live CAS violation: entity.version should be 5 after stale v3 redelivery, "
            f"got {versions[entity_id]}.  The per-entity CAS guard is not working."
        )
    finally:
        await store.close()
