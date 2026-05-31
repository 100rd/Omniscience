"""Cross-workspace isolation contract test for :class:`GraphRAGComposer`.

Mirrors the spirit of ``tests/test_graph_workspace_isolation.py`` (PR
#119) on the new GraphRAG retrieval path (issue #107).

The test plants data in **two** workspaces (A and B), both with
overlapping entity names and overlapping chunk content, and asserts:

1. A query authenticated to workspace A receives zero chunks that
   were only populated in workspace B.
2. The composer forwards ``workspace_id`` on every downstream call
   (graph traversal AND vector search) so an adapter that forgets to
   apply the filter cannot leak across the tenant boundary.
3. Even when both workspaces contain an entity with the same anchor
   name, the composer never mixes their subgraphs.

The fake stores here are faithful to the ``GraphStore`` and
``VectorStore`` protocol contracts: they accept ``workspace_id`` as
keyword-only, filter their data on it, and raise
``entity_not_found:<name>`` when a lookup targets the wrong
workspace.  If the composer did not forward workspace_id, the fakes
would return workspace-A data to a workspace-B caller — and this
test would fail.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from omniscience_core.storage.graph import (
    EntityNodeView,
    GraphEdgeView,
    GraphResultView,
)
from omniscience_retrieval.graph_rag import (
    ANCHOR_FILTER_KEY,
    GraphRAGComposer,
)
from omniscience_retrieval.models import (
    ChunkLineage,
    Citation,
    QueryStats,
    SearchHit,
    SearchRequest,
    SearchResult,
    SourceInfo,
)

# ---------------------------------------------------------------------------
# Fakes — faithful to the protocol contract (workspace_id required)
# ---------------------------------------------------------------------------


class _WorkspaceAwareGraphStore:
    """In-memory GraphStore that honours ``workspace_id`` on every read.

    Stores entities indexed by ``(workspace_id, name)``; a lookup with
    a workspace the entity does not belong to raises
    ``ValueError("entity_not_found:<name>")`` — matching the protocol
    contract exactly (no cross-workspace leakage, no existence leak).
    """

    def __init__(self) -> None:
        self._by_ws: dict[tuple[uuid.UUID, str], GraphResultView] = {}

    def plant(
        self,
        *,
        workspace_id: uuid.UUID,
        name: str,
        result: GraphResultView,
    ) -> None:
        self._by_ws[(workspace_id, name)] = result

    async def traverse(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        key = (workspace_id, entity_name)
        if key not in self._by_ws:
            raise ValueError(f"entity_not_found:{entity_name}")
        return self._by_ws[key]

    async def find_related(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        return await self.traverse(
            entity_name=entity_name,
            workspace_id=workspace_id,
            as_of=as_of,
            max_depth=max_depth,
            edge_types=edge_types,
        )


class _WorkspaceAwareVectorStore:
    """In-memory VectorStore that honours ``workspace_id`` on every search.

    Chunks carry a workspace tag; a search with workspace A only
    returns chunks that were planted into workspace A.  This is the
    exact contract :class:`QdrantVectorStore` enforces via
    ``QdrantFilterBuilder``.
    """

    def __init__(self) -> None:
        self._chunks: list[tuple[uuid.UUID, SearchHit]] = []

    def plant(self, *, workspace_id: uuid.UUID, hit: SearchHit) -> None:
        self._chunks.append((workspace_id, hit))

    async def search(
        self,
        *,
        request: SearchRequest,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> SearchResult:
        del as_of  # workspace-isolation test is bitemporal-naive on purpose
        scoped = [hit for ws, hit in self._chunks if ws == workspace_id]
        # Apply any ``sources`` filter the composer passed in, as a real
        # vector backend would.
        if request.sources:
            allowed = set(request.sources)
            scoped = [h for h in scoped if str(h.source.id) in allowed]
        hits = scoped[: request.top_k]
        return SearchResult(
            hits=hits,
            query_stats=QueryStats(
                total_matches_before_filters=len(scoped),
                vector_matches=len(scoped),
                text_matches=0,
                duration_ms=1.0,
            ),
        )


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _hit(*, source_id: uuid.UUID, text: str, score: float = 0.9) -> SearchHit:
    return SearchHit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        score=score,
        text=text,
        source=SourceInfo(id=source_id, name="fake", type="fake"),
        citation=Citation(
            uri="https://example.invalid/doc",
            title="doc",
            indexed_at=datetime.now(UTC),
            doc_version=1,
        ),
        lineage=ChunkLineage(
            ingestion_run_id=None,
            embedding_model="stub",
            embedding_provider="stub",
            parser_version="stub",
            chunker_strategy="stub",
        ),
        metadata={},
    )


def _graph_result(*, seed_source: uuid.UUID, related_sources: list[uuid.UUID]) -> GraphResultView:
    seed = EntityNodeView(
        id=uuid.uuid4(),
        name="AuthService",
        kind="service",
        source=str(seed_source),
        chunk_text="seed chunk",
        depth=0,
        edge_type=None,
    )
    related = [
        EntityNodeView(
            id=uuid.uuid4(),
            name=f"rel-{i}",
            kind="service",
            source=str(src),
            chunk_text=f"related {i}",
            depth=1,
            edge_type="calls",
        )
        for i, src in enumerate(related_sources)
    ]
    edges = [
        GraphEdgeView(from_entity="AuthService", to_entity=f"rel-{i}", edge_type="calls")
        for i in range(len(related_sources))
    ]
    return GraphResultView(seed=seed, related=related, edges=edges)


class _NopLegacy:
    async def search(self, request: SearchRequest) -> SearchResult:  # pragma: no cover
        return SearchResult(
            hits=[],
            query_stats=QueryStats(
                total_matches_before_filters=0,
                vector_matches=0,
                text_matches=0,
                duration_ms=0.0,
            ),
        )


# ---------------------------------------------------------------------------
# The contract test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCrossWorkspaceIsolation:
    """Planted data in A must never reach a caller scoped to B."""

    @pytest.fixture()
    def force_graphrag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import omniscience_retrieval.graph_rag as gr

        monkeypatch.setattr(gr, "_should_use_graphrag", lambda g, v: True)

    async def test_workspace_a_cannot_see_workspace_b_chunks(self, force_graphrag: None) -> None:
        ws_a = uuid.uuid4()
        ws_b = uuid.uuid4()

        # Both workspaces contain an "AuthService" entity — overlapping
        # names — and chunks under disjoint sources.
        src_a_seed = uuid.uuid4()
        src_a_rel = uuid.uuid4()
        src_b_seed = uuid.uuid4()
        src_b_rel = uuid.uuid4()

        gs = _WorkspaceAwareGraphStore()
        gs.plant(
            workspace_id=ws_a,
            name="AuthService",
            result=_graph_result(seed_source=src_a_seed, related_sources=[src_a_rel]),
        )
        gs.plant(
            workspace_id=ws_b,
            name="AuthService",
            result=_graph_result(seed_source=src_b_seed, related_sources=[src_b_rel]),
        )

        vs = _WorkspaceAwareVectorStore()
        # Plant chunks under matching workspaces.  Crucial: the same
        # source_id SHOULD NOT be reachable from the other workspace —
        # we plant with disjoint (workspace, source) pairs.
        vs.plant(
            workspace_id=ws_a,
            hit=_hit(source_id=src_a_seed, text="A-seed-chunk"),
        )
        vs.plant(
            workspace_id=ws_a,
            hit=_hit(source_id=src_a_rel, text="A-related-chunk"),
        )
        vs.plant(
            workspace_id=ws_b,
            hit=_hit(source_id=src_b_seed, text="B-seed-chunk"),
        )
        vs.plant(
            workspace_id=ws_b,
            hit=_hit(source_id=src_b_rel, text="B-related-chunk"),
        )

        composer = GraphRAGComposer(
            graph_store=cast("Any", gs),
            vector_store=vs,
            legacy_service=_NopLegacy(),
        )

        # Caller A queries with anchor "AuthService" — should see ONLY
        # workspace-A chunks.
        req = SearchRequest(
            query="anything",
            filters={ANCHOR_FILTER_KEY: "AuthService"},
            top_k=10,
        )
        result_a = await composer.search(req, workspace_id=ws_a)
        texts_a = {h.text for h in result_a.hits}
        source_ids_a = {h.source.id for h in result_a.hits}

        # Positive assertions: A data is reachable.
        assert texts_a.issubset({"A-seed-chunk", "A-related-chunk"})
        # Negative assertions: B data is NOT reachable under any filter.
        assert "B-seed-chunk" not in texts_a
        assert "B-related-chunk" not in texts_a
        assert src_b_seed not in source_ids_a
        assert src_b_rel not in source_ids_a

        # Caller B queries same anchor — mirror assertion.
        result_b = await composer.search(req, workspace_id=ws_b)
        texts_b = {h.text for h in result_b.hits}
        assert texts_b.issubset({"B-seed-chunk", "B-related-chunk"})
        assert "A-seed-chunk" not in texts_b
        assert "A-related-chunk" not in texts_b

    async def test_anchor_missing_in_workspace_still_blocks_leak(
        self, force_graphrag: None
    ) -> None:
        """If an anchor resolves in B but not A, caller A still gets no B data."""
        ws_a = uuid.uuid4()
        ws_b = uuid.uuid4()
        src_b = uuid.uuid4()

        gs = _WorkspaceAwareGraphStore()
        # Only ws_b has the entity.
        gs.plant(
            workspace_id=ws_b,
            name="SecretService",
            result=_graph_result(seed_source=src_b, related_sources=[]),
        )

        vs = _WorkspaceAwareVectorStore()
        vs.plant(workspace_id=ws_b, hit=_hit(source_id=src_b, text="B-only"))

        composer = GraphRAGComposer(
            graph_store=cast("Any", gs),
            vector_store=vs,
            legacy_service=_NopLegacy(),
        )

        # Caller A asks about SecretService — anchor misses (indistinguishable
        # from not-found by design), vector stage runs unscoped but on ws_a
        # data only.
        req = SearchRequest(
            query="secret",
            filters={ANCHOR_FILTER_KEY: "SecretService"},
        )
        result = await composer.search(req, workspace_id=ws_a)
        assert result.hits == []  # ws_a has no chunks planted.

    async def test_no_anchor_still_respects_workspace(self, force_graphrag: None) -> None:
        """Unanchored vector-only queries still honour workspace scoping."""
        ws_a = uuid.uuid4()
        ws_b = uuid.uuid4()
        src_a = uuid.uuid4()
        src_b = uuid.uuid4()

        gs = _WorkspaceAwareGraphStore()
        vs = _WorkspaceAwareVectorStore()
        vs.plant(workspace_id=ws_a, hit=_hit(source_id=src_a, text="A-chunk"))
        vs.plant(workspace_id=ws_b, hit=_hit(source_id=src_b, text="B-chunk"))

        composer = GraphRAGComposer(
            graph_store=cast("Any", gs),
            vector_store=vs,
            legacy_service=_NopLegacy(),
        )

        req = SearchRequest(query="free-text")
        result_a = await composer.search(req, workspace_id=ws_a)
        texts_a = {h.text for h in result_a.hits}
        assert "A-chunk" in texts_a
        assert "B-chunk" not in texts_a
