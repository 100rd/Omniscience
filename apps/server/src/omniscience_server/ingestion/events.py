"""Event types for the ingestion pipeline.

``DocumentChangeEvent`` is the payload consumed from the NATS
``INGEST_CHANGES`` stream.  ``ProcessResult`` captures the outcome of
running a single document through the pipeline.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class TopologyEdge(BaseModel):
    """One K8s-native topology edge carried on an operator change event.

    Mirrors the operator publisher's ``EdgeRef`` (``operator/internal/
    publisher/publisher.go``) — ``kind`` is the relation type (e.g.
    ``in_cluster``) and ``target_external_id`` is the stable external_id of
    the edge target.  Defined here (rather than imported from
    ``operator_graph``) so the wire model stays dependency-light; the worker
    re-projects these onto ``OperatorEventEdge`` when invoking the bridge.
    """

    kind: str
    target_external_id: str


class DocumentChangeEvent(BaseModel):
    """Payload emitted by source connectors on every document change.

    Published to ``ingest.changes.<source_type>`` and consumed by
    :class:`~omniscience_server.ingestion.worker.IngestionWorker`.

    Operator-event extension (Gap A wiring)
    ---------------------------------------
    The in-cluster Go operator (``source_type == "k8s-operator"``) publishes a
    richer payload than other connectors: per-entity ``metadata`` (cluster,
    namespace, kind, name, and kind-specific fields such as
    ``storage_class_name``) and a list of K8s-native ``topology_edges``.  Both
    are **optional** additive fields — non-operator connectors omit them and
    the existing vector pipeline is unchanged.  When present, the worker also
    routes the event into the Neo4j graph via
    :func:`~omniscience_server.ingestion.operator_graph.route_operator_event_to_graph`.
    """

    source_id: UUID
    """Database PK of the :class:`~omniscience_core.db.models.Source` row."""

    source_type: str
    """Connector type string (e.g. ``"git"``, ``"fs"``, ``"k8s-operator"``)."""

    external_id: str
    """Source-native document identifier — stable across syncs."""

    uri: str
    """Human-readable address for the document (URL, file path, …)."""

    action: Literal["created", "updated", "deleted"]
    """Change type as reported by the connector."""

    metadata: dict[str, str] | None = None
    """Operator-only: per-entity metadata (cluster/namespace/kind/name and
    kind-specific fields like ``storage_class_name``).  ``None`` for
    non-operator connectors — the vector pipeline never reads it."""

    topology_edges: list[TopologyEdge] | None = Field(default=None)
    """Operator-only: K8s-native topology edges (e.g. ``in_cluster``).
    ``None`` for non-operator connectors."""


class ProcessResult(BaseModel):
    """Outcome of processing a single :class:`DocumentChangeEvent`.

    Returned by :meth:`~omniscience_server.ingestion.pipeline.IngestionPipeline.run`
    and used by :class:`~omniscience_server.ingestion.worker.IngestionWorker`
    to update run counters and emit metrics.
    """

    source_id: UUID
    external_id: str
    action: str
    """Effective action: ``created``, ``updated``, ``unchanged``, ``deleted``, or ``error``."""

    duration_ms: float
    """Wall-clock time to process this document, in milliseconds."""

    error: str | None = None
    """Human-readable error message when ``action == "error"``."""


__all__ = ["DocumentChangeEvent", "ProcessResult", "TopologyEdge"]
