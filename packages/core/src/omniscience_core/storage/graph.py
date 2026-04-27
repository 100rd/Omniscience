"""``GraphStore`` protocol: backend-neutral entity/edge persistence and traversal.

This module defines the typed contract that the Neo4j adapter
(``omniscience_index.stores.neo4j_store``) satisfies.  Prior to v0.2
the pgvector adapter (``omniscience_retrieval.adapters.pgvector_graph``)
also satisfied this contract; it was removed at the #105 cutover.

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
    """Backend-neutral view of a single entity returned from a traversal.

    Populated by every ``GraphStore`` implementation.  Mirrors the shape
    consumed by REST/MCP handlers in the current pgvector path.

    Bitemporal fields (``valid_from``, ``valid_to``, ``recorded_at``) are
    optional per ADR-0008 §1.  They are populated when the read path
    surfaces a ``:Entity`` mirror or an ``:EntityState`` row that carries
    the bitemporal triple.  Pre-bitemporal legacy rows that pre-date
    issue #130's backfill leave them as ``None``.  Callers that do not
    care about time-travel semantics can ignore the fields entirely
    (the protocol's ``as_of`` parameter is opt-in).
    """

    name: str
    kind: str
    source: str
    chunk_text: str | None
    depth: int = 0
    edge_type: str | None = None
    # ADR-0008 §1 — bitemporal triple.  Optional: legacy callers / legacy
    # rows leave them None.  See ADR-0008 §5 for the open-closed
    # ``[valid_from, valid_to)`` interval convention and ``valid_to=None``
    # as the "still valid" sentinel.
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    recorded_at: datetime | None = None


@dataclass(slots=True)
class GraphEdgeView:
    """Backend-neutral view of a directed edge between two entities.

    Bitemporal fields follow the same contract as :class:`EntityNodeView`
    (ADR-0008 §3 — every relationship carries the triple as direct
    relationship properties).  Optional for legacy callers.
    """

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
    """Input payload for ``GraphStore.upsert_entity``.

    Shaped to match ``ExtractedEntity`` from the parsers package so the
    ingestion pipeline can pass values through with minimal conversion.
    """

    id: uuid.UUID
    source_id: uuid.UUID
    entity_type: str
    name: str
    display_name: str
    chunk_id: uuid.UUID | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class EdgeUpsert:
    """Input payload for ``GraphStore.upsert_edge``."""

    source_entity_id: uuid.UUID
    target_entity_id: uuid.UUID
    edge_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class GraphStore(Protocol):
    """Abstract graph-store contract (entity + edge persistence + traversal).

    Implementations
    ---------------
    - ``omniscience_index.stores.neo4j_store.Neo4jGraphStore`` — the
      sole production backend as of v0.2 (ADR-0005, issue #104).

    ACL invariant
    -------------
    Every read method MUST apply a workspace filter derived from the
    required ``workspace_id`` parameter.  An adapter that silently
    widens a read across workspaces is a P0 security bug — see issue
    #117 and the P0 fix in PR #119.
    """

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
    ) -> None:
        """Persist a batch of entities and edges for one document.

        On re-ingestion the previous entities/edges for this source are
        deleted and replaced so the graph stays consistent with the
        current document content.

        ``entities`` and ``edges`` are typed as ``list[Any]`` to admit
        both the parser's ``ExtractedEntity``/``ExtractedEdge`` shapes
        and the protocol-native ``EntityUpsert``/``EdgeUpsert``
        dataclasses — adapters duck-type the attribute names.  This is
        the single hot ingestion path.
        """
        ...

    async def upsert_entity(
        self,
        *,
        entity: EntityUpsert,
        workspace_id: uuid.UUID,
    ) -> None:
        """Insert or update a single entity.

        Granular upsert is provided for backends (Neo4j) that can write
        one node at a time efficiently.  Batch writes go through
        :meth:`upsert_graph`.
        """
        ...

    async def upsert_edge(
        self,
        *,
        edge: EdgeUpsert,
        workspace_id: uuid.UUID,
    ) -> None:
        """Insert or update a single edge.

        See :meth:`upsert_entity` — batch writes go through
        :meth:`upsert_graph`.
        """
        ...

    async def delete_tombstoned(self) -> int:
        """Hard-delete entities orphaned by tombstoned documents.

        Returns the number of entity rows removed.  Tombstoning a
        *document* does not delete its entities by design
        (re-ingestion replaces them), but an adapter may implement
        proactive cleanup for long-abandoned sources.
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
    ) -> EntityNodeView | None:
        """Resolve an entity by fully-qualified name, within workspace.

        Returns ``None`` when no entity with that name exists in the
        caller's workspace.  Cross-workspace entities are
        indistinguishable from "not found" by design — existence is
        never leaked.

        Parameters
        ----------
        entity_name:
            Fully-qualified ``Entity.name`` string.
        workspace_id:
            REQUIRED.  The traversal is confined to this workspace.
        as_of:
            Optional point-in-time anchor.  When ``None`` (default),
            returns the still-current state via the ``:Entity`` mirror —
            byte-identical to the pre-bitemporal contract (ADR-0008
            §5 hot-path guarantee).  When set, returns the
            ``:EntityState`` version that satisfies the ADR-0008 §5
            canonical predicate ``valid_from <= as_of AND (as_of <
            valid_to OR valid_to IS NULL)``.  Issue #132.
        """
        ...

    async def find_related(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        """BFS traversal from a seed entity through its edges.

        Parameters
        ----------
        entity_name:
            Fully-qualified name of the seed entity.
        workspace_id:
            REQUIRED.  Seed lookup AND every traversal hop are
            confined to this workspace.  There is no "skip filtering"
            sentinel.
        as_of:
            Optional point-in-time anchor.  ``None`` (default) returns
            the still-current graph.  When set, every node in the
            traversal (seed + neighbours) AND every relationship
            satisfies the ADR-0008 §5 canonical predicate; a relationship
            is included iff it was valid at ``as_of`` AND both endpoints
            were valid at ``as_of``.  Issue #132.
        max_depth:
            Maximum number of hops from the seed (>=1).  Values <1
            are clamped to 1.
        edge_types:
            Optional allowlist of edge types to follow.

        Raises
        ------
        ValueError
            When no entity with the given name exists in the caller's
            workspace at ``as_of`` (``"entity_not_found:<name>"``).
        """
        ...

    async def traverse(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        """Alias of ``find_related`` using the graph-database vocabulary.

        Kept on the protocol because issue #103 enumerates ``traverse``
        as a first-class method name.  ``as_of`` semantics mirror
        :meth:`find_related` — see issue #132 / ADR-0008 §5.
        """
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


__all__ = [
    "EdgeUpsert",
    "EntityNodeView",
    "EntityUpsert",
    "GraphEdgeView",
    "GraphResultView",
    "GraphStore",
]
