"""Operator-event -> Neo4j graph routing (Gap A bridge).

The in-cluster Go operator (``operator/``, ADR-0007) publishes rich entity
events to NATS JetStream — each carrying ``metadata`` (cluster, namespace,
kind, name, and kind-specific fields such as ``storage_class_name``) and a
``topology_edges`` list derived from K8s-native pointers
(``operator/internal/publisher/publisher.go``).  Today those events reach the
server's *vector* pipeline only; their entities and edges never land in the
Neo4j graph.

This module is the bridge.  :func:`operator_event_to_graph` is a **pure**
function that maps one operator event payload into ``(entities, edges)`` ready
for :meth:`omniscience_core.storage.GraphStore.upsert_graph`, and
:func:`route_operator_event_to_graph` is the thin async wiring that invokes the
store.  The objects produced carry exactly the attributes
``omniscience_index.stores.neo4j.mappers`` reads off each entity/edge via
``getattr`` (``id``/``source_id``/``entity_type``/``name``/``display_name``/
``chunk_id``/``metadata`` for entities; ``source_entity_id``/``target_name``/
``edge_type``/``metadata`` for edges) — see ``_entity_to_params`` /
``_edge_to_params`` (the cluster-promotion landed in f8240b7 reads
``metadata["cluster"]`` off these same objects).

Identity
--------
Operator entities are identified by their stable ``external_id`` (see
``operator/internal/entity/common.go``):

  * namespaced:     ``k8s_resource/{cluster_id}/{Kind}/{namespace}/{name}``
  * cluster-scoped: ``k8s_resource/{cluster_id}/{Kind}/{name}``

The graph node id is a deterministic UUIDv5 over ``(workspace_id, external_id)``
so retries and restarts produce stable lineage and the same resource never
spawns duplicate nodes.  Edges that target a node not present in the same batch
are emitted as **name edges** (``target_name`` = the target external_id) so the
Neo4j adapter creates/links a stub that ``resolve_pending_stubs`` later merges.

Supported kinds (this bridge): ``Cluster``, ``Namespace``, ``StorageClass``
(+ the ``in_cluster`` edge), and ``PersistentVolumeClaim`` (+ a NEW
``uses_storageclass`` edge PVC -> StorageClass derived from
``metadata["storage_class_name"]``).  Unknown kinds map to a node with NO
derived edges rather than being dropped — fail-open on coverage, never on ACL.

Wiring
------
The pure mapper and the thin async :func:`route_operator_event_to_graph` are
delivered and unit-tested here.  The ingestion worker invokes the router from
its single-document operator-event path
(:meth:`omniscience_server.ingestion.worker.IngestionWorker._maybe_route_operator_graph`):
``DocumentChangeEvent`` now carries optional ``metadata``/``topology_edges``
fields (``events.py``), which the worker projects onto :class:`OperatorEvent`
(stamping the *server-resolved* ``workspace_id`` from ``Source.tenant_id`` —
never a payload field) and hands to this router AFTER the vector pipeline runs.
The vector path is untouched; routing is a no-op for non-operator events and
for operator events without a ``metadata`` payload.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Canonical entity-id namespace prefix every operator external_id starts with
# (operator/internal/entity/common.go::EntityKindPrefix).
ENTITY_KIND_PREFIX = "k8s_resource"

# Edge relation types (match the operator's snake_case + the Neo4j edge-type
# regex ^[A-Za-z][A-Za-z0-9_]{0,63}$).
RELATION_IN_CLUSTER = "in_cluster"
# NEW edge (this bridge): a PersistentVolumeClaim consumes a StorageClass.
RELATION_USES_STORAGECLASS = "uses_storageclass"

# Fixed UUIDv5 namespace for deriving operator graph-node ids.  Distinct from
# the operator's own SourceID/ClusterID namespace so the two derivations never
# collide.
_NS_OPERATOR_GRAPH = uuid.UUID("8c3b1f4a-2d6e-4b7c-9a1f-5e0d2c4b6a7f")

_KIND_STORAGECLASS = "StorageClass"
_KIND_PVC = "PersistentVolumeClaim"


# ---------------------------------------------------------------------------
# Wire model — the rich operator event payload
# ---------------------------------------------------------------------------


class OperatorEventEdge(BaseModel):
    """One topology edge on an operator event (EdgeRef in the Go publisher)."""

    kind: str = Field(description="Relation type, e.g. 'in_cluster'.")
    target_external_id: str = Field(description="Stable external_id of the edge target.")


class OperatorEvent(BaseModel):
    """The operator's NATS payload (superset of ``DocumentChangeEvent``).

    Mirrors ``operator/internal/entity/entity.go::Event`` — snake_case JSON
    tags, ``metadata`` carrying cluster/namespace/kind/name plus kind-specific
    fields, and an optional ``topology_edges`` list.  ``edges`` (owner edges)
    is accepted but not consumed by this bridge.
    """

    source_id: uuid.UUID
    source_type: str
    external_id: str
    uri: str = ""
    action: Literal["created", "updated", "deleted"]
    workspace_id: uuid.UUID
    metadata: dict[str, str] = Field(default_factory=dict)
    topology_edges: list[OperatorEventEdge] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Graph write-side objects — shaped for GraphStore.upsert_graph
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OperatorGraphEntity:
    """Entity object consumed by ``GraphStore.upsert_graph``.

    Carries exactly the attributes the Neo4j ``_entity_to_params`` mapper
    reads via ``getattr``; ``metadata`` carries ``workspace_id`` (used by the
    adapter to derive the batch workspace) and ``cluster`` (promoted to the
    indexed ``:Entity.cluster`` property in f8240b7).
    """

    id: uuid.UUID
    source_id: uuid.UUID
    entity_type: str
    name: str
    display_name: str
    chunk_id: uuid.UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OperatorGraphEdge:
    """Edge object consumed by ``GraphStore.upsert_graph``.

    Uses ``target_name`` (the target external_id) so the Neo4j adapter creates
    a stub when the target is not in the same batch — the standard cross-batch
    edge pattern (``resolve_pending_stubs`` merges it later).
    """

    source_entity_id: uuid.UUID
    target_name: str
    edge_type: str
    target_entity_id: uuid.UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------


def derive_entity_id(workspace_id: uuid.UUID, external_id: str) -> uuid.UUID:
    """Return a deterministic graph-node id for ``(workspace_id, external_id)``.

    Stable across operator restarts and event redeliveries so the same K8s
    resource maps to the same node — idempotent upserts, no duplicates.
    """
    return uuid.uuid5(_NS_OPERATOR_GRAPH, f"{workspace_id}/{external_id}")


def cluster_scoped_external_id(cluster_id: str, kind: str, name: str) -> str:
    """Build a cluster-scoped external_id (StorageClass/Namespace/PV/Node).

    Mirrors ``operator/internal/entity/common.go::externalIDForClusterScoped``:
    ``k8s_resource/{cluster_id}/{Kind}/{name}``.
    """
    return f"{ENTITY_KIND_PREFIX}/{cluster_id}/{kind}/{name}"


# ---------------------------------------------------------------------------
# Pure mapper
# ---------------------------------------------------------------------------


def operator_event_to_graph(
    event: OperatorEvent,
) -> tuple[list[OperatorGraphEntity], list[OperatorGraphEdge]]:
    """Map one operator event into ``(entities, edges)`` for ``upsert_graph``.

    Pure — no I/O.  Always returns exactly one entity (the event's own
    resource).  Edges are derived from the K8s-native ``topology_edges`` the
    operator already emits (e.g. the ``in_cluster`` edge on Cluster-scoped
    resources) PLUS, for a ``PersistentVolumeClaim``, the NEW
    ``uses_storageclass`` edge synthesised from ``metadata["storage_class_name"]``
    (the operator emits PVC ``in_cluster`` only; the PVC->StorageClass link is
    derived here from the K8s-native ``spec.storageClassName`` the operator
    stamped into metadata — never inferred from name patterns).

    A blank/absent ``storage_class_name`` yields NO ``uses_storageclass`` edge
    (a PVC may bind a default class or none) — fail-closed on a missing pointer
    rather than fabricating an edge to an empty target.
    """
    meta = dict(event.metadata)
    kind = meta.get("kind", "")
    name = meta.get("name", event.external_id)
    cluster = meta.get("cluster")
    cluster_id = meta.get("cluster_id", "")

    entity_metadata: dict[str, Any] = dict(meta)
    entity_metadata["workspace_id"] = str(event.workspace_id)
    # ``cluster`` is promoted to the indexed :Entity.cluster property by the
    # adapter (f8240b7); keep it in metadata so the promotion has a source.
    if cluster is not None:
        entity_metadata["cluster"] = cluster

    entity = OperatorGraphEntity(
        id=derive_entity_id(event.workspace_id, event.external_id),
        source_id=event.source_id,
        # Surface the K8s Kind as the entity_type so ``list_entities`` can
        # filter by kind directly (StorageClass, PersistentVolumeClaim, …).
        entity_type=kind or ENTITY_KIND_PREFIX,
        name=name,
        display_name=name,
        chunk_id=None,
        metadata=entity_metadata,
    )

    source_entity_id = entity.id
    edges: list[OperatorGraphEdge] = []
    seen_targets: set[tuple[str, str]] = set()

    # 1) Operator-emitted topology edges (e.g. in_cluster on Cluster-scoped
    #    resources).  Carried through verbatim as name edges to the target's
    #    external_id.
    for edge in event.topology_edges:
        relation = edge.kind
        target = edge.target_external_id
        if not relation or not target:
            continue
        key = (relation, target)
        if key in seen_targets:
            continue
        seen_targets.add(key)
        edges.append(
            OperatorGraphEdge(
                source_entity_id=source_entity_id,
                target_name=target,
                edge_type=relation,
                metadata={"workspace_id": str(event.workspace_id)},
            )
        )

    # 2) NEW derived edge: PVC -> StorageClass (uses_storageclass), built from
    #    the K8s-native spec.storageClassName the operator stamped on the PVC.
    if kind == _KIND_PVC:
        storage_class_name = (meta.get("storage_class_name") or "").strip()
        if storage_class_name and cluster_id:
            target = cluster_scoped_external_id(cluster_id, _KIND_STORAGECLASS, storage_class_name)
            key = (RELATION_USES_STORAGECLASS, target)
            if key not in seen_targets:
                seen_targets.add(key)
                edges.append(
                    OperatorGraphEdge(
                        source_entity_id=source_entity_id,
                        target_name=target,
                        edge_type=RELATION_USES_STORAGECLASS,
                        metadata={
                            "workspace_id": str(event.workspace_id),
                            "storage_class_name": storage_class_name,
                        },
                    )
                )

    return [entity], edges


# ---------------------------------------------------------------------------
# Thin async wiring
# ---------------------------------------------------------------------------


async def route_operator_event_to_graph(
    *,
    graph_store: Any,
    event: OperatorEvent,
    document_id: uuid.UUID | None = None,
) -> tuple[int, int]:
    """Map ``event`` and persist it via ``GraphStore.upsert_graph``.

    Returns ``(entities_written, edges_written)``.  ``document_id`` defaults to
    the entity's own derived id when not supplied (the operator path has no
    separate Document PK at this layer).  Deletes are handled by the existing
    tombstone path, not here — this routes created/updated events into the
    graph; a ``deleted`` action still upserts the terminal state so the graph
    reflects the last-known shape (end-dating is the adapter's job).
    """
    entities, edges = operator_event_to_graph(event)
    doc_id = document_id if document_id is not None else entities[0].id
    await graph_store.upsert_graph(
        source_id=event.source_id,
        document_id=doc_id,
        entities=entities,
        edges=edges,
    )
    return len(entities), len(edges)


__all__ = [
    "RELATION_IN_CLUSTER",
    "RELATION_USES_STORAGECLASS",
    "OperatorEvent",
    "OperatorEventEdge",
    "OperatorGraphEdge",
    "OperatorGraphEntity",
    "cluster_scoped_external_id",
    "derive_entity_id",
    "operator_event_to_graph",
    "route_operator_event_to_graph",
]
