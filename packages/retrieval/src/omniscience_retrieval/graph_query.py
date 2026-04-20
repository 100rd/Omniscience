"""BFS graph traversal service for the Omniscience entity graph.

Provides :class:`GraphQueryService` which traverses the entity/edge graph
starting from a named seed entity, up to a configurable depth, with optional
edge-type filtering.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

import structlog
from omniscience_core.db.models import Chunk, Edge, Entity
from sqlalchemy import select
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


class GraphQueryService:
    """Traverse the entity graph using BFS from a named seed entity.

    Parameters
    ----------
    session_factory:
        Async SQLAlchemy session factory (context-manager style).
    """

    def __init__(self, session_factory: Any) -> None:
        self._factory = session_factory

    async def get_related(
        self,
        entity_name: str,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResult:
        """BFS traversal from a seed entity through its edges.

        Parameters
        ----------
        entity_name:
            FQN (``name`` column) of the seed entity.
        max_depth:
            Maximum number of hops from the seed (≥1).  Values <1 are
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
            When no entity with the given name exists in the DB.
        """
        max_depth = max(1, max_depth)

        session: AsyncSession
        async with self._factory() as session:
            # Resolve seed entity
            seed_entity = await self._fetch_entity_by_name(session, entity_name)
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

                # Fetch all edges originating from (outgoing) OR targeting
                # (incoming) the current entity so the traversal is undirected.
                next_hops = await self._fetch_adjacent(session, current_id, edge_types=edge_types)

                for edge, neighbor_entity in next_hops:
                    neighbor_id = str(neighbor_entity.id)
                    # Record the edge regardless of whether neighbor is new
                    # (we add the edge only once per direction though).
                    from_name = (
                        seed_entity.name
                        if edge.source_entity_id == seed_entity.id
                        else neighbor_entity.name
                    )
                    # Determine proper from/to from the edge itself.
                    from_entity_result = await self._fetch_entity_by_id(
                        session, str(edge.source_entity_id)
                    )
                    to_entity_result = await self._fetch_entity_by_id(
                        session, str(edge.target_entity_id)
                    )
                    from_name = from_entity_result.name if from_entity_result else from_name
                    to_name = to_entity_result.name if to_entity_result else neighbor_entity.name

                    edge_result = EdgeResult(
                        from_entity=from_name,
                        to_entity=to_name,
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
            max_depth=max_depth,
            entities_found=1 + len(related_nodes),
            edges_traversed=len(result_edges),
        )

        return GraphResult(seed=seed_node, related=related_nodes, edges=result_edges)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _fetch_entity_by_name(self, session: AsyncSession, name: str) -> Entity | None:
        """Return the first Entity whose ``name`` matches exactly."""
        result = await session.execute(select(Entity).where(Entity.name == name).limit(1))
        return result.scalar_one_or_none()

    async def _fetch_entity_by_id(self, session: AsyncSession, entity_id: str) -> Entity | None:
        """Return an Entity by its primary key."""
        from uuid import UUID

        return await session.get(Entity, UUID(entity_id))

    async def _fetch_chunk_text(self, session: AsyncSession, chunk_id: object) -> str | None:
        """Return the first chunk's text for an entity, or None."""
        if chunk_id is None:
            return None
        from uuid import UUID

        chunk = await session.get(Chunk, UUID(str(chunk_id)))
        return chunk.text if chunk is not None else None

    async def _fetch_adjacent(
        self,
        session: AsyncSession,
        entity_id: str,
        edge_types: list[str] | None,
    ) -> list[tuple[Edge, Entity]]:
        """Return (edge, neighbor_entity) pairs for all adjacent edges.

        Traversal is bidirectional: we look at both outgoing
        (source_entity_id == entity_id) and incoming
        (target_entity_id == entity_id) edges.
        """
        from uuid import UUID

        eid = UUID(entity_id)

        # Outgoing edges
        outgoing_query = (
            select(Edge, Entity)
            .join(Entity, Edge.target_entity_id == Entity.id)
            .where(Edge.source_entity_id == eid)
        )

        # Incoming edges
        incoming_query = (
            select(Edge, Entity)
            .join(Entity, Edge.source_entity_id == Entity.id)
            .where(Edge.target_entity_id == eid)
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
