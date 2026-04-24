"""Pgvector-backed adapter for the ``GraphStore`` protocol.

Wraps the existing ``IndexWriter.upsert_graph`` + ``GraphQueryService``
so all current call sites can depend on the protocol rather than on
the concrete pgvector classes.  Zero behavioural change: every method
delegates to code already in production.

See ``packages/core/src/omniscience_core/storage/graph.py`` for the
protocol contract and the ACL invariant.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from omniscience_core.storage.graph import (
    EdgeUpsert,
    EntityNodeView,
    EntityUpsert,
    GraphEdgeView,
    GraphResultView,
)
from omniscience_index.writer import IndexWriter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from omniscience_retrieval.graph_query import (
    EdgeResult,
    EntityNode,
    GraphQueryService,
    GraphResult,
)

log = structlog.get_logger(__name__)


class PgVectorGraphStore:
    """Pgvector-backed ``GraphStore`` — the concrete Phase-1 adapter.

    Composition
    -----------
    - ``IndexWriter``         for entity/edge writes (``upsert_graph``).
    - ``GraphQueryService``   for traversal reads (workspace-scoped).

    Both classes existed before #103; this adapter only re-exposes
    them through the protocol-shaped surface and performs view-model
    translation (``GraphResult`` -> ``GraphResultView``).
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory
        self._writer = IndexWriter(session_factory)
        self._query_service = GraphQueryService(session_factory)

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
        """Delegate to ``IndexWriter.upsert_graph`` unchanged."""
        await self._writer.upsert_graph(
            source_id=source_id,
            document_id=document_id,
            entities=entities,
            edges=edges,
        )

    async def upsert_entity(
        self,
        *,
        entity: EntityUpsert,
        workspace_id: uuid.UUID,
    ) -> None:
        """Not supported on pgvector — use ``upsert_graph`` for batch writes.

        Kept on the protocol for Phase 2 (Neo4j) parity.  The pgvector
        writer intentionally batches entity/edge writes inside a single
        transaction so the graph view stays consistent with the
        document's chunks.  A single-entity upsert would have to
        either (a) break that atomicity, or (b) block on a fan-in
        queue — neither is desirable today.
        """
        raise NotImplementedError(
            "PgVectorGraphStore batches writes via upsert_graph; "
            "granular upsert_entity is reserved for Phase 2 adapters."
        )

    async def upsert_edge(
        self,
        *,
        edge: EdgeUpsert,
        workspace_id: uuid.UUID,
    ) -> None:
        """Not supported on pgvector — use ``upsert_graph`` for batch writes."""
        raise NotImplementedError(
            "PgVectorGraphStore batches writes via upsert_graph; "
            "granular upsert_edge is reserved for Phase 2 adapters."
        )

    async def delete_tombstoned(self) -> int:
        """No-op on pgvector — entities cascade from the owning source.

        Returns 0.  Entities are garbage-collected when a ``Source``
        row is hard-deleted (``ON DELETE CASCADE``).  Tombstoning a
        *document* does not prune entities by design — re-ingestion
        replaces them via ``upsert_graph``.
        """
        return 0

    # ------------------------------------------------------------------
    # Read API — all methods enforce workspace_id
    # ------------------------------------------------------------------

    async def get_entity(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
    ) -> EntityNodeView | None:
        """Resolve an entity by FQN, scoped to ``workspace_id``.

        Returns ``None`` when not found in that workspace.  Cross-
        workspace existence is never leaked.
        """
        try:
            result = await self._query_service.get_related(
                entity_name=entity_name,
                workspace_id=workspace_id,
                max_depth=1,
            )
        except ValueError as exc:
            if str(exc).startswith("entity_not_found:"):
                return None
            raise
        return _node_to_view(result.seed)

    async def find_related(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        """BFS traversal from a seed entity, scoped to ``workspace_id``.

        Delegates to :class:`GraphQueryService.get_related` unchanged
        — the ACL enforcement path is entirely handled there.
        """
        result: GraphResult = await self._query_service.get_related(
            entity_name=entity_name,
            workspace_id=workspace_id,
            max_depth=max_depth,
            edge_types=edge_types,
        )
        return _result_to_view(result)

    async def traverse(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        """Alias of :meth:`find_related` using graph-DB vocabulary."""
        return await self.find_related(
            entity_name=entity_name,
            workspace_id=workspace_id,
            max_depth=max_depth,
            edge_types=edge_types,
        )


# ---------------------------------------------------------------------------
# View-model translators
# ---------------------------------------------------------------------------


def _node_to_view(node: EntityNode) -> EntityNodeView:
    """Translate a retrieval ``EntityNode`` to the core ``EntityNodeView``."""
    return EntityNodeView(
        name=node.name,
        kind=node.kind,
        source=node.source,
        chunk_text=node.chunk_text,
        depth=node.depth,
        edge_type=node.edge_type,
    )


def _edge_to_view(edge: EdgeResult) -> GraphEdgeView:
    """Translate a retrieval ``EdgeResult`` to the core ``GraphEdgeView``."""
    return GraphEdgeView(
        from_entity=edge.from_entity,
        to_entity=edge.to_entity,
        edge_type=edge.edge_type,
    )


def _result_to_view(result: GraphResult) -> GraphResultView:
    """Translate a retrieval ``GraphResult`` to the core ``GraphResultView``."""
    return GraphResultView(
        seed=_node_to_view(result.seed),
        related=[_node_to_view(n) for n in result.related],
        edges=[_edge_to_view(e) for e in result.edges],
    )


__all__ = ["PgVectorGraphStore"]
