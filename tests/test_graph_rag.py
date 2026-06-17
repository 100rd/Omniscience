"""Unit + contract tests for :class:`GraphRAGComposer` (issue #107).

Covers:

- Dispatch: legacy path when the backend pair is anything other than
  Neo4j+Qdrant; GraphRAG path when both are the new stack.
- Query without anchor, query with anchor, anchor-missing (graph
  returns empty), empty graph result, empty vector result.
- Cross-workspace filter pass-through: the composer forwards
  ``workspace_id`` to every downstream call; a full isolation
  contract test lives in ``test_graphrag_workspace_isolation.py``.
- Dedup when the vector adapter returns duplicate chunk IDs.
- Telemetry: Prometheus counters increment on the right labels, and
  stage-duration histograms are observed for every composed query.

All tests use in-process mocks (no containers) — the contract is
expressed on the ``GraphStore`` / ``VectorStore`` protocols so mocks
are faithful.
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
    CANDIDATE_EXPANSION_FACTOR,
    MAX_ANCHOR_CANDIDATES,
    GraphRAGComposer,
    _build_affinity_map,
    _collect_candidates,
    _extract_anchor,
    _linear_blend,
    _should_use_graphrag,
    _widen_request,
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
from prometheus_client import CollectorRegistry, generate_latest

# ---------------------------------------------------------------------------
# Fakes — minimal GraphStore / VectorStore / legacy service
# ---------------------------------------------------------------------------


class _FakeGraphStore:
    """In-memory GraphStore fake that records calls and returns fixtures."""

    def __init__(
        self,
        *,
        result: GraphResultView | None = None,
        raise_not_found: bool = False,
    ) -> None:
        self._result = result
        self._raise = raise_not_found
        self.traverse_calls: list[dict[str, Any]] = []

    async def traverse(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        self.traverse_calls.append(
            {
                "entity_name": entity_name,
                "workspace_id": workspace_id,
                "as_of": as_of,
                "max_depth": max_depth,
                "edge_types": edge_types,
            }
        )
        if self._raise:
            raise ValueError(f"entity_not_found:{entity_name}")
        if self._result is None:
            raise AssertionError("no_result_configured")
        return self._result

    # Unused by the composer but required by the protocol contract.
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


class _FakeVectorStore:
    """In-memory VectorStore fake (protocol-compatible search)."""

    def __init__(self, result: SearchResult) -> None:
        self._result = result
        self.search_calls: list[dict[str, Any]] = []

    async def search(
        self,
        *,
        request: SearchRequest,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> SearchResult:
        self.search_calls.append(
            {"request": request, "workspace_id": workspace_id, "as_of": as_of}
        )
        return self._result


class _FakeLegacyService:
    """Legacy-path stub that records calls and returns a configured result."""

    def __init__(self, result: SearchResult) -> None:
        self._result = result
        self.search_calls: list[SearchRequest] = []

    async def search(self, request: SearchRequest) -> SearchResult:
        self.search_calls.append(request)
        return self._result


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_hit(
    *,
    chunk_id: uuid.UUID | None = None,
    source_id: uuid.UUID | None = None,
    score: float = 1.0,
    text: str = "chunk text",
) -> SearchHit:
    """Minimal valid :class:`SearchHit`."""
    return SearchHit(
        chunk_id=chunk_id or uuid.uuid4(),
        document_id=uuid.uuid4(),
        score=score,
        text=text,
        source=SourceInfo(
            id=source_id or uuid.uuid4(),
            name="fake-source",
            type="fake",
        ),
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


def _make_result(hits: list[SearchHit]) -> SearchResult:
    return SearchResult(
        hits=hits,
        query_stats=QueryStats(
            total_matches_before_filters=len(hits),
            vector_matches=len(hits),
            text_matches=0,
            duration_ms=1.0,
        ),
    )


def _make_graph_result(
    *,
    seed_source: uuid.UUID,
    related: list[tuple[uuid.UUID, int]],
) -> GraphResultView:
    """Build a ``GraphResultView`` with given seed/related source ids."""
    seed = EntityNodeView(
        id=uuid.uuid4(),
        name="seed-entity",
        kind="service",
        source=str(seed_source),
        chunk_text="seed chunk",
        depth=0,
        edge_type=None,
    )
    related_nodes = [
        EntityNodeView(
            id=uuid.uuid4(),
            name=f"rel-{i}",
            kind="service",
            source=str(src),
            chunk_text=f"related chunk {i}",
            depth=depth,
            edge_type="calls",
        )
        for i, (src, depth) in enumerate(related)
    ]
    edges = [
        GraphEdgeView(from_entity="seed-entity", to_entity=f"rel-{i}", edge_type="calls")
        for i in range(len(related))
    ]
    return GraphResultView(seed=seed, related=related_nodes, edges=edges)


# ---------------------------------------------------------------------------
# Pure-helper tests — no composer, no async
# ---------------------------------------------------------------------------


class TestExtractAnchor:
    """Unit tests for :func:`_extract_anchor`."""

    def test_returns_empty_when_filters_none(self) -> None:
        req = SearchRequest(query="q")
        assert _extract_anchor(req) == ""

    def test_returns_empty_when_filters_missing_key(self) -> None:
        req = SearchRequest(query="q", filters={"other": "thing"})
        assert _extract_anchor(req) == ""

    def test_returns_value_when_present(self) -> None:
        req = SearchRequest(query="q", filters={ANCHOR_FILTER_KEY: "AuthService"})
        assert _extract_anchor(req) == "AuthService"

    def test_strips_whitespace(self) -> None:
        req = SearchRequest(query="q", filters={ANCHOR_FILTER_KEY: "  AuthService  "})
        assert _extract_anchor(req) == "AuthService"

    def test_ignores_non_string_values(self) -> None:
        req = SearchRequest(query="q", filters={ANCHOR_FILTER_KEY: 42})
        assert _extract_anchor(req) == ""


class TestLinearBlend:
    """Unit tests for :func:`_linear_blend`."""

    def test_pure_vector_when_affinity_zero(self) -> None:
        assert _linear_blend(vector_score=0.8, graph_affinity=0.0) == pytest.approx(0.8 * 0.75)

    def test_affinity_boost(self) -> None:
        # With MERGE_ALPHA=0.25: 0.75 * 1.0 + 0.25 * 1.0 = 1.0
        assert _linear_blend(vector_score=1.0, graph_affinity=1.0) == pytest.approx(1.0)


class TestCollectCandidates:
    """Unit tests for :func:`_collect_candidates`."""

    def test_seed_counts_as_depth_zero(self) -> None:
        seed_src = uuid.uuid4()
        result = _make_graph_result(seed_source=seed_src, related=[])
        ids, depths = _collect_candidates(result)
        assert ids == (str(seed_src),)
        assert depths == (0,)

    def test_dedupes_duplicate_sources(self) -> None:
        seed_src = uuid.uuid4()
        other = uuid.uuid4()
        result = _make_graph_result(
            seed_source=seed_src,
            related=[(other, 1), (other, 2), (seed_src, 1)],
        )
        ids, _ = _collect_candidates(result)
        # seed + 1 unique related (second is duplicate of first, third duplicates seed)
        assert len(ids) == 2
        assert ids[0] == str(seed_src)
        assert ids[1] == str(other)

    def test_caps_at_max_anchor_candidates(self) -> None:
        seed_src = uuid.uuid4()
        related = [(uuid.uuid4(), 1) for _ in range(MAX_ANCHOR_CANDIDATES + 10)]
        result = _make_graph_result(seed_source=seed_src, related=related)
        ids, _ = _collect_candidates(result)
        assert len(ids) == MAX_ANCHOR_CANDIDATES

    def test_prioritize_by_edge_weight(self) -> None:
        seed_src = uuid.uuid4()
        src_a = uuid.uuid4()
        src_b = uuid.uuid4()
        src_c = uuid.uuid4()

        seed = EntityNodeView(
            id=uuid.uuid4(),
            name="seed",
            kind="service",
            source=str(seed_src),
            chunk_text="seed chunk",
            depth=0,
            edge_type=None,
        )
        related = [
            EntityNodeView(
                id=uuid.uuid4(),
                name="b",
                kind="service",
                source=str(src_b),
                chunk_text="b chunk",
                depth=1,
                edge_type="OWNED_BY",  # weight 0.4
            ),
            EntityNodeView(
                id=uuid.uuid4(),
                name="a",
                kind="service",
                source=str(src_a),
                chunk_text="a chunk",
                depth=1,
                edge_type="DEPENDS_ON",  # weight 1.0
            ),
            EntityNodeView(
                id=uuid.uuid4(),
                name="c",
                kind="service",
                source=str(src_c),
                chunk_text="c chunk",
                depth=1,
                edge_type="ROUTES_TO",  # weight 0.9
            ),
        ]
        result = GraphResultView(seed=seed, related=related, edges=[])
        ids, depths = _collect_candidates(result)

        # Expected sorted order: seed_src (always index 0), src_a (1.0), src_c (0.9), src_b (0.4)
        assert ids == (str(seed_src), str(src_a), str(src_c), str(src_b))
        assert depths == (0, 1, 1, 1)

    def test_prioritize_by_depth_decay(self) -> None:
        seed_src = uuid.uuid4()
        src_a = uuid.uuid4()
        src_b = uuid.uuid4()

        seed = EntityNodeView(
            id=uuid.uuid4(),
            name="seed",
            kind="service",
            source=str(seed_src),
            chunk_text="seed chunk",
            depth=0,
            edge_type=None,
        )
        related = [
            EntityNodeView(
                id=uuid.uuid4(),
                name="a",
                kind="service",
                source=str(src_a),
                chunk_text="a chunk",
                depth=3,
                edge_type="DEPENDS_ON",  # weight 1.0, decay 0.25 -> score 0.25, min depth 3
            ),
            EntityNodeView(
                id=uuid.uuid4(),
                name="b",
                kind="service",
                source=str(src_b),
                chunk_text="b chunk",
                depth=1,
                edge_type="OWNED_BY",  # weight 0.4, decay 1.0 -> score 0.4, min depth 1
            ),
        ]
        result = GraphResultView(seed=seed, related=related, edges=[])
        ids, depths = _collect_candidates(result)

        assert ids == (str(seed_src), str(src_b), str(src_a))
        assert depths == (0, 1, 3)

    def test_tie_breaking_bfs_order(self) -> None:
        seed_src = uuid.uuid4()
        src_a = uuid.uuid4()
        src_b = uuid.uuid4()

        seed = EntityNodeView(
            id=uuid.uuid4(),
            name="seed",
            kind="service",
            source=str(seed_src),
            chunk_text="seed chunk",
            depth=0,
            edge_type=None,
        )

        # Swapping order to verify BFS order governs tie-breaking (same score = 1.0)
        # Scenario 1: A then B
        related_1 = [
            EntityNodeView(
                id=uuid.uuid4(),
                name="a",
                kind="service",
                source=str(src_a),
                chunk_text="a",
                depth=1,
                edge_type="CALLS",
            ),
            EntityNodeView(
                id=uuid.uuid4(),
                name="b",
                kind="service",
                source=str(src_b),
                chunk_text="b",
                depth=1,
                edge_type="DEPENDS_ON",
            ),
        ]
        result_1 = GraphResultView(seed=seed, related=related_1, edges=[])
        ids_1, _ = _collect_candidates(result_1)
        assert ids_1 == (str(seed_src), str(src_a), str(src_b))

        # Scenario 2: B then A
        related_2 = [
            EntityNodeView(
                id=uuid.uuid4(),
                name="b",
                kind="service",
                source=str(src_b),
                chunk_text="b",
                depth=1,
                edge_type="DEPENDS_ON",
            ),
            EntityNodeView(
                id=uuid.uuid4(),
                name="a",
                kind="service",
                source=str(src_a),
                chunk_text="a",
                depth=1,
                edge_type="CALLS",
            ),
        ]
        result_2 = GraphResultView(seed=seed, related=related_2, edges=[])
        ids_2, _ = _collect_candidates(result_2)
        assert ids_2 == (str(seed_src), str(src_b), str(src_a))


class TestBuildAffinityMap:
    """Unit tests for :func:`_build_affinity_map`."""

    def test_decays_by_depth(self) -> None:
        from omniscience_retrieval.graph_rag import _AnchorStageResult

        anchor = _AnchorStageResult(
            anchor_requested=True,
            anchor_name="x",
            anchor_hit=True,
            candidate_source_ids=("A", "B", "C"),
            candidate_depths=(0, 1, 2),
            duration_s=0.0,
        )
        m = _build_affinity_map(anchor)
        # depth 0 and 1 both map to base (1.0); depth 2 halves to 0.5.
        assert m["A"] == 1.0
        assert m["B"] == 1.0
        assert m["C"] == 0.5


# ---------------------------------------------------------------------------
# Dispatch tests
# ---------------------------------------------------------------------------


class TestDispatch:
    """Ensure legacy fallback runs when the backend pair is not Neo4j+Qdrant."""

    def test_legacy_when_pgvector_stack(self) -> None:
        # Fake objects that are NOT Neo4jGraphStore/QdrantVectorStore.
        g = _FakeGraphStore()
        v = _FakeVectorStore(_make_result([]))
        assert _should_use_graphrag(cast("Any", g), v) is False

    def test_composer_property_reflects_dispatch(self) -> None:
        g = _FakeGraphStore()
        v = _FakeVectorStore(_make_result([]))
        legacy = _FakeLegacyService(_make_result([]))
        composer = GraphRAGComposer(
            graph_store=cast("Any", g),
            vector_store=v,
            legacy_service=legacy,
        )
        assert composer.graphrag_active is False


# ---------------------------------------------------------------------------
# Composer behaviour tests (legacy fallback path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestLegacyFallback:
    """When dispatch says "legacy", the composer forwards unchanged."""

    async def test_forwards_request_to_legacy(self) -> None:
        g = _FakeGraphStore()
        v = _FakeVectorStore(_make_result([]))
        legacy_result = _make_result([_make_hit(score=0.9)])
        legacy = _FakeLegacyService(legacy_result)
        composer = GraphRAGComposer(
            graph_store=cast("Any", g),
            vector_store=v,
            legacy_service=legacy,
        )
        req = SearchRequest(query="hello")
        ws = uuid.uuid4()
        result = await composer.search(req, workspace_id=ws)
        # Issue #133: composer always stamps ``effective_as_of`` into
        # the response envelope (response generation time when as_of
        # is None).  The hits and query_stats round-trip unchanged.
        assert result.hits == legacy_result.hits
        assert result.query_stats == legacy_result.query_stats
        assert result.effective_as_of is not None
        assert result.meta is None
        assert len(legacy.search_calls) == 1
        assert len(g.traverse_calls) == 0
        assert len(v.search_calls) == 0


# ---------------------------------------------------------------------------
# Composer behaviour tests (GraphRAG path — forced via monkeypatch)
# ---------------------------------------------------------------------------


@pytest.fixture()
def force_graphrag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the dispatch to treat any store pair as the Neo4j+Qdrant stack.

    Patches :func:`_should_use_graphrag` so the composer runs its
    staged pipeline against the in-memory fakes.
    """
    import omniscience_retrieval.graph_rag as gr

    monkeypatch.setattr(gr, "_should_use_graphrag", lambda g, v: True)


@pytest.mark.asyncio
class TestComposedPath:
    """Behavioural tests when the composer runs its staged pipeline."""

    async def test_no_anchor_runs_vector_only(self, force_graphrag: None) -> None:
        g = _FakeGraphStore()  # never called when no anchor
        hit = _make_hit(score=0.9)
        v = _FakeVectorStore(_make_result([hit]))
        legacy = _FakeLegacyService(_make_result([]))
        composer = GraphRAGComposer(
            graph_store=cast("Any", g), vector_store=v, legacy_service=legacy
        )
        req = SearchRequest(query="no-anchor")
        ws = uuid.uuid4()
        result = await composer.search(req, workspace_id=ws)
        assert len(g.traverse_calls) == 0
        assert len(v.search_calls) == 1
        # Workspace forwarded.
        assert v.search_calls[0]["workspace_id"] == ws
        # top_k widened for the vector stage.
        called_req: SearchRequest = v.search_calls[0]["request"]
        assert called_req.top_k == req.top_k * CANDIDATE_EXPANSION_FACTOR
        # Result sliced back to requested top_k.
        assert len(result.hits) == 1

    async def test_anchor_hit_scopes_vector_search(self, force_graphrag: None) -> None:
        seed_src = uuid.uuid4()
        other_src = uuid.uuid4()
        g = _FakeGraphStore(
            result=_make_graph_result(seed_source=seed_src, related=[(other_src, 1)])
        )
        # Return a hit whose source is in the candidate set.
        hit = _make_hit(source_id=seed_src, score=0.5)
        v = _FakeVectorStore(_make_result([hit]))
        legacy = _FakeLegacyService(_make_result([]))
        composer = GraphRAGComposer(
            graph_store=cast("Any", g), vector_store=v, legacy_service=legacy
        )
        req = SearchRequest(
            query="with-anchor",
            filters={ANCHOR_FILTER_KEY: "AuthService"},
        )
        ws = uuid.uuid4()
        await composer.search(req, workspace_id=ws)
        # Graph traversal ran with the anchor + workspace.
        assert len(g.traverse_calls) == 1
        assert g.traverse_calls[0]["entity_name"] == "AuthService"
        assert g.traverse_calls[0]["workspace_id"] == ws
        # Vector search got the candidate source IDs as ``sources``.
        v_req: SearchRequest = v.search_calls[0]["request"]
        assert v_req.sources is not None
        assert str(seed_src) in v_req.sources
        assert str(other_src) in v_req.sources
        # Anchor hint stripped before forwarding.
        assert v_req.filters is None or ANCHOR_FILTER_KEY not in v_req.filters

    async def test_anchor_miss_runs_unscoped_vector(self, force_graphrag: None) -> None:
        g = _FakeGraphStore(raise_not_found=True)
        v = _FakeVectorStore(_make_result([_make_hit(score=0.7)]))
        legacy = _FakeLegacyService(_make_result([]))
        composer = GraphRAGComposer(
            graph_store=cast("Any", g), vector_store=v, legacy_service=legacy
        )
        req = SearchRequest(
            query="anchor-missing",
            filters={ANCHOR_FILTER_KEY: "DoesNotExist"},
        )
        ws = uuid.uuid4()
        result = await composer.search(req, workspace_id=ws)
        # Graph attempted, raised, swallowed.
        assert len(g.traverse_calls) == 1
        # Vector still ran (with no source scoping).
        v_req: SearchRequest = v.search_calls[0]["request"]
        assert v_req.sources is None
        assert len(result.hits) == 1

    async def test_empty_vector_result_returns_empty(self, force_graphrag: None) -> None:
        g = _FakeGraphStore(result=_make_graph_result(seed_source=uuid.uuid4(), related=[]))
        v = _FakeVectorStore(_make_result([]))
        legacy = _FakeLegacyService(_make_result([]))
        composer = GraphRAGComposer(
            graph_store=cast("Any", g), vector_store=v, legacy_service=legacy
        )
        req = SearchRequest(query="empty", filters={ANCHOR_FILTER_KEY: "X"})
        result = await composer.search(req, workspace_id=uuid.uuid4())
        assert result.hits == []

    async def test_dedupes_duplicate_chunk_ids(self, force_graphrag: None) -> None:
        chunk_id = uuid.uuid4()
        dup_a = _make_hit(chunk_id=chunk_id, score=0.9)
        dup_b = _make_hit(chunk_id=chunk_id, score=0.5)
        other = _make_hit(score=0.4)
        v = _FakeVectorStore(_make_result([dup_a, dup_b, other]))
        g = _FakeGraphStore()
        legacy = _FakeLegacyService(_make_result([]))
        composer = GraphRAGComposer(
            graph_store=cast("Any", g), vector_store=v, legacy_service=legacy
        )
        req = SearchRequest(query="dedup", top_k=10)
        result = await composer.search(req, workspace_id=uuid.uuid4())
        # Two unique chunks survive.
        ids = {h.chunk_id for h in result.hits}
        assert len(ids) == 2
        assert chunk_id in ids

    async def test_anchor_boost_reorders_results(self, force_graphrag: None) -> None:
        """When anchor surfaces a source, hits from that source outrank stronger vector hits."""
        seed_src = uuid.uuid4()
        unrelated_src = uuid.uuid4()
        # Anchor graph includes seed_src only.
        g = _FakeGraphStore(result=_make_graph_result(seed_source=seed_src, related=[]))
        # Vector returns: (a) unrelated_src high score, (b) seed_src lower score.
        hit_unrelated = _make_hit(source_id=unrelated_src, score=0.9)
        hit_related = _make_hit(source_id=seed_src, score=0.85)
        v = _FakeVectorStore(_make_result([hit_unrelated, hit_related]))
        legacy = _FakeLegacyService(_make_result([]))
        composer = GraphRAGComposer(
            graph_store=cast("Any", g), vector_store=v, legacy_service=legacy
        )
        req = SearchRequest(
            query="boost-test",
            filters={ANCHOR_FILTER_KEY: "X"},
            top_k=2,
        )
        result = await composer.search(req, workspace_id=uuid.uuid4())
        # Related hit now outranks unrelated because of the graph affinity bonus.
        assert result.hits[0].source.id == seed_src


# ---------------------------------------------------------------------------
# Telemetry tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTelemetry:
    """Verify Prometheus metrics emit on the right labels."""

    async def test_metrics_exposed_via_generate_latest(self, force_graphrag: None) -> None:
        # Issue a composed query so the counters/histograms get touched.
        g = _FakeGraphStore(result=_make_graph_result(seed_source=uuid.uuid4(), related=[]))
        v = _FakeVectorStore(_make_result([_make_hit(score=0.5)]))
        legacy = _FakeLegacyService(_make_result([]))
        composer = GraphRAGComposer(
            graph_store=cast("Any", g), vector_store=v, legacy_service=legacy
        )
        req = SearchRequest(query="telem", filters={ANCHOR_FILTER_KEY: "X"})
        await composer.search(req, workspace_id=uuid.uuid4())
        exposition = generate_latest().decode()
        # All four metric families present.
        assert "omniscience_graphrag_stage_duration_seconds" in exposition
        assert "omniscience_graphrag_candidate_set_size" in exposition
        assert "omniscience_graphrag_anchor_total" in exposition
        assert "omniscience_graphrag_queries_total" in exposition

    async def test_anchor_outcome_labels(self, force_graphrag: None) -> None:
        from omniscience_retrieval.graph_rag import _ANCHOR_HIT_TOTAL

        # absent
        g1 = _FakeGraphStore()
        v = _FakeVectorStore(_make_result([]))
        legacy = _FakeLegacyService(_make_result([]))
        composer = GraphRAGComposer(
            graph_store=cast("Any", g1), vector_store=v, legacy_service=legacy
        )
        before_absent = _ANCHOR_HIT_TOTAL.labels(outcome="absent")._value.get()
        await composer.search(SearchRequest(query="a"), workspace_id=uuid.uuid4())
        assert _ANCHOR_HIT_TOTAL.labels(outcome="absent")._value.get() == before_absent + 1

        # miss
        g2 = _FakeGraphStore(raise_not_found=True)
        composer2 = GraphRAGComposer(
            graph_store=cast("Any", g2), vector_store=v, legacy_service=legacy
        )
        before_miss = _ANCHOR_HIT_TOTAL.labels(outcome="miss")._value.get()
        await composer2.search(
            SearchRequest(query="b", filters={ANCHOR_FILTER_KEY: "X"}),
            workspace_id=uuid.uuid4(),
        )
        assert _ANCHOR_HIT_TOTAL.labels(outcome="miss")._value.get() == before_miss + 1

        # hit
        g3 = _FakeGraphStore(result=_make_graph_result(seed_source=uuid.uuid4(), related=[]))
        composer3 = GraphRAGComposer(
            graph_store=cast("Any", g3), vector_store=v, legacy_service=legacy
        )
        before_hit = _ANCHOR_HIT_TOTAL.labels(outcome="hit")._value.get()
        await composer3.search(
            SearchRequest(query="c", filters={ANCHOR_FILTER_KEY: "Y"}),
            workspace_id=uuid.uuid4(),
        )
        assert _ANCHOR_HIT_TOTAL.labels(outcome="hit")._value.get() == before_hit + 1


# ---------------------------------------------------------------------------
# _widen_request test
# ---------------------------------------------------------------------------


class TestWidenRequest:
    """:func:`_widen_request` shapes the request forwarded to the vector store."""

    def test_preserves_original_sources_when_no_candidates(self) -> None:
        from omniscience_retrieval.graph_rag import _AnchorStageResult

        anchor = _AnchorStageResult(
            anchor_requested=False,
            anchor_name="",
            anchor_hit=False,
            candidate_source_ids=(),
            candidate_depths=(),
            duration_s=0.0,
        )
        req = SearchRequest(query="x", sources=["keep-me"])
        widened = _widen_request(req, anchor=anchor)
        assert widened.sources == ["keep-me"]

    def test_overrides_sources_with_candidates(self) -> None:
        from omniscience_retrieval.graph_rag import _AnchorStageResult

        anchor = _AnchorStageResult(
            anchor_requested=True,
            anchor_name="X",
            anchor_hit=True,
            candidate_source_ids=("A", "B"),
            candidate_depths=(0, 1),
            duration_s=0.0,
        )
        req = SearchRequest(query="x", sources=["original"])
        widened = _widen_request(req, anchor=anchor)
        assert widened.sources == ["A", "B"]

    def test_strips_anchor_filter(self) -> None:
        from omniscience_retrieval.graph_rag import _AnchorStageResult

        anchor = _AnchorStageResult(
            anchor_requested=False,
            anchor_name="",
            anchor_hit=False,
            candidate_source_ids=(),
            candidate_depths=(),
            duration_s=0.0,
        )
        req = SearchRequest(
            query="x",
            filters={ANCHOR_FILTER_KEY: "Foo", "keep": "bar"},
        )
        widened = _widen_request(req, anchor=anchor)
        assert widened.filters == {"keep": "bar"}


# ---------------------------------------------------------------------------
# Cross-store ``as_of`` propagation (issue #134)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAsOfThreading:
    """Both the graph step AND the vector step receive ``as_of``."""

    async def test_as_of_forwarded_to_vector_stage(self, force_graphrag: None) -> None:
        """Issue #134: composer threads ``as_of`` to ``VectorStore.search``.

        Pre-#134 only the graph step carried ``as_of``; the vector stage
        was unfiltered on time.  This test asserts the new contract on
        the wire — the composer calls ``vector_store.search(as_of=T)``.
        """
        seed = uuid.uuid4()
        g = _FakeGraphStore(result=_make_graph_result(seed_source=seed, related=[]))
        v = _FakeVectorStore(_make_result([_make_hit(source_id=seed, score=0.9)]))
        legacy = _FakeLegacyService(_make_result([]))
        composer = GraphRAGComposer(
            graph_store=cast("Any", g), vector_store=v, legacy_service=legacy
        )
        as_of_t1 = datetime(2026, 4, 12, 19, 25, 0, tzinfo=UTC)
        await composer.search(
            SearchRequest(query="cross-store", filters={ANCHOR_FILTER_KEY: "X"}),
            workspace_id=uuid.uuid4(),
            as_of=as_of_t1,
        )
        # Graph step received as_of (from #170).
        assert g.traverse_calls[0]["as_of"] == as_of_t1
        # Vector step also received as_of (NEW in #134).
        assert v.search_calls[0]["as_of"] == as_of_t1

    async def test_as_of_none_does_not_propagate_predicate(self, force_graphrag: None) -> None:
        """``as_of=None`` reaches the vector store as ``None`` — current-state read."""
        seed = uuid.uuid4()
        g = _FakeGraphStore(result=_make_graph_result(seed_source=seed, related=[]))
        v = _FakeVectorStore(_make_result([_make_hit(source_id=seed, score=0.5)]))
        legacy = _FakeLegacyService(_make_result([]))
        composer = GraphRAGComposer(
            graph_store=cast("Any", g), vector_store=v, legacy_service=legacy
        )
        await composer.search(
            SearchRequest(query="hot-path", filters={ANCHOR_FILTER_KEY: "X"}),
            workspace_id=uuid.uuid4(),
            as_of=None,
        )
        assert v.search_calls[0]["as_of"] is None

    async def test_as_of_from_request_is_threaded_to_vector(self, force_graphrag: None) -> None:
        """``request.as_of`` (no kw override) reaches the vector stage too."""
        seed = uuid.uuid4()
        g = _FakeGraphStore(result=_make_graph_result(seed_source=seed, related=[]))
        v = _FakeVectorStore(_make_result([_make_hit(source_id=seed, score=0.5)]))
        legacy = _FakeLegacyService(_make_result([]))
        composer = GraphRAGComposer(
            graph_store=cast("Any", g), vector_store=v, legacy_service=legacy
        )
        as_of_t1 = datetime(2026, 4, 12, 19, 25, 0, tzinfo=UTC)
        await composer.search(
            SearchRequest(
                query="from-request",
                filters={ANCHOR_FILTER_KEY: "X"},
                as_of=as_of_t1,
            ),
            workspace_id=uuid.uuid4(),
            # No keyword override — request.as_of is the source of truth.
        )
        assert v.search_calls[0]["as_of"] == as_of_t1

    async def test_keyword_as_of_overrides_request_as_of(self, force_graphrag: None) -> None:
        """The keyword ``as_of`` wins over ``request.as_of`` — explicit server override.

        Documented contract on :meth:`GraphRAGComposer.search`; this
        test pins it for the vector stage too so both halves of the
        cross-store consistency contract observe the same effective T.
        """
        seed = uuid.uuid4()
        g = _FakeGraphStore(result=_make_graph_result(seed_source=seed, related=[]))
        v = _FakeVectorStore(_make_result([_make_hit(source_id=seed, score=0.5)]))
        legacy = _FakeLegacyService(_make_result([]))
        composer = GraphRAGComposer(
            graph_store=cast("Any", g), vector_store=v, legacy_service=legacy
        )
        as_of_request = datetime(2026, 1, 1, tzinfo=UTC)
        as_of_kw = datetime(2026, 6, 1, tzinfo=UTC)
        await composer.search(
            SearchRequest(
                query="override",
                filters={ANCHOR_FILTER_KEY: "X"},
                as_of=as_of_request,
            ),
            workspace_id=uuid.uuid4(),
            as_of=as_of_kw,
        )
        # Both stages see the keyword value.
        assert g.traverse_calls[0]["as_of"] == as_of_kw
        assert v.search_calls[0]["as_of"] == as_of_kw

    async def test_cross_store_consistency_v3_entity_returns_v1_chunk(
        self, force_graphrag: None
    ) -> None:
        """A v3 graph entity at ``as_of=T1`` returns chunks linked to v1 state.

        The graph store's ``traverse(as_of=T1)`` returns the v1 entity
        (seed_source=v1_src).  The vector store, called with the same
        ``as_of=T1``, must filter to chunks valid at T1 — those chunks
        were planted with ``source_id=v1_src``.  Both stores agree on
        T1 → same source → cross-store consistent evidence.
        """
        v1_src = uuid.uuid4()  # the v1 entity's source at as_of=T1
        v3_src = uuid.uuid4()  # the v3 entity's source (current state)
        as_of_t1 = datetime(2026, 1, 15, tzinfo=UTC)

        # Graph @T1 -> v1 entity (source = v1_src).
        g = _FakeGraphStore(result=_make_graph_result(seed_source=v1_src, related=[]))
        # Vector simulator: returns chunks scoped to whatever ``sources``
        # the composer asked for — the request.sources comes from the
        # graph candidates, so a hit will have source_id == v1_src.  The
        # vector adapter ALSO receives as_of, which a real Qdrant adapter
        # would apply to its bitemporal filter; the fake captures it on
        # ``search_calls`` so the test can pin the propagation contract.
        hit_v1 = _make_hit(source_id=v1_src, score=0.9)
        v = _FakeVectorStore(_make_result([hit_v1]))
        legacy = _FakeLegacyService(_make_result([]))
        composer = GraphRAGComposer(
            graph_store=cast("Any", g), vector_store=v, legacy_service=legacy
        )
        result = await composer.search(
            SearchRequest(query="incident", filters={ANCHOR_FILTER_KEY: "Pod"}),
            workspace_id=uuid.uuid4(),
            as_of=as_of_t1,
        )
        # Both stores received the same as_of.
        assert g.traverse_calls[0]["as_of"] == as_of_t1
        assert v.search_calls[0]["as_of"] == as_of_t1
        # Graph candidates (sources=[v1_src]) flowed into the vector request.
        v_req: SearchRequest = v.search_calls[0]["request"]
        assert v_req.sources == [str(v1_src)]
        # The returned chunk's source matches the v1 (point-in-time) entity,
        # not v3 (current state).
        assert len(result.hits) == 1
        assert result.hits[0].source.id == v1_src
        # Sanity: v3_src is the "current" entity but is not in the result
        # because the graph filter at T1 returned v1 only.
        assert v3_src != v1_src
        assert all(h.source.id != v3_src for h in result.hits)


# Silence unused-import warning on Any (used only in annotations above).
_ = CollectorRegistry


# ---------------------------------------------------------------------------
# Stage 3: text_matches flow-through (fix/push-sources-and-graphrag-sparse)
# ---------------------------------------------------------------------------


def _make_result_with_text_matches(
    hits: list[SearchHit], *, text_matches: int = 0
) -> SearchResult:
    """Build a ``SearchResult`` with an explicit ``text_matches`` value.

    Used to verify that non-zero sparse-hit counts flow through the
    merge stage unchanged.
    """
    return SearchResult(
        hits=hits,
        query_stats=QueryStats(
            total_matches_before_filters=len(hits),
            vector_matches=max(len(hits) - text_matches, 0),
            text_matches=text_matches,
            duration_ms=1.0,
        ),
    )


class _StrategyCapturingVectorStore:
    """VectorStore fake that returns a configured result AND records the strategy used.

    Unlike ``_FakeVectorStore`` it returns a result whose ``text_matches``
    mirrors what a real RRF search would return — non-zero when the strategy
    is ``"hybrid"`` or ``"keyword"``, zero for ``"structural"``.  This lets
    the flow-through tests assert without mocking the strategy dispatch.
    """

    def __init__(
        self,
        hits: list[SearchHit],
        *,
        text_matches_per_strategy: dict[str, int] | None = None,
    ) -> None:
        self._hits = hits
        # default: hybrid/keyword → non-zero, structural → 0
        self._tm_map: dict[str, int] = text_matches_per_strategy or {
            "hybrid": len(hits),
            "auto": len(hits),
            "keyword": len(hits),
            "structural": 0,
        }
        self.search_calls: list[dict[str, Any]] = []

    async def search(
        self,
        *,
        request: SearchRequest,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> SearchResult:
        self.search_calls.append(
            {"request": request, "workspace_id": workspace_id, "as_of": as_of}
        )
        tm = self._tm_map.get(request.retrieval_strategy, 0)
        return _make_result_with_text_matches(self._hits, text_matches=tm)


@pytest.mark.asyncio
class TestHybridTextMatchesFlow:
    """Stage 3: verify text_matches from vector store flows through GraphRAG pipeline.

    The core regression: before Stage 3 QdrantVectorStore was dense-only and
    always returned text_matches=0.  After Stage 3 the store dispatches to
    _search_hybrid_rrf / _search_sparse which return real counts.  This class
    asserts that GraphRAGComposer does NOT zero-out text_matches on its way
    through the merge and stamp stages.

    ACL invariant: workspace_id is forwarded on every search call.
    """

    async def test_hybrid_strategy_text_matches_nonzero(self, force_graphrag: None) -> None:
        """hybrid strategy → real text_matches propagated to QueryStats."""
        hit = _make_hit(score=0.8)
        v = _StrategyCapturingVectorStore([hit])
        g = _FakeGraphStore()
        legacy = _FakeLegacyService(_make_result([]))
        composer = GraphRAGComposer(
            graph_store=cast("Any", g), vector_store=v, legacy_service=legacy
        )
        req = SearchRequest(query="nginx crash", retrieval_strategy="hybrid")
        result = await composer.search(req, workspace_id=uuid.uuid4())
        assert result.query_stats.text_matches == 1
        # Confirm the strategy was forwarded to the vector store.
        assert v.search_calls[0]["request"].retrieval_strategy == "hybrid"

    async def test_keyword_strategy_text_matches_nonzero(self, force_graphrag: None) -> None:
        """keyword strategy → text_matches equals hit count (sparse-only)."""
        hit = _make_hit(score=0.7)
        v = _StrategyCapturingVectorStore([hit])
        g = _FakeGraphStore()
        legacy = _FakeLegacyService(_make_result([]))
        composer = GraphRAGComposer(
            graph_store=cast("Any", g), vector_store=v, legacy_service=legacy
        )
        req = SearchRequest(query="OOMKilled", retrieval_strategy="keyword")
        result = await composer.search(req, workspace_id=uuid.uuid4())
        assert result.query_stats.text_matches == 1
        assert v.search_calls[0]["request"].retrieval_strategy == "keyword"

    async def test_structural_strategy_text_matches_zero(self, force_graphrag: None) -> None:
        """structural strategy → dense-only, text_matches stays 0."""
        hit = _make_hit(score=0.6)
        v = _StrategyCapturingVectorStore([hit])
        g = _FakeGraphStore()
        legacy = _FakeLegacyService(_make_result([]))
        composer = GraphRAGComposer(
            graph_store=cast("Any", g), vector_store=v, legacy_service=legacy
        )
        req = SearchRequest(query="k8s pod", retrieval_strategy="structural")
        result = await composer.search(req, workspace_id=uuid.uuid4())
        assert result.query_stats.text_matches == 0
        assert v.search_calls[0]["request"].retrieval_strategy == "structural"

    async def test_auto_strategy_text_matches_nonzero(self, force_graphrag: None) -> None:
        """auto strategy → treated as hybrid, text_matches is non-zero."""
        hit = _make_hit(score=0.75)
        v = _StrategyCapturingVectorStore([hit])
        g = _FakeGraphStore()
        legacy = _FakeLegacyService(_make_result([]))
        composer = GraphRAGComposer(
            graph_store=cast("Any", g), vector_store=v, legacy_service=legacy
        )
        req = SearchRequest(query="connection refused", retrieval_strategy="auto")
        result = await composer.search(req, workspace_id=uuid.uuid4())
        assert result.query_stats.text_matches == 1
        assert v.search_calls[0]["request"].retrieval_strategy == "auto"

    async def test_text_matches_preserved_after_graph_affinity_merge(
        self, force_graphrag: None
    ) -> None:
        """text_matches from vector stage survives the graph-affinity merge stage.

        The merge stage reorders hits and slices to top_k; it must NOT
        recalculate query_stats from scratch (which would zero text_matches).
        """
        seed_src = uuid.uuid4()
        g = _FakeGraphStore(result=_make_graph_result(seed_source=seed_src, related=[]))
        hit1 = _make_hit(source_id=seed_src, score=0.9)
        hit2 = _make_hit(score=0.7)
        v = _StrategyCapturingVectorStore([hit1, hit2])
        legacy = _FakeLegacyService(_make_result([]))
        composer = GraphRAGComposer(
            graph_store=cast("Any", g), vector_store=v, legacy_service=legacy
        )
        req = SearchRequest(
            query="service failure",
            retrieval_strategy="hybrid",
            filters={ANCHOR_FILTER_KEY: "AuthService"},
        )
        result = await composer.search(req, workspace_id=uuid.uuid4())
        # After merge (graph-affinity reorder) text_matches must still be 2.
        assert result.query_stats.text_matches == 2

    async def test_retrieval_strategy_forwarded_with_anchor(self, force_graphrag: None) -> None:
        """When anchor is present, retrieval_strategy still flows to vector store.

        Regression guard: _widen_request sets request.sources from the anchor
        candidates; it must NOT reset retrieval_strategy to 'structural' as an
        implicit side-effect of setting sources.
        """
        seed_src = uuid.uuid4()
        g = _FakeGraphStore(result=_make_graph_result(seed_source=seed_src, related=[]))
        hit = _make_hit(source_id=seed_src, score=0.8)
        v = _StrategyCapturingVectorStore([hit])
        legacy = _FakeLegacyService(_make_result([]))
        composer = GraphRAGComposer(
            graph_store=cast("Any", g), vector_store=v, legacy_service=legacy
        )
        req = SearchRequest(
            query="database timeout",
            retrieval_strategy="hybrid",
            filters={ANCHOR_FILTER_KEY: "DBService"},
        )
        ws = uuid.uuid4()
        result = await composer.search(req, workspace_id=ws)
        # Vector store received the anchor-scoped sources AND hybrid strategy.
        v_req: SearchRequest = v.search_calls[0]["request"]
        assert v_req.retrieval_strategy == "hybrid"
        assert v_req.sources == [str(seed_src)]
        # text_matches flows through.
        assert result.query_stats.text_matches == 1
        # workspace_id forwarded.
        assert v.search_calls[0]["workspace_id"] == ws
