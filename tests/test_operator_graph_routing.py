"""Tests for operator-event -> Neo4j graph routing (Gap A bridge).

Covers the pure mapper ``operator_event_to_graph`` and the thin async wiring
``route_operator_event_to_graph`` in
``omniscience_server.ingestion.operator_graph``:

- Cluster / Namespace / StorageClass map to one entity each, with the
  operator-emitted ``in_cluster`` topology edge carried through.
- PersistentVolumeClaim maps to one entity PLUS the NEW ``uses_storageclass``
  edge derived from ``metadata["storage_class_name"]`` (PVC -> StorageClass).
- A blank/absent ``storage_class_name`` yields NO ``uses_storageclass`` edge.
- ``cluster`` is preserved in entity metadata so the adapter can promote it to
  the indexed ``:Entity.cluster`` property (f8240b7); ``workspace_id`` is
  stamped on every entity and edge (ACL invariant).
- Entity ids are deterministic over ``(workspace_id, external_id)`` —
  idempotent re-ingest, no duplicates; two workspaces never collide.
- The async router invokes ``GraphStore.upsert_graph`` with the mapped
  entities+edges and the event's ``source_id``.

The mapper is pure (no I/O); the router is tested against an in-memory store
fake that records the ``upsert_graph`` call — no live Neo4j.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from omniscience_server.ingestion.operator_graph import (
    RELATION_IN_CLUSTER,
    RELATION_USES_STORAGECLASS,
    OperatorEvent,
    cluster_scoped_external_id,
    derive_entity_id,
    operator_event_to_graph,
    route_operator_event_to_graph,
)

_WS = uuid.UUID("aaaaaaaa-0000-1111-2222-333344445555")
_WS_OTHER = uuid.UUID("bbbbbbbb-0000-1111-2222-333344445555")
_SOURCE = uuid.UUID("11111111-2222-3333-4444-555566667777")
_CLUSTER_ID = "cccccccc-0000-1111-2222-333344445555"
_CLUSTER_NAME = "prod-eu"


def _cluster_anchor_external_id() -> str:
    return cluster_scoped_external_id(_CLUSTER_ID, "Cluster", _CLUSTER_NAME)


def _storageclass_event(name: str = "gold") -> OperatorEvent:
    external_id = cluster_scoped_external_id(_CLUSTER_ID, "StorageClass", name)
    return OperatorEvent(
        source_id=_SOURCE,
        source_type="k8s-operator",
        external_id=external_id,
        uri=f"kube://{_CLUSTER_NAME}//StorageClass/{name}",
        action="created",
        workspace_id=_WS,
        metadata={
            "cluster": _CLUSTER_NAME,
            "cluster_id": _CLUSTER_ID,
            "namespace": "",
            "name": name,
            "kind": "StorageClass",
            "emitter": "k8s-operator",
        },
        topology_edges=[
            {"kind": RELATION_IN_CLUSTER, "target_external_id": _cluster_anchor_external_id()}
        ],
    )


def _namespace_event(name: str = "payments") -> OperatorEvent:
    external_id = cluster_scoped_external_id(_CLUSTER_ID, "Namespace", name)
    return OperatorEvent(
        source_id=_SOURCE,
        source_type="k8s-operator",
        external_id=external_id,
        action="created",
        workspace_id=_WS,
        metadata={
            "cluster": _CLUSTER_NAME,
            "cluster_id": _CLUSTER_ID,
            "namespace": "",
            "name": name,
            "kind": "Namespace",
        },
        topology_edges=[
            {"kind": RELATION_IN_CLUSTER, "target_external_id": _cluster_anchor_external_id()}
        ],
    )


def _cluster_event() -> OperatorEvent:
    return OperatorEvent(
        source_id=_SOURCE,
        source_type="k8s-operator",
        external_id=_cluster_anchor_external_id(),
        action="created",
        workspace_id=_WS,
        metadata={
            "cluster": _CLUSTER_NAME,
            "cluster_id": _CLUSTER_ID,
            "namespace": "",
            "name": _CLUSTER_NAME,
            "kind": "Cluster",
        },
        topology_edges=[],
    )


def _pvc_event(
    *,
    name: str = "data-pvc",
    namespace: str = "payments",
    storage_class_name: str | None = "gold",
) -> OperatorEvent:
    external_id = f"k8s_resource/{_CLUSTER_ID}/PersistentVolumeClaim/{namespace}/{name}"
    metadata: dict[str, str] = {
        "cluster": _CLUSTER_NAME,
        "cluster_id": _CLUSTER_ID,
        "namespace": namespace,
        "name": name,
        "kind": "PersistentVolumeClaim",
    }
    if storage_class_name is not None:
        metadata["storage_class_name"] = storage_class_name
    return OperatorEvent(
        source_id=_SOURCE,
        source_type="k8s-operator",
        external_id=external_id,
        action="created",
        workspace_id=_WS,
        metadata=metadata,
        topology_edges=[
            {"kind": RELATION_IN_CLUSTER, "target_external_id": _cluster_anchor_external_id()}
        ],
    )


# ---------------------------------------------------------------------------
# Pure mapper — entity shape + cluster + ACL
# ---------------------------------------------------------------------------


def test_storageclass_maps_to_one_entity_with_cluster_promoted() -> None:
    entities, _edges = operator_event_to_graph(_storageclass_event("gold"))

    assert len(entities) == 1
    ent = entities[0]
    assert ent.entity_type == "StorageClass"
    assert ent.name == "gold"
    assert ent.display_name == "gold"
    assert ent.source_id == _SOURCE
    # cluster preserved for the adapter's indexed-property promotion (f8240b7).
    assert ent.metadata["cluster"] == _CLUSTER_NAME
    # ACL: workspace stamped on the entity so upsert_graph can derive the batch.
    assert ent.metadata["workspace_id"] == str(_WS)


def test_storageclass_carries_in_cluster_edge() -> None:
    _entities, edges = operator_event_to_graph(_storageclass_event("gold"))

    assert len(edges) == 1
    edge = edges[0]
    assert edge.edge_type == RELATION_IN_CLUSTER
    assert edge.target_name == _cluster_anchor_external_id()
    assert edge.metadata["workspace_id"] == str(_WS)


def test_namespace_maps_to_entity_and_in_cluster_edge() -> None:
    entities, edges = operator_event_to_graph(_namespace_event("payments"))

    assert entities[0].entity_type == "Namespace"
    assert [e.edge_type for e in edges] == [RELATION_IN_CLUSTER]


def test_cluster_anchor_has_no_edges() -> None:
    entities, edges = operator_event_to_graph(_cluster_event())

    assert entities[0].entity_type == "Cluster"
    assert edges == []


# ---------------------------------------------------------------------------
# Pure mapper — the NEW uses_storageclass edge (PVC -> StorageClass)
# ---------------------------------------------------------------------------


def test_pvc_derives_uses_storageclass_edge() -> None:
    _entities, edges = operator_event_to_graph(_pvc_event(storage_class_name="gold"))

    by_type = {e.edge_type: e for e in edges}
    assert RELATION_IN_CLUSTER in by_type
    assert RELATION_USES_STORAGECLASS in by_type

    uses = by_type[RELATION_USES_STORAGECLASS]
    # Target is the cluster-scoped StorageClass external_id derived from the
    # K8s-native spec.storageClassName the operator stamped on the PVC.
    assert uses.target_name == cluster_scoped_external_id(_CLUSTER_ID, "StorageClass", "gold")
    assert uses.metadata["storage_class_name"] == "gold"
    assert uses.metadata["workspace_id"] == str(_WS)


def test_pvc_without_storage_class_has_no_uses_edge() -> None:
    _entities, edges = operator_event_to_graph(_pvc_event(storage_class_name=None))
    assert all(e.edge_type != RELATION_USES_STORAGECLASS for e in edges)


def test_pvc_blank_storage_class_has_no_uses_edge() -> None:
    _entities, edges = operator_event_to_graph(_pvc_event(storage_class_name="   "))
    assert all(e.edge_type != RELATION_USES_STORAGECLASS for e in edges)


# ---------------------------------------------------------------------------
# Identity — deterministic, idempotent, per-workspace
# ---------------------------------------------------------------------------


def test_entity_id_is_deterministic() -> None:
    e1, _ = operator_event_to_graph(_storageclass_event("gold"))
    e2, _ = operator_event_to_graph(_storageclass_event("gold"))
    assert e1[0].id == e2[0].id
    assert e1[0].id == derive_entity_id(_WS, _storageclass_event("gold").external_id)


def test_same_external_id_different_workspace_yields_distinct_ids() -> None:
    ev_a = _storageclass_event("gold")
    ev_b = _storageclass_event("gold").model_copy(update={"workspace_id": _WS_OTHER})

    a, _ = operator_event_to_graph(ev_a)
    b, _ = operator_event_to_graph(ev_b)
    assert a[0].id != b[0].id
    assert b[0].metadata["workspace_id"] == str(_WS_OTHER)


def test_duplicate_topology_edges_deduped() -> None:
    target = _cluster_anchor_external_id()
    base = _storageclass_event("gold")
    # Rebuild through the model so the duplicate edges are validated/coerced
    # to OperatorEventEdge (model_copy(update=...) would leave raw dicts).
    ev = OperatorEvent(
        source_id=base.source_id,
        source_type=base.source_type,
        external_id=base.external_id,
        action=base.action,
        workspace_id=base.workspace_id,
        metadata=base.metadata,
        topology_edges=[
            {"kind": RELATION_IN_CLUSTER, "target_external_id": target},
            {"kind": RELATION_IN_CLUSTER, "target_external_id": target},
        ],
    )
    _entities, edges = operator_event_to_graph(ev)
    assert len(edges) == 1


# ---------------------------------------------------------------------------
# Async wiring — route_operator_event_to_graph -> GraphStore.upsert_graph
# ---------------------------------------------------------------------------


class _RecordingGraphStore:
    """Records the single upsert_graph call the router makes."""

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
                "snapshot_at": snapshot_at,
            }
        )


@pytest.mark.asyncio
async def test_router_persists_pvc_entity_and_edges() -> None:
    store = _RecordingGraphStore()
    written = await route_operator_event_to_graph(graph_store=store, event=_pvc_event())

    assert written == (1, 2)  # one entity, in_cluster + uses_storageclass
    assert len(store.calls) == 1
    call = store.calls[0]
    assert call["source_id"] == _SOURCE
    assert len(call["entities"]) == 1
    assert {e.edge_type for e in call["edges"]} == {
        RELATION_IN_CLUSTER,
        RELATION_USES_STORAGECLASS,
    }
    # document_id defaults to the entity's own derived id.
    assert call["document_id"] == call["entities"][0].id


@pytest.mark.asyncio
async def test_router_persists_storageclass() -> None:
    store = _RecordingGraphStore()
    written = await route_operator_event_to_graph(graph_store=store, event=_storageclass_event())

    assert written == (1, 1)
    assert store.calls[0]["entities"][0].entity_type == "StorageClass"
