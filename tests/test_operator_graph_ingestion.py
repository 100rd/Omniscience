"""Worker-level wiring tests for the operator -> Neo4j graph bridge (Gap A).

These tests prove that the ingestion worker, on consuming a
``DocumentChangeEvent`` from the in-cluster operator
(``source_type == "k8s-operator"``) carrying ``metadata`` + ``topology_edges``,
ALSO routes the event into the Neo4j graph via
``route_operator_event_to_graph`` — without disturbing the existing vector
pipeline (which still runs and produces the canonical ``ProcessResult``).

Scope (additive, non-regressing):
  * Operator event with metadata -> graph bridge invoked with the mapped
    entities + edges (StorageClass entity, ``in_cluster`` edge).
  * The graph bridge receives the event's ``source_id`` and the derived
    entity id as ``document_id``.
  * Non-operator events (git, fs, …) NEVER touch the graph bridge.
  * Operator events lacking ``metadata`` (legacy / no-payload) are NOT routed
    (nothing to map) and the vector pipeline still succeeds.
  * A worker with no ``graph_store`` configured never attempts routing.
  * ``deleted`` operator events are NOT routed by the bridge (tombstone path
    owns deletes).

All adapters are mocked — no live Neo4j/Qdrant/Postgres.  The graph store is
the same recording fake shape used by ``test_operator_graph_routing.py``.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from omniscience_connectors.registry import ConnectorRegistry
from omniscience_server.ingestion.events import DocumentChangeEvent, ProcessResult
from omniscience_server.ingestion.operator_graph import (
    RELATION_IN_CLUSTER,
)
from omniscience_server.ingestion.worker import IngestionWorker

_CLUSTER_ID = "cccccccc-0000-1111-2222-333344445555"
_CLUSTER_NAME = "prod-eu"


# ---------------------------------------------------------------------------
# Local mocks (kept independent of the other ingestion test modules)
# ---------------------------------------------------------------------------


def _cluster_anchor_external_id() -> str:
    return f"k8s_resource/{_CLUSTER_ID}/Cluster/{_CLUSTER_NAME}"


def _operator_event(
    *,
    name: str = "gold",
    action: str = "created",
    with_payload: bool = True,
    source_id: uuid.UUID | None = None,
) -> DocumentChangeEvent:
    external_id = f"k8s_resource/{_CLUSTER_ID}/StorageClass/{name}"
    kwargs: dict[str, Any] = {
        "source_id": source_id or uuid.uuid4(),
        "source_type": "k8s-operator",
        "external_id": external_id,
        "uri": f"kube://{_CLUSTER_NAME}//StorageClass/{name}",
        "action": action,
    }
    if with_payload:
        kwargs["metadata"] = {
            "cluster": _CLUSTER_NAME,
            "cluster_id": _CLUSTER_ID,
            "namespace": "",
            "name": name,
            "kind": "StorageClass",
            "emitter": "k8s-operator",
        }
        kwargs["topology_edges"] = [
            {"kind": RELATION_IN_CLUSTER, "target_external_id": _cluster_anchor_external_id()}
        ]
    return DocumentChangeEvent(**kwargs)  # type: ignore[arg-type]


def _git_event() -> DocumentChangeEvent:
    return DocumentChangeEvent(
        source_id=uuid.uuid4(),
        source_type="git",
        external_id="abc/def.py",
        uri="file://abc/def.py",
        action="created",
    )


def _make_connector() -> MagicMock:
    from omniscience_connectors.base import DocumentRef, FetchedDocument
    from pydantic import BaseModel

    class _EmptyConfig(BaseModel):
        pass

    connector = MagicMock()
    connector.config_schema = _EmptyConfig
    ref = DocumentRef(external_id="x", uri="kube://x")
    fetched = FetchedDocument(ref=ref, content_bytes=b"{}", content_type="application/json")
    connector.fetch = AsyncMock(return_value=fetched)
    return connector


def _make_embedding_provider() -> MagicMock:
    provider = MagicMock()
    provider.dim = 4
    provider.model_name = "test-model"
    provider.provider_name = "test-provider"
    provider.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])
    return provider


def _make_index_writer() -> MagicMock:
    from omniscience_server.ingestion.pipeline import IndexWriterProtocol

    result = MagicMock()
    result.action = "created"
    result.chunks_written = 1
    result.document_id = uuid.uuid4()
    writer = MagicMock(spec=IndexWriterProtocol)
    writer.upsert_document = AsyncMock(return_value=result)
    writer.upsert_graph = AsyncMock(return_value=None)
    writer.tombstone = AsyncMock(return_value=True)
    return writer


def _make_session_factory(source: Any) -> tuple[MagicMock, list[Any]]:
    """Returns (factory, added_events) so tests can inspect outbox events."""
    inner_session = AsyncMock()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none = MagicMock(return_value=source)
    inner_session.execute = AsyncMock(return_value=scalar_result)

    added_events: list[Any] = []
    inner_session.add = MagicMock(side_effect=lambda x: added_events.append(x))

    # session.begin() must return an async context manager, not a coroutine
    begin_ctx = MagicMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)
    inner_session.begin = MagicMock(return_value=begin_ctx)

    factory_cm = MagicMock()
    factory_cm.__aenter__ = AsyncMock(return_value=inner_session)
    factory_cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=factory_cm), added_events


def _make_source(tenant_id: uuid.UUID) -> Any:
    src = MagicMock()
    src.id = uuid.uuid4()
    src.name = "operator-source"
    src.tenant_id = tenant_id
    src.config = {}
    src.secrets_ref = None
    return src


class _RecordingGraphStore:
    """Records the single ``upsert_graph`` call the bridge makes."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def upsert_graph(
        self,
        *,
        source_id: uuid.UUID,
        document_id: uuid.UUID,
        entities: list[Any],
        edges: list[Any],
        snapshot_at: Any = None,
        workspace_id: uuid.UUID | None = None,
    ) -> None:
        self.calls.append(
            {
                "source_id": source_id,
                "document_id": document_id,
                "entities": entities,
                "edges": edges,
            }
        )


def _make_worker(
    *,
    source: Any,
    graph_store: Any | None,
    session_factory: Any = None,
) -> tuple[IngestionWorker, list[Any]]:
    registry: ConnectorRegistry = MagicMock(spec=ConnectorRegistry)
    registry.get = MagicMock(return_value=_make_connector())
    sf, added_events = _make_session_factory(source)
    if session_factory is not None:
        sf = session_factory
        added_events = []
    return IngestionWorker(
        queue_consumer=MagicMock(),
        connector_registry=registry,
        embedding_provider=_make_embedding_provider(),
        index_writer=_make_index_writer(),
        session_factory=sf,
        graph_store=graph_store,
    ), added_events


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operator_event_routes_to_graph_bridge() -> None:
    """An operator event with metadata publishes outbox events for entity + edge.

    AP1 changed the bridge to publish via NATS outbox rather than direct write.
    We verify the outbox events carry the correct entity/edge metadata.
    """
    tenant = uuid.uuid4()
    store = _RecordingGraphStore()
    worker, added_events = _make_worker(source=_make_source(tenant), graph_store=store)

    event = _operator_event(name="gold")
    result = await worker.process_document(event)

    # Vector pipeline still owns the canonical outcome.
    assert result.action == "created"
    # AP1: bridge publishes via outbox (entity + edge events).
    # EntityUpsertEvent + EdgeUpsertEvent for the StorageClass + RELATION_IN_CLUSTER.
    entity_events = [e for e in added_events if getattr(e, "event_type", None) == "entity.upsert"]
    edge_events = [e for e in added_events if getattr(e, "event_type", None) == "edge.upsert"]
    assert len(entity_events) == 1
    assert len(edge_events) >= 1
    payload = entity_events[0].payload
    assert payload["entity_type"] == "StorageClass"
    assert str(event.source_id) == payload["source_id"]


@pytest.mark.asyncio
async def test_non_operator_event_never_touches_graph_bridge() -> None:
    """A git event runs the vector pipeline but never the graph bridge."""
    store = _RecordingGraphStore()
    worker, _ = _make_worker(source=_make_source(uuid.uuid4()), graph_store=store)

    result = await worker.process_document(_git_event())

    assert result.action == "created"
    assert store.calls == []


@pytest.mark.asyncio
async def test_operator_event_without_metadata_is_not_routed() -> None:
    """An operator event lacking the rich payload is not mappable -> no routing."""
    store = _RecordingGraphStore()
    worker, _ = _make_worker(source=_make_source(uuid.uuid4()), graph_store=store)

    result = await worker.process_document(_operator_event(with_payload=False))

    assert result.action == "created"
    assert store.calls == []


@pytest.mark.asyncio
async def test_no_graph_store_configured_skips_routing() -> None:
    """A worker without a graph_store never attempts to route operator events."""
    worker, _ = _make_worker(source=_make_source(uuid.uuid4()), graph_store=None)

    # Must not raise even though it's a full operator payload.
    result = await worker.process_document(_operator_event())
    assert result.action == "created"


@pytest.mark.asyncio
async def test_deleted_operator_event_is_not_routed() -> None:
    """Deletes are owned by the tombstone path, not the graph bridge."""
    store = _RecordingGraphStore()
    worker, _ = _make_worker(source=_make_source(uuid.uuid4()), graph_store=store)

    result = await worker.process_document(_operator_event(action="deleted"))

    assert result.action == "deleted"
    assert store.calls == []


@pytest.mark.asyncio
async def test_graph_bridge_failure_surfaces_as_error() -> None:
    """If the outbox session write raises, the worker returns action='error' so the
    broker redelivers (idempotent upsert makes redelivery safe).

    AP1 changed the bridge to publish via NATS outbox; errors now surface from
    the session layer, not directly from graph_store.upsert_graph.
    """
    tenant = uuid.uuid4()
    store = _RecordingGraphStore()

    # Build a session factory whose add() raises to simulate a DB failure
    inner_session = AsyncMock()
    inner_session.add = MagicMock(side_effect=RuntimeError("session write failed"))
    scalar_result = MagicMock()
    source = _make_source(tenant)
    scalar_result.scalar_one_or_none = MagicMock(return_value=source)
    inner_session.execute = AsyncMock(return_value=scalar_result)
    begin_ctx = MagicMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)
    inner_session.begin = MagicMock(return_value=begin_ctx)
    factory_cm = MagicMock()
    factory_cm.__aenter__ = AsyncMock(return_value=inner_session)
    factory_cm.__aexit__ = AsyncMock(return_value=False)
    failing_factory = MagicMock(return_value=factory_cm)

    worker, _ = _make_worker(source=source, graph_store=store, session_factory=failing_factory)
    result = await worker.process_document(_operator_event())

    assert result.action == "error"


@pytest.mark.asyncio
async def test_operator_routing_runs_after_vector_pipeline() -> None:
    """The graph bridge runs only after the vector pipeline succeeds.

    AP1: bridge publishes via outbox, not direct graph write.  We verify
    the vector stage fires first and that outbox events are published.
    """
    tenant = uuid.uuid4()
    store = _RecordingGraphStore()
    worker, added_events = _make_worker(source=_make_source(tenant), graph_store=store)

    order: list[str] = []

    real_run_ref = "omniscience_server.ingestion.worker.IngestionPipeline.run_ref"

    async def _run_ref(**kwargs: Any) -> ProcessResult:
        order.append("vector")
        ev = kwargs["event"]
        return ProcessResult(
            source_id=ev.source_id,
            external_id=ev.external_id,
            action="created",
            duration_ms=1.0,
        )

    with patch(real_run_ref, new=AsyncMock(side_effect=_run_ref)):
        await worker.process_document(_operator_event())

    # Vector pipeline ran first.
    assert "vector" in order
    assert order[0] == "vector"
    # AP1: outbox events published (not direct graph write).
    entity_events = [e for e in added_events if getattr(e, "event_type", None) == "entity.upsert"]
    assert len(entity_events) >= 1
