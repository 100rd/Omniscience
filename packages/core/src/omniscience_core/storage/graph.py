"""``GraphStore`` protocol: backend-neutral entity/edge persistence and traversal.

This module defines the typed contract that the Neo4j adapter
(``omniscience_index.stores.neo4j_store``) and PostgresOnlyStore satisfy.

Design rules (ADR-0005 + issue #117)
------------------------------------

1. **Workspace is an invariant, not a parameter.**  Every read method
   takes ``workspace_id: uuid.UUID`` as a **keyword-only, required**
   parameter.  There is no default, no ``None`` sentinel, no "admin"
   bypass.  An adapter MUST confine every read to the supplied
   workspace, including transitive hops across edges.

2. **Signatures mirror current call sites.**  The protocol is driven
   by ingestion-time and retrieval-time use-cases; it is deliberately
   not a superset of any single backend's capabilities.

3. **View-model dataclasses are ORM-free.**  ``EntityNodeView`` and
   ``GraphEdgeView`` carry only primitive/uuid fields so a Neo4j or
   Qdrant adapter can populate them without re-importing SQLAlchemy
   models.

AP1 — single-writer invariant (migration 0013)
----------------------------------------------

* ``delete_tombstoned_graph`` replaces the old ``delete_tombstoned`` name
  (which clashed with ``VectorStore.delete_tombstoned``).
* ``upsert_graph`` now requires ``workspace_id`` (keyword-only).
* ``upsert_edge_by_name`` uses ``source_entity_id`` / ``metadata`` to match
  both Neo4j and PostgresOnlyStore implementations.
* ``list_entities`` protocol is truth — ``kind`` is required, ``limit``
  / ``offset`` are NOT in the protocol (move them to adapter internals
  if needed).
* ``get_entity_versions`` added for per-entity anti-entropy (AP2
  closed-loop reconciliation, ``reconcile_worker.py``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# View-model dataclasses (ORM-free)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EntityNodeView:
    """Backend-neutral view of a single entity returned from a traversal."""

    id: uuid.UUID
    name: str
    kind: str
    source: str
    chunk_text: str | None
    depth: int = 0
    edge_type: str | None = None
    # Human-readable label for display / matching (e.g. Jira ticket key).
    # Optional: populated when the backend stores a distinct display label.
    display_name: str | None = None
    # ADR-0008 §1 — bitemporal triple.
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    is_parked: bool = False
    recorded_at: datetime | None = None
    version: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class GraphEdgeView:
    """Backend-neutral view of a directed edge between two entities."""

    from_entity: str
    to_entity: str
    edge_type: str
    # ADR-0008 §3 — bitemporal triple as relationship properties.
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    recorded_at: datetime | None = None


@dataclass
class GraphResultView:
    """Container for the result of a traversal (seed + related + edges)."""

    seed: EntityNodeView
    related: list[EntityNodeView] = field(default_factory=list)
    edges: list[GraphEdgeView] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Write-side input dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EntityUpsert:
    """Input payload for ``GraphStore.upsert_entity``."""

    id: uuid.UUID
    source_id: uuid.UUID
    entity_type: str
    name: str
    display_name: str
    chunk_id: uuid.UUID | None
    metadata: dict[str, Any] = field(default_factory=dict)
    version: int | None = None
    epoch: int | None = None
    forced_replay: bool = False
    # AP2 — per-entity content hash for cross-store anti-entropy.
    content_hash: str | None = None


@dataclass(slots=True)
class EdgeUpsert:
    """Input payload for ``GraphStore.upsert_edge``."""

    source_entity_id: uuid.UUID
    target_entity_id: uuid.UUID
    edge_type: str
    metadata: dict[str, Any] = field(default_factory=dict)
    version: int | None = None
    epoch: int | None = None
    forced_replay: bool = False


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class GraphStore(Protocol):
    """Abstract graph-store contract (entity + edge persistence + traversal)."""

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    async def upsert_graph(
        self,
        *,
        source_id: uuid.UUID,
        document_id: uuid.UUID,
        entities: list[Any],
        edges: list[Any],
        workspace_id: uuid.UUID,
        snapshot_at: datetime | None = None,
        version: int | None = None,
        epoch: int | None = None,
        forced_replay: bool = False,
    ) -> None: ...

    async def upsert_entity(
        self,
        *,
        entity: EntityUpsert,
        workspace_id: uuid.UUID,
    ) -> None: ...

    async def upsert_edge(
        self,
        *,
        edge: EdgeUpsert,
        workspace_id: uuid.UUID,
    ) -> None: ...

    async def upsert_edge_by_name(
        self,
        *,
        source_entity_id: uuid.UUID,
        target_name: str,
        edge_type: str,
        workspace_id: uuid.UUID,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Create an edge from ``source_entity_id`` to a target identified by
        name (creates a stub target node if the target does not yet exist).

        ``metadata`` is stored as relationship properties in addition to the
        mandatory ``workspace_id``.
        """
        ...

    async def delete_tombstoned_graph(self) -> int:
        """Remove tombstoned entity nodes from the graph store.

        Renamed from ``delete_tombstoned`` (AP1) to avoid collision with
        ``VectorStore.delete_tombstoned`` which carries a different signature
        (``older_than: timedelta``).

        Returns the number of nodes deleted.
        """
        ...

    async def resolve_pending_stubs(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> int:
        """Merge stub nodes with real entities in the same workspace.

        A *stub* is a placeholder entity node created by
        :meth:`upsert_edge_by_name` when the target entity does not yet
        exist.  After the target is ingested, this method performs a
        batch Cypher MATCH+MERGE to replace stub nodes with the real
        entity (preserving existing edges).

        Returns:
            Number of stub nodes resolved (merged into real entities) in
            this call.  Returns ``0`` when there is nothing to resolve.
        """
        ...

    # ------------------------------------------------------------------
    # Read API — every method REQUIRES workspace_id (keyword-only)
    # ------------------------------------------------------------------

    async def get_entity(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> EntityNodeView | None: ...

    async def find_related(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView: ...

    async def traverse(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView: ...

    async def list_entities(
        self,
        *,
        workspace_id: uuid.UUID,
        kind: str,
        cluster: str | None = None,
        name: str | None = None,
        as_of: datetime | None = None,
    ) -> list[EntityNodeView]:
        """List entities of ``kind`` within ``workspace_id``, optionally
        filtered by the indexed ``cluster`` property and/or exact ``name``.

        Answers deterministic platform-state questions such as "which
        StorageClasses exist in cluster X" and "does a StorageClass named
        'gold' exist in cluster X" for the change-validation gate.  The
        ``cluster`` filter matches a first-class indexed node property
        (never a metadata substring scan).  ``as_of`` returns the version
        valid at T per ADR-0008 §5; the workspace predicate always leads.
        """
        ...

    async def find_entities_by_metadata(
        self,
        *,
        workspace_id: uuid.UUID,
        key: str,
        value: Any,
    ) -> list[EntityNodeView]:
        """Find entities where metadata[key] == value within workspace."""
        ...

    async def get_all_entities(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> list[EntityNodeView]:
        """Return all entities for a workspace (use with caution)."""
        ...

    # ------------------------------------------------------------------
    # Stats API — issue #111 (workspace-scoped histograms + totals)
    # ------------------------------------------------------------------

    async def count_entities(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> int:
        """Return total number of entities visible in ``workspace_id``.

        Workspace-scoped per the protocol's ACL invariant; cross-workspace
        widening is forbidden.  Used by the stats overview endpoint.
        """
        ...

    async def count_entities_by_kind(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[str, int]:
        """Return a histogram of entity kinds within ``workspace_id``.

        Map keys are the entity ``kind`` strings (e.g. ``"function"``,
        ``"terraform_resource"``); values are non-negative counts.
        """
        ...

    async def count_edges_by_type(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[str, int]:
        """Return a histogram of edge types within ``workspace_id``.

        Map keys are the edge ``edge_type`` strings (e.g. ``"calls"``);
        values are non-negative counts.  Edges are counted once even when
        they connect entities across multiple sources within the same
        workspace.
        """
        ...

    async def count_entities_by_source(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[str, int]:
        """Return a per-source entity count map within ``workspace_id``.

        Map keys are the stringified ``source_id`` UUIDs; values are
        non-negative counts.  Used to assemble the per-source stats
        table without N+1 queries.
        """
        ...

    async def merge_nodes(
        self,
        *,
        workspace_id: uuid.UUID,
        source_id: uuid.UUID,
        target_id: uuid.UUID,
    ) -> bool:
        """Merge source_id node into target_id node reversibly."""
        ...

    async def unmerge_node(
        self,
        *,
        workspace_id: uuid.UUID,
        merged_node_id: uuid.UUID,
    ) -> bool:
        """Split/unmerge a previously merged node back to its original identity."""
        ...

    async def get_entity_versions(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[uuid.UUID, int]:
        """Return a map of entity_id → version for all entities in the workspace.

        Used by the reconcile worker (AP2) to detect per-entity version drift
        between Postgres (source of truth) and graph/vector projections.  Only
        returns entities currently visible in this store — entities absent from
        the returned dict are treated as having version 0 (need upsert).
        """
        ...

    async def get_entity_content_hashes(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[uuid.UUID, str]:
        """Return a map of entity_id → content_hash for entities that have been hashed.

        AP2 — per-entity anti-entropy: used by the reconcile worker to detect
        same-version content drift (e.g. a projection whose content_hash differs
        from Postgres ``Entity.content_hash``).  Entities without a stored hash
        (written before AP2) are omitted from the result.  The reconcile worker
        skips hash comparison for omitted entities and falls back to version-only
        drift detection.
        """
        ...


__all__ = [
    "EdgeUpsert",
    "EntityNodeView",
    "EntityUpsert",
    "GraphEdgeView",
    "GraphResultView",
    "GraphStore",
]
