"""Test-only ``GraphStore`` proxy backed by ``GraphQueryService``.

The production v0.2 code path wires ``app.state.graph_store`` to a
:class:`omniscience_index.stores.Neo4jGraphStore` instance.  The MCP
and REST handlers consume it through the ``GraphStore`` protocol and
never depend on concrete backend semantics.

Most unit tests in :mod:`tests.test_graph_query` and
:mod:`tests.test_graph_workspace_isolation` do **not** test the Neo4j
adapter — they test the MCP/REST wiring and the underlying
``GraphQueryService`` SQL shape.  Spinning up a Neo4j testcontainer
for those would be wasteful (and impossible in the default CI
lane).  Instead these tests use :class:`_GraphQueryProxy` below,
which satisfies the ``GraphStore`` protocol by delegating directly
to a ``GraphQueryService`` built from an in-memory fake session
factory.

This proxy replaces the ``PgVectorGraphStore`` adapter that was
removed at the #105 cutover.  It exists only inside the test tree;
production code never imports it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from omniscience_core.storage.graph import (
    EdgeUpsert,
    EntityNodeView,
    EntityUpsert,
    GraphEdgeView,
    GraphResultView,
    GraphStore,
)
from omniscience_retrieval.graph_query import (
    EdgeResult,
    EntityNode,
    GraphQueryService,
    GraphResult,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _node_to_view(node: EntityNode) -> EntityNodeView:
    return EntityNodeView(
        name=node.name,
        kind=node.kind,
        source=node.source,
        chunk_text=node.chunk_text,
        depth=node.depth,
        edge_type=node.edge_type,
    )


def _edge_to_view(edge: EdgeResult) -> GraphEdgeView:
    return GraphEdgeView(
        from_entity=edge.from_entity,
        to_entity=edge.to_entity,
        edge_type=edge.edge_type,
    )


def _result_to_view(result: GraphResult) -> GraphResultView:
    return GraphResultView(
        seed=_node_to_view(result.seed),
        related=[_node_to_view(n) for n in result.related],
        edges=[_edge_to_view(e) for e in result.edges],
    )


class GraphQueryProxy(GraphStore):
    """Adapter: ``GraphStore`` protocol -> ``GraphQueryService`` calls.

    Write methods (``upsert_entity`` / ``upsert_edge`` / ``upsert_graph``)
    are intentionally minimal — they raise ``NotImplementedError`` because
    no production test exercises them through this proxy.  The REST/MCP
    tests stub the read path via ``patch(...GraphQueryService.get_related)``.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._query_service = GraphQueryService(session_factory)

    async def get_entity(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> EntityNodeView | None:
        # ADR-0008 §5 / issue #132 — the proxy has no notion of versions
        # because GraphQueryService is the pre-bitemporal SQL backend.
        # We accept the parameter to match the protocol but reject any
        # non-None value rather than silently returning latest, which
        # would lie to a caller that explicitly asked for a point in time.
        if as_of is not None:
            raise NotImplementedError(
                "GraphQueryProxy does not support as_of reads (issue #132); "
                "use the Neo4j adapter for bitemporal queries."
            )
        try:
            result = await self._query_service.get_related(
                entity_name=entity_name,
                workspace_id=workspace_id,
                max_depth=1,
            )
        except ValueError as exc:
            if str(exc).startswith("entity_not_found"):
                return None
            raise
        return _node_to_view(result.seed)

    async def find_related(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        if as_of is not None:
            raise NotImplementedError(
                "GraphQueryProxy does not support as_of reads (issue #132); "
                "use the Neo4j adapter for bitemporal queries."
            )
        result = await self._query_service.get_related(
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
        as_of: datetime | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        return await self.find_related(
            entity_name=entity_name,
            workspace_id=workspace_id,
            as_of=as_of,
            max_depth=max_depth,
            edge_types=edge_types,
        )

    async def upsert_entity(
        self,
        *,
        entity: EntityUpsert,
        workspace_id: uuid.UUID,
    ) -> None:
        raise NotImplementedError(
            "GraphQueryProxy.upsert_entity is not implemented; use the Neo4j "
            "store directly in integration tests."
        )

    async def upsert_edge(
        self,
        *,
        edge: EdgeUpsert,
        workspace_id: uuid.UUID,
    ) -> None:
        raise NotImplementedError(
            "GraphQueryProxy.upsert_edge is not implemented; use the Neo4j "
            "store directly in integration tests."
        )

    async def upsert_graph(
        self,
        *,
        source_id: uuid.UUID,
        document_id: uuid.UUID,
        entities: list[EntityUpsert],
        edges: list[EdgeUpsert],
    ) -> None:
        raise NotImplementedError(
            "GraphQueryProxy.upsert_graph is not implemented; use the Neo4j "
            "store directly in integration tests."
        )

    async def delete_tombstoned(self) -> int:
        return 0


__all__ = ["GraphQueryProxy"]
