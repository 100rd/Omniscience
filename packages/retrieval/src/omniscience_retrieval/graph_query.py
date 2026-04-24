"""BFS graph traversal service for the Omniscience entity graph.

Provides :class:`GraphQueryService` which traverses the entity/edge graph
starting from a named seed entity, up to a configurable depth, with optional
edge-type filtering.

Workspace isolation (see issue #117)
------------------------------------

Every public method requires an explicit ``workspace_id`` and confines both
the seed lookup and the BFS expansion to entities whose owning
``sources.tenant_id`` matches that workspace.  ``NULL`` tenant rows (legacy
data that predates multi-tenancy) are admitted alongside the caller's own
workspace — this mirrors the backward-compat semantics of
:func:`omniscience_core.auth.workspace.workspace_filter`.

The ``workspace_id`` parameter is keyword-only and required — there is no
default — so a caller without a workspace cannot silently widen the
traversal.  Callers that lack a workspace must fail upstream (REST/MCP
layer) before reaching this service.

Scoping is applied at three points to be transitively complete:

1. Seed lookup (``_fetch_entity_by_name``).
2. BFS expansion (``_fetch_adjacent``) — the NEIGHBOUR entity must also
   belong to the workspace, so a planted cross-tenant edge cannot leak.
3. Edge-endpoint resolution (``_fetch_entity_by_id``) used to populate
   the ``from``/``to`` names on result edges.  Any endpoint outside the
   workspace causes the edge to be dropped entirely rather than surfaced
   with partial information.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import structlog
from omniscience_core.db.models import Chunk, Edge, Entity, Source
from sqlalchemy import ColumnElement, Select, null, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Result models (plain dataclasses — no Pydantic dependency here)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EntityNode:
    """A single entity node returned in a graph traversal result."""

    name: str
    kind: str  # entity_type field
    source: str  # source_id as string
    chunk_text: str | None
    depth: int = 0
    edge_type: str | None = None


@dataclass(slots=True)
class EdgeResult:
    """A directed edge returned in a graph traversal result."""

    from_entity: str
    to_entity: str
    edge_type: str


@dataclass
class GraphResult:
    """Complete result of a graph traversal."""

    seed: EntityNode
    related: list[EntityNode] = field(default_factory=list)
    edges: list[EdgeResult] = field(default_factory=list)

    @property
    def stats(self) -> dict[str, int]:
        depth_reached = max((n.depth for n in self.related), default=0)
        return {
            "entities_found": 1 + len(self.related),
            "edges_traversed": len(self.edges),
            "depth_reached": depth_reached,
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the MCP/REST wire format."""
        seed_dict: dict[str, Any] = {
            "name": self.seed.name,
            "kind": self.seed.kind,
            "source": self.seed.source,
            "chunk_text": self.seed.chunk_text,
        }
        related_list: list[dict[str, Any]] = [
            {
                "name": n.name,
                "kind": n.kind,
                "source": n.source,
                "chunk_text": n.chunk_text,
                "depth": n.depth,
                "edge_type": n.edge_type,
            }
            for n in self.related
        ]
        edges_list: list[dict[str, Any]] = [
            {
                "from": e.from_entity,
                "to": e.to_entity,
                "type": e.edge_type,
            }
            for e in self.edges
        ]
        return {
            "seed": seed_dict,
            "related": related_list,
            "edges": edges_list,
            "stats": self.stats,
        }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def _workspace_predicate(workspace_id: uuid.UUID) -> ColumnElement[bool]:
    """Return the WHERE predicate that confines a query to *workspace_id*.

    Entities and edges do not carry workspace metadata directly — ownership
    is inherited from the owning :class:`Source` via its ``tenant_id``
    column.  Every query that applies this predicate MUST join
    ``Entity.source_id == Source.id`` first.

    ``NULL`` tenant_id rows are always included for backward compatibility
    with legacy data that predates multi-tenancy.
    """
    return or_(Source.tenant_id == workspace_id, Source.tenant_id == null())


class GraphQueryService:
    """Traverse the entity graph using BFS from a named seed entity.

    All queries are workspace-scoped.  Callers supply an explicit
    ``workspace_id`` on every invocation; there is no way to ask for a
    cross-workspace view (by design — see issue #117).

    Parameters
    ----------
    session_factory:
        Async SQLAlchemy session factory (context-manager style).
    """

    def __init__(self, session_factory: Any) -> None:
        self._factory = session_factory

    async def get_related(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResult:
        """BFS traversal from a seed entity through its edges.

        Parameters
        ----------
        entity_name:
            FQN (``name`` column) of the seed entity.
        workspace_id:
            REQUIRED workspace UUID.  Seed lookup AND every traversal hop
            are confined to this workspace.  There is no "skip filtering"
            sentinel value — a caller that cannot name a workspace must
            fail upstream.
        max_depth:
            Maximum number of hops from the seed (>=1).  Values <1 are
            clamped to 1.
        edge_types:
            Optional allowlist of edge types to follow.  When ``None``,
            all edge types are traversed.

        Returns
        -------
        GraphResult
            Seed entity, related nodes (with depth/edge_type), and the
            edges that connect them.

        Raises
        ------
        ValueError
            When no entity with the given name exists in the caller's
            workspace.  Cross-workspace entities are indistinguishable
            from "not found" by design — we never leak existence.
        """
        max_depth = max(1, max_depth)

        session: AsyncSession
        async with self._factory() as session:
            # 1. Resolve seed entity within the caller's workspace only.
            seed_entity = await self._fetch_entity_by_name(
                session, entity_name, workspace_id=workspace_id
            )
            if seed_entity is None:
                raise ValueError(f"entity_not_found:{entity_name}")

            seed_chunk_text = await self._fetch_chunk_text(session, seed_entity.chunk_id)

            seed_node = EntityNode(
                name=seed_entity.name,
                kind=seed_entity.entity_type,
                source=str(seed_entity.source_id),
                chunk_text=seed_chunk_text,
                depth=0,
            )

            # BFS state
            visited_ids: set[str] = {str(seed_entity.id)}
            # Queue entries: (entity_id, current_depth, edge_type_used)
            frontier: deque[tuple[str, int, str | None]] = deque()
            frontier.append((str(seed_entity.id), 0, None))

            related_nodes: list[EntityNode] = []
            result_edges: list[EdgeResult] = []

            while frontier:
                current_id, current_depth, _ = frontier.popleft()

                if current_depth >= max_depth:
                    continue

                # 2. Expand — only neighbours in the same workspace are
                # returned.  A planted cross-tenant edge is invisible here.
                next_hops = await self._fetch_adjacent(
                    session,
                    current_id,
                    edge_types=edge_types,
                    workspace_id=workspace_id,
                )

                for edge, neighbor_entity in next_hops:
                    neighbor_id = str(neighbor_entity.id)
                    # 3. Resolve the canonical from/to names for the edge,
                    # re-applying the workspace predicate on both endpoints.
                    # If either endpoint is outside the workspace, skip the
                    # edge entirely — do not surface partial/mixed data.
                    from_entity_result = await self._fetch_entity_by_id(
                        session,
                        str(edge.source_entity_id),
                        workspace_id=workspace_id,
                    )
                    to_entity_result = await self._fetch_entity_by_id(
                        session,
                        str(edge.target_entity_id),
                        workspace_id=workspace_id,
                    )
                    if from_entity_result is None or to_entity_result is None:
                        continue

                    edge_result = EdgeResult(
                        from_entity=from_entity_result.name,
                        to_entity=to_entity_result.name,
                        edge_type=edge.edge_type,
                    )
                    # Deduplicate edges
                    if edge_result not in result_edges:
                        result_edges.append(edge_result)

                    if neighbor_id not in visited_ids:
                        visited_ids.add(neighbor_id)
                        chunk_text = await self._fetch_chunk_text(
                            session, neighbor_entity.chunk_id
                        )
                        next_depth = current_depth + 1
                        node = EntityNode(
                            name=neighbor_entity.name,
                            kind=neighbor_entity.entity_type,
                            source=str(neighbor_entity.source_id),
                            chunk_text=chunk_text,
                            depth=next_depth,
                            edge_type=edge.edge_type,
                        )
                        related_nodes.append(node)
                        frontier.append((neighbor_id, next_depth, edge.edge_type))

        log.info(
            "graph_traversal_complete",
            seed=entity_name,
            workspace_id=str(workspace_id),
            max_depth=max_depth,
            entities_found=1 + len(related_nodes),
            edges_traversed=len(result_edges),
        )

        return GraphResult(seed=seed_node, related=related_nodes, edges=result_edges)

    # ------------------------------------------------------------------
    # Private helpers — every read pipes through the workspace predicate
    # ------------------------------------------------------------------

    async def _fetch_entity_by_name(
        self,
        session: AsyncSession,
        name: str,
        *,
        workspace_id: uuid.UUID,
    ) -> Entity | None:
        """Return the first matching Entity scoped to *workspace_id*."""
        stmt: Select[Any] = (
            select(Entity)
            .join(Source, Entity.source_id == Source.id)
            .where(Entity.name == name)
            .where(_workspace_predicate(workspace_id))
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _fetch_entity_by_id(
        self,
        session: AsyncSession,
        entity_id: str,
        *,
        workspace_id: uuid.UUID,
    ) -> Entity | None:
        """Return an Entity by PK, constrained to *workspace_id*."""
        stmt: Select[Any] = (
            select(Entity)
            .join(Source, Entity.source_id == Source.id)
            .where(Entity.id == uuid.UUID(entity_id))
            .where(_workspace_predicate(workspace_id))
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _fetch_chunk_text(self, session: AsyncSession, chunk_id: object) -> str | None:
        """Return the chunk's text, or None.

        Chunks do not carry workspace metadata directly; they are reached
        only via entities we have already authorised, so fetching by PK
        is safe once the owning entity has passed the workspace filter.
        """
        if chunk_id is None:
            return None

        chunk = await session.get(Chunk, uuid.UUID(str(chunk_id)))
        return chunk.text if chunk is not None else None

    async def _fetch_adjacent(
        self,
        session: AsyncSession,
        entity_id: str,
        edge_types: list[str] | None,
        *,
        workspace_id: uuid.UUID,
    ) -> list[tuple[Edge, Entity]]:
        """Return (edge, neighbor_entity) pairs whose NEIGHBOUR is in *workspace_id*.

        Traversal is bidirectional: we look at both outgoing
        (source_entity_id == entity_id) and incoming
        (target_entity_id == entity_id) edges.  For every hop, the
        neighbour entity (and therefore its owning source) must itself
        belong to the workspace — this closes the transitive leak path.
        """
        eid = uuid.UUID(entity_id)

        outgoing_query: Select[Any] = (
            select(Edge, Entity)
            .join(Entity, Edge.target_entity_id == Entity.id)
            .join(Source, Entity.source_id == Source.id)
            .where(Edge.source_entity_id == eid)
            .where(_workspace_predicate(workspace_id))
        )

        incoming_query: Select[Any] = (
            select(Edge, Entity)
            .join(Entity, Edge.source_entity_id == Entity.id)
            .join(Source, Entity.source_id == Source.id)
            .where(Edge.target_entity_id == eid)
            .where(_workspace_predicate(workspace_id))
        )

        if edge_types:
            outgoing_query = outgoing_query.where(Edge.edge_type.in_(edge_types))
            incoming_query = incoming_query.where(Edge.edge_type.in_(edge_types))

        out_result = await session.execute(outgoing_query)
        in_result = await session.execute(incoming_query)

        pairs: list[tuple[Edge, Entity]] = []
        for row in out_result:
            pairs.append((row[0], row[1]))
        for row in in_result:
            pairs.append((row[0], row[1]))

        return pairs


__all__ = ["EdgeResult", "EntityNode", "GraphQueryService", "GraphResult"]
