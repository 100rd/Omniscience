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
    _EPOCH_DROPS,
    ANCHOR_FILTER_KEY,
    CANDIDATE_EXPANSION_FACTOR,
    MAX_ANCHOR_CANDIDATES,
    GraphRAGComposer,
    _apply_graph_watermark_filter,
    _apply_watermark_filter,
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
        ids, depths, _, _ = _collect_candidates(result)
        assert ids == (str(seed_src),)
        assert depths == (0,)

    def test_dedupes_duplicate_sources(self) -> None:
        seed_src = uuid.uuid4()
        other = uuid.uuid4()
        result = _make_graph_result(
            seed_source=seed_src,
            related=[(other, 1), (other, 2), (seed_src, 1)],
        )
        ids, _, _, _ = _collect_candidates(result)
        # seed + 1 unique related (second is duplicate of first, third duplicates seed)
        assert len(ids) == 2
        assert ids[0] == str(seed_src)
        assert ids[1] == str(other)

    def test_caps_at_max_anchor_candidates(self) -> None:
        seed_src = uuid.uuid4()
        related = [(uuid.uuid4(), 1) for _ in range(MAX_ANCHOR_CANDIDATES + 10)]
        result = _make_graph_result(seed_source=seed_src, related=related)
        ids, _, _, _ = _collect_candidates(result)
        assert len(ids) == MAX_ANCHOR_CANDIDATES

    def test_prioritizes_by_degree_centrality(self) -> None:
        seed_src = uuid.uuid4()
        rel1_src = uuid.uuid4()
        rel2_src = uuid.uuid4()
        rel3_src = uuid.uuid4()

        seed = EntityNodeView(
            id=uuid.uuid4(),
            name="seed-entity",
            kind="service",
            source=str(seed_src),
            chunk_text="seed chunk",
            depth=0,
        )
        rel1 = EntityNodeView(
            id=uuid.uuid4(),
            name="rel-1",
            kind="service",
            source=str(rel1_src),
            chunk_text="rel 1",
            depth=1,
        )
        rel2 = EntityNodeView(
            id=uuid.uuid4(),
            name="rel-2",
            kind="service",
            source=str(rel2_src),
            chunk_text="rel 2",
            depth=2,
        )
        rel3 = EntityNodeView(
            id=uuid.uuid4(),
            name="rel-3",
            kind="service",
            source=str(rel3_src),
            chunk_text="rel 3",
            depth=1,
        )

        edges = [
            GraphEdgeView(from_entity="seed-entity", to_entity="rel-2", edge_type="calls"),
            GraphEdgeView(from_entity="seed-entity", to_entity="rel-1", edge_type="calls"),
            GraphEdgeView(from_entity="rel-1", to_entity="rel-2", edge_type="calls"),
            GraphEdgeView(from_entity="rel-3", to_entity="rel-2", edge_type="calls"),
        ]

        result = GraphResultView(seed=seed, related=[rel3, rel1, rel2], edges=edges)
        _collect_candidates(result)

        assert result.related[0].name == "rel-2"
        assert result.related[1].name == "rel-1"
        assert result.related[2].name == "rel-3"

    def test_tie_breaks_by_depth(self) -> None:
        seed_src = uuid.uuid4()
        rel1_src = uuid.uuid4()
        rel2_src = uuid.uuid4()

        seed = EntityNodeView(
            id=uuid.uuid4(),
            name="seed-entity",
            kind="service",
            source=str(seed_src),
            chunk_text="seed chunk",
            depth=0,
        )
        rel1 = EntityNodeView(
            id=uuid.uuid4(),
            name="rel-1",
            kind="service",
            source=str(rel1_src),
            chunk_text="rel 1",
            depth=1,
        )
        rel2 = EntityNodeView(
            id=uuid.uuid4(),
            name="rel-2",
            kind="service",
            source=str(rel2_src),
            chunk_text="rel 2",
            depth=2,
        )
        edges = [
            GraphEdgeView(from_entity="seed-entity", to_entity="rel-1", edge_type="calls"),
            GraphEdgeView(from_entity="seed-entity", to_entity="rel-2", edge_type="calls"),
        ]

        result = GraphResultView(seed=seed, related=[rel2, rel1], edges=edges)
        _collect_candidates(result)

        assert result.related[0].name == "rel-1"
        assert result.related[1].name == "rel-2"

    def test_filters_discarded_edges(self) -> None:
        seed_src = uuid.uuid4()
        seed = EntityNodeView(
            id=uuid.uuid4(),
            name="seed-entity",
            kind="service",
            source=str(seed_src),
            chunk_text="seed chunk",
            depth=0,
        )

        related = [
            EntityNodeView(
                id=uuid.uuid4(),
                name=f"rel-{i}",
                kind="service",
                source=str(uuid.uuid4()),
                chunk_text=f"rel chunk {i}",
                depth=99 if i == MAX_ANCHOR_CANDIDATES + 1 else 1,
            )
            for i in range(MAX_ANCHOR_CANDIDATES + 2)
        ]

        edges = [
            GraphEdgeView(from_entity="seed-entity", to_entity=f"rel-{i}", edge_type="calls")
            for i in range(len(related))
            if i != MAX_ANCHOR_CANDIDATES + 1
        ]
        discarded_entity_name = f"rel-{MAX_ANCHOR_CANDIDATES + 1}"
        edges.append(
            GraphEdgeView(from_entity="rel-0", to_entity=discarded_entity_name, edge_type="calls")
        )

        result = GraphResultView(seed=seed, related=related, edges=edges)
        _collect_candidates(result)

        assert discarded_entity_name not in [n.name for n in result.related]

        edge_targets = {e.to_entity for e in result.edges}
        assert discarded_entity_name not in edge_targets


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
            candidate_centralities={},
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
            candidate_centralities={},
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
            candidate_centralities={},
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
            candidate_centralities={},
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


@pytest.mark.asyncio
class TestReadTimePostgresValidation:
    """Tests for the read-time Postgres validation in GraphRAGComposer."""

    async def test_validate_entities_active_and_matching(self, force_graphrag: None) -> None:
        from unittest.mock import AsyncMock, MagicMock

        # We mock DB results: seed-entity is active, rel-0 is inactive (not returned by execute)
        session = AsyncMock()
        session.__aenter__.return_value = session
        execute_mock = MagicMock()
        # Mock result.all() returning only the active entity name
        execute_mock.all.return_value = [("seed-entity",)]
        session.execute.return_value = execute_mock

        session_factory = MagicMock(return_value=session)

        seed_src = uuid.uuid4()
        other_src = uuid.uuid4()

        g = _FakeGraphStore(
            result=_make_graph_result(seed_source=seed_src, related=[(other_src, 1)])
        )

        v = _FakeVectorStore(_make_result([]))
        legacy = _FakeLegacyService(_make_result([]))

        composer = GraphRAGComposer(
            graph_store=cast("Any", g),
            vector_store=v,
            legacy_service=legacy,
            session_factory=session_factory,
        )

        req = SearchRequest(
            query="with-anchor",
            filters={ANCHOR_FILTER_KEY: "AuthService"},
        )
        ws = uuid.uuid4()

        anchor = await composer._run_anchor_stage(request=req, workspace_id=ws)
        # seed-entity is valid, so anchor hit is True
        assert anchor.anchor_hit is True
        # "rel-0" is filtered out, so candidate_source_ids has only seed_src
        assert anchor.candidate_source_ids == (str(seed_src),)

    async def test_validate_entities_seed_inactive_causes_anchor_miss(
        self, force_graphrag: None
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        session = AsyncMock()
        session.__aenter__.return_value = session
        execute_mock = MagicMock()
        # Seed entity not returned -> empty list
        execute_mock.all.return_value = []
        session.execute.return_value = execute_mock
        session_factory = MagicMock(return_value=session)

        g = _FakeGraphStore(result=_make_graph_result(seed_source=uuid.uuid4(), related=[]))
        v = _FakeVectorStore(_make_result([]))
        legacy = _FakeLegacyService(_make_result([]))

        composer = GraphRAGComposer(
            graph_store=cast("Any", g),
            vector_store=v,
            legacy_service=legacy,
            session_factory=session_factory,
        )

        req = SearchRequest(
            query="with-anchor",
            filters={ANCHOR_FILTER_KEY: "AuthService"},
        )
        anchor = await composer._run_anchor_stage(request=req, workspace_id=uuid.uuid4())
        # Seed was invalid, so anchor_hit should be False (treated as anchor miss)
        assert anchor.anchor_hit is False

    async def test_validate_hits_filters_inactive_documents(self, force_graphrag: None) -> None:
        from unittest.mock import AsyncMock, MagicMock

        session = AsyncMock()
        session.__aenter__.return_value = session
        execute_mock = MagicMock()

        chunk1_id = uuid.uuid4()
        chunk2_id = uuid.uuid4()

        # Only chunk1 is active in Postgres
        execute_mock.all.return_value = [(chunk1_id,)]
        session.execute.return_value = execute_mock
        session_factory = MagicMock(return_value=session)

        g = _FakeGraphStore()

        hit1 = _make_hit(chunk_id=chunk1_id, score=0.9)
        hit2 = _make_hit(chunk_id=chunk2_id, score=0.8)

        v = _FakeVectorStore(_make_result([hit1, hit2]))
        legacy = _FakeLegacyService(_make_result([]))

        composer = GraphRAGComposer(
            graph_store=cast("Any", g),
            vector_store=v,
            legacy_service=legacy,
            session_factory=session_factory,
        )

        req = SearchRequest(query="test")
        result = await composer.search(req, workspace_id=uuid.uuid4())

        # Only hit1 should be returned, hit2 is filtered out because it's not valid/active
        assert len(result.hits) == 1
        assert result.hits[0].chunk_id == chunk1_id


# ---------------------------------------------------------------------------
# v11-AP2 tests: explicit anchor-miss, no control-flow-by-exception
# ---------------------------------------------------------------------------


def test_apply_graph_watermark_filter_returns_tuple() -> None:
    """_apply_graph_watermark_filter returns (result, seed_excluded) tuple (v11-AP2).

    Verifies the API change: the function no longer raises ValueError for
    graph-ahead seeds — it returns an explicit boolean signal.
    """

    src = str(uuid.uuid4())

    # Graph-ahead seed: version=10, watermark=7 → seed_excluded=True
    seed_ahead = EntityNodeView(
        id=uuid.uuid4(),
        name="AheadSeed",
        kind="service",
        source=src,
        chunk_text=None,
        depth=0,
        version=10,
    )
    graph_result_ahead = GraphResultView(seed=seed_ahead, related=[], edges=[])
    _ahead_result, seed_excluded = _apply_graph_watermark_filter(graph_result_ahead, {src: 7})
    assert seed_excluded is True, (
        "v11-AP2: graph-ahead seed must set seed_excluded=True (not raise ValueError)"
    )

    # Seed within watermark: version=5, watermark=7 → seed_excluded=False
    seed_ok = EntityNodeView(
        id=uuid.uuid4(),
        name="OkSeed",
        kind="service",
        source=src,
        chunk_text=None,
        depth=0,
        version=5,
    )
    graph_result_ok = GraphResultView(seed=seed_ok, related=[], edges=[])
    _ok_result, seed_excluded_ok = _apply_graph_watermark_filter(graph_result_ok, {src: 7})
    assert seed_excluded_ok is False, "Seed within watermark must not be excluded"


@pytest.mark.asyncio
async def test_genuine_traverse_error_propagates_not_swallowed_as_anchor_miss(
    force_graphrag: None,
) -> None:
    """v11-AP2: a genuine traverse() error (non-ValueError) must NOT be masked as anchor-miss.

    Before v11-AP2, the too-wide except block caught ANY exception from
    the graph store and treated it as anchor_hit=False.  This masked
    real errors (network issues, schema errors) as silent misses.
    Now only ValueError is caught; all other exceptions propagate.
    """

    class _RuntimeErrorStore:
        """A graph store that raises RuntimeError (simulates a network/schema error)."""

        async def traverse(
            self,
            *,
            entity_name: str,
            workspace_id: uuid.UUID,
            as_of: datetime | None = None,
            max_depth: int = 1,
            edge_types: list[str] | None = None,
        ) -> GraphResultView:
            raise RuntimeError("connection_pool_exhausted: simulated network failure")

    v = _FakeVectorStore(_make_result([]))
    legacy = _FakeLegacyService(_make_result([]))
    composer = GraphRAGComposer(
        graph_store=cast("Any", _RuntimeErrorStore()),
        vector_store=v,
        legacy_service=legacy,
    )
    # Force graphrag path
    composer._graphrag_active = True  # type: ignore[attr-defined]

    req = SearchRequest(
        query="traverse-error",
        top_k=5,
        filters={ANCHOR_FILTER_KEY: "SomeEntity"},
    )
    # The RuntimeError must propagate — it must NOT be silently converted to anchor_hit=False.
    with pytest.raises(RuntimeError, match="connection_pool_exhausted"):
        await composer._run_anchor_stage(
            request=req,
            workspace_id=uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_graph_ahead_seed_yields_clean_anchor_miss_no_exception(
    force_graphrag: None,
) -> None:
    """v11-AP2: a graph-ahead seed yields anchor_hit=False without raising any exception.

    _apply_graph_watermark_filter returns (result, seed_excluded=True); the
    anchor stage maps this to anchor_hit=False deterministically.  No
    exception is raised, no genuine traverse error is masked.
    """
    src_id = uuid.uuid4()
    src_str = str(src_id)

    seed = EntityNodeView(
        id=uuid.uuid4(),
        name="AheadSeed",
        kind="service",
        source=src_str,
        chunk_text=None,
        depth=0,
        version=10,  # > watermark of 7
    )
    graph_result = GraphResultView(seed=seed, related=[], edges=[])

    class _AheadSeedStore:
        async def traverse(
            self,
            *,
            entity_name: str,
            workspace_id: uuid.UUID,
            as_of: datetime | None = None,
            max_depth: int = 1,
            edge_types: list[str] | None = None,
        ) -> GraphResultView:
            return graph_result

    v = _FakeVectorStore(_make_result([]))
    legacy = _FakeLegacyService(_make_result([]))
    composer = GraphRAGComposer(
        graph_store=cast("Any", _AheadSeedStore()),
        vector_store=v,
        legacy_service=legacy,
    )
    composer._graphrag_active = True  # type: ignore[attr-defined]

    per_source_wm = {src_str: 7}

    req = SearchRequest(
        query="graph-ahead",
        top_k=5,
        filters={ANCHOR_FILTER_KEY: "AheadSeed"},
    )
    # Must not raise — the graph-ahead seed is a clean anchor-miss via explicit signal.
    anchor = await composer._run_anchor_stage(
        request=req,
        workspace_id=uuid.uuid4(),
        per_source_watermark=per_source_wm,
    )
    assert anchor.anchor_hit is False, (
        "v11-AP2: graph-ahead seed must yield anchor_hit=False (no exception)"
    )
    assert anchor.anchor_requested is True
    assert anchor.candidate_source_ids == ()


# ---------------------------------------------------------------------------
# v11-AP5 tests: reachability recompute after pruning ahead-nodes
# ---------------------------------------------------------------------------


def test_recompute_reachability_drops_phantom_nodes() -> None:
    """v11-AP5: a node reachable ONLY via an excluded ahead-node is removed.

    Topology:
      Seed --edge1--> AheadNode (version=10, wm=7 → excluded)
      AheadNode --edge2--> PhantomNode

    After AheadNode is excluded and edge1/edge2 are pruned, PhantomNode
    is no longer reachable from Seed — it must be removed from related.
    """

    src_seed = str(uuid.uuid4())
    src_ahead = str(uuid.uuid4())
    src_phantom = str(uuid.uuid4())

    seed = EntityNodeView(
        id=uuid.uuid4(),
        name="Seed",
        kind="service",
        source=src_seed,
        chunk_text=None,
        depth=0,
        version=5,
    )
    ahead_node = EntityNodeView(
        id=uuid.uuid4(),
        name="AheadNode",
        kind="repo",
        source=src_ahead,
        chunk_text=None,
        depth=1,
        version=10,  # > ahead_node's watermark of 7 → will be excluded
    )
    phantom_node = EntityNodeView(
        id=uuid.uuid4(),
        name="PhantomNode",
        kind="team",
        source=src_phantom,
        chunk_text=None,
        depth=2,
        version=3,  # within watermark — but only reachable via AheadNode
    )

    edge1 = GraphEdgeView(from_entity="Seed", to_entity="AheadNode", edge_type="OWNS")
    edge2 = GraphEdgeView(from_entity="AheadNode", to_entity="PhantomNode", edge_type="USES")

    graph_result = GraphResultView(
        seed=seed,
        related=[ahead_node, phantom_node],
        edges=[edge1, edge2],
    )
    per_source_wm = {src_seed: 10, src_ahead: 7, src_phantom: 10}

    filtered, seed_excluded = _apply_graph_watermark_filter(graph_result, per_source_wm)

    assert seed_excluded is False, "Seed is within watermark (v5 <= wm=10)"
    remaining_names = {n.name for n in filtered.related}
    assert "AheadNode" not in remaining_names, (
        "AheadNode (version=10 > wm=7) must be excluded by watermark filter"
    )
    assert "PhantomNode" not in remaining_names, (
        "PhantomNode reachable ONLY via excluded AheadNode must also be removed (v11-AP5 BFS)"
    )


def test_recompute_reachability_keeps_directly_connected_nodes() -> None:
    """v11-AP5: nodes directly connected to seed survive even when other paths are cut.

    Topology:
      Seed --edge1--> DirectNode (version=3, within watermark)
      Seed --edge2--> AheadNode (version=10 > watermark → excluded)
      AheadNode --edge3--> RelatedViaAhead (only reachable via AheadNode → dropped)

    After exclusion + BFS:
      - DirectNode: still reachable via edge1 → kept
      - AheadNode: excluded by watermark
      - RelatedViaAhead: unreachable after AheadNode removed → dropped
    """

    src_seed = str(uuid.uuid4())
    src_direct = str(uuid.uuid4())
    src_ahead = str(uuid.uuid4())
    src_via_ahead = str(uuid.uuid4())

    seed = EntityNodeView(
        id=uuid.uuid4(),
        name="Seed",
        kind="service",
        source=src_seed,
        chunk_text=None,
        depth=0,
        version=5,
    )
    direct_node = EntityNodeView(
        id=uuid.uuid4(),
        name="DirectNode",
        kind="repo",
        source=src_direct,
        chunk_text=None,
        depth=1,
        version=3,
    )
    ahead_node = EntityNodeView(
        id=uuid.uuid4(),
        name="AheadNode",
        kind="team",
        source=src_ahead,
        chunk_text=None,
        depth=1,
        version=10,  # > wm=7
    )
    via_ahead = EntityNodeView(
        id=uuid.uuid4(),
        name="RelatedViaAhead",
        kind="service",
        source=src_via_ahead,
        chunk_text=None,
        depth=2,
        version=2,
    )

    edges = [
        GraphEdgeView(from_entity="Seed", to_entity="DirectNode", edge_type="OWNS"),
        GraphEdgeView(from_entity="Seed", to_entity="AheadNode", edge_type="USES"),
        GraphEdgeView(from_entity="AheadNode", to_entity="RelatedViaAhead", edge_type="PART_OF"),
    ]
    graph_result = GraphResultView(
        seed=seed, related=[direct_node, ahead_node, via_ahead], edges=edges
    )
    per_source_wm = {src_seed: 10, src_direct: 10, src_ahead: 7, src_via_ahead: 10}

    filtered, seed_excluded = _apply_graph_watermark_filter(graph_result, per_source_wm)

    assert seed_excluded is False
    remaining_names = {n.name for n in filtered.related}
    assert "DirectNode" in remaining_names, (
        "DirectNode directly connected to Seed and within watermark must survive"
    )
    assert "AheadNode" not in remaining_names, "AheadNode (version=10 > wm=7) must be excluded"
    assert "RelatedViaAhead" not in remaining_names, (
        "RelatedViaAhead reachable only via excluded AheadNode must be dropped (v11-AP5)"
    )


# ---------------------------------------------------------------------------
# v13-AP1: reason-labeled _EPOCH_DROPS counter tests
# ---------------------------------------------------------------------------


class TestEpochDropsCounter:
    """v13-AP1: _EPOCH_DROPS fires with correct (reason, filter) labels.

    All assertions use DELTA semantics: capture counter value before the call,
    assert the exact increment after.  This is CRITICAL because the default
    Prometheus registry accumulates across all tests in the process.
    Asserting absolute values would cause cross-test interference.
    """

    def test_epoch_drops_counter_in_map_lag(self) -> None:
        """lag: hit with applied_version > watermark > 0 increments lag/hit counter."""
        src_id = uuid.uuid4()
        # applied_version=5, wm=3 → 5 > 3 → lag (wm>0 so not cold_zero)
        hit_lag = SearchHit(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            score=0.8,
            text="lag-hit",
            source=SourceInfo(id=src_id, name="src", type="slack"),
            citation=Citation(
                uri="mem://x",
                title=None,
                indexed_at=datetime(2026, 6, 1, tzinfo=UTC),
                doc_version=1,
            ),
            lineage=ChunkLineage(
                ingestion_run_id=None,
                embedding_model="stub",
                embedding_provider="stub",
                parser_version="1",
                chunker_strategy="fixed",
            ),
            metadata={},
            applied_version=5,
        )
        # Hit that should PASS (applied_version=0, wm=0 → 0 <= 0 → PASS, no counter)
        hit_epoch0 = SearchHit(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            score=0.7,
            text="epoch0-hit",
            source=SourceInfo(id=src_id, name="src", type="slack"),
            citation=Citation(
                uri="mem://x",
                title=None,
                indexed_at=datetime(2026, 6, 1, tzinfo=UTC),
                doc_version=1,
            ),
            lineage=ChunkLineage(
                ingestion_run_id=None,
                embedding_model="stub",
                embedding_provider="stub",
                parser_version="1",
                chunker_strategy="fixed",
            ),
            metadata={},
            applied_version=0,
        )

        before_lag = _EPOCH_DROPS.labels(reason="lag", filter="hit")._value.get()
        before_cold = _EPOCH_DROPS.labels(reason="cold_zero", filter="hit")._value.get()

        result = _apply_watermark_filter([hit_lag, hit_epoch0], {str(src_id): 3})

        # hit_lag (av=5 > wm=3 > 0) must be dropped with reason=lag
        dropped_versions = {h.applied_version for h in result}
        assert 5 not in dropped_versions, "av=5 > wm=3 must be dropped"

        # hit_epoch0 (av=0, wm=0 → 0 <= 0) must PASS — no counter
        assert 0 in dropped_versions, "av=0, wm=0 must PASS (epoch-0 consistent read)"

        # lag counter fires once for the dropped hit
        assert _EPOCH_DROPS.labels(reason="lag", filter="hit")._value.get() == before_lag + 1, (
            "v13-AP1: lag/hit counter must increment by 1 for av=5 > wm=3"
        )
        # cold_zero counter must NOT fire (wm=3 ≠ 0)
        assert _EPOCH_DROPS.labels(reason="cold_zero", filter="hit")._value.get() == before_cold, (
            "v13-AP1: cold_zero/hit counter must not fire when wm>0"
        )

    def test_epoch_drops_counter_in_map_cold_zero(self) -> None:
        """cold_zero: hit with av=1, wm=0 increments cold_zero/hit counter.

        BOUNDARY: av=0, wm=0 → 0 <= 0 → PASS with NO counter increment.
        """
        src_id = uuid.uuid4()

        hit_cold = SearchHit(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            score=0.8,
            text="cold-hit",
            source=SourceInfo(id=src_id, name="src", type="slack"),
            citation=Citation(
                uri="mem://x",
                title=None,
                indexed_at=datetime(2026, 6, 1, tzinfo=UTC),
                doc_version=1,
            ),
            lineage=ChunkLineage(
                ingestion_run_id=None,
                embedding_model="stub",
                embedding_provider="stub",
                parser_version="1",
                chunker_strategy="fixed",
            ),
            metadata={},
            applied_version=1,  # av=1, wm=0 → cold_zero drop
        )
        # BOUNDARY: av=0, wm=0 → PASS (0 <= 0); no counter
        hit_epoch0 = SearchHit(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            score=0.7,
            text="epoch0",
            source=SourceInfo(id=src_id, name="src", type="slack"),
            citation=Citation(
                uri="mem://x",
                title=None,
                indexed_at=datetime(2026, 6, 1, tzinfo=UTC),
                doc_version=1,
            ),
            lineage=ChunkLineage(
                ingestion_run_id=None,
                embedding_model="stub",
                embedding_provider="stub",
                parser_version="1",
                chunker_strategy="fixed",
            ),
            metadata={},
            applied_version=0,  # av=0, wm=0 → PASS boundary
        )

        before_cold = _EPOCH_DROPS.labels(reason="cold_zero", filter="hit")._value.get()
        before_lag = _EPOCH_DROPS.labels(reason="lag", filter="hit")._value.get()

        result = _apply_watermark_filter([hit_cold, hit_epoch0], {str(src_id): 0})

        # av=1 with wm=0 must be dropped (cold_zero)
        dropped_versions = {h.applied_version for h in result}
        assert 1 not in dropped_versions, "av=1 with wm=0 must be dropped (cold_zero)"

        # av=0, wm=0 → 0 <= 0 → PASS (epoch-0 boundary)
        assert 0 in dropped_versions, "av=0, wm=0 must PASS (epoch-0 consistent read)"

        # cold_zero fires once for av=1 drop
        assert (
            _EPOCH_DROPS.labels(reason="cold_zero", filter="hit")._value.get() == before_cold + 1
        ), "v13-AP1: cold_zero/hit must fire for av=1 with wm=0"
        # lag must NOT fire (wm=0 classifies as cold_zero, not lag)
        assert _EPOCH_DROPS.labels(reason="lag", filter="hit")._value.get() == before_lag, (
            "v13-AP1: lag/hit must not fire for av=1 with wm=0 (classified as cold_zero)"
        )


def test_graph_node_drop_increments_epoch_drops_node() -> None:
    """v13-AP1: _apply_graph_watermark_filter increments _EPOCH_DROPS(filter='node').

    Verifies:
    - related node with av > wm > 0 fires lag/node
    - related node with av >= 1 and wm == 0 fires cold_zero/node
    - seed-excluded path (version > wm) does NOT fire any node counter
      (the caller's anchor-miss is the observable signal)
    - unmapped source graph node PASSES without firing any counter
    """
    src_seed = str(uuid.uuid4())
    src_lag = str(uuid.uuid4())
    src_cold = str(uuid.uuid4())
    src_unmapped = str(uuid.uuid4())

    seed = EntityNodeView(
        id=uuid.uuid4(),
        name="Seed",
        kind="service",
        source=src_seed,
        chunk_text=None,
        depth=0,
        version=3,
    )
    node_lag = EntityNodeView(
        id=uuid.uuid4(),
        name="LagNode",
        kind="repo",
        source=src_lag,
        chunk_text=None,
        depth=1,
        version=7,  # > wm=3 and wm > 0 → lag
    )
    node_cold = EntityNodeView(
        id=uuid.uuid4(),
        name="ColdNode",
        kind="team",
        source=src_cold,
        chunk_text=None,
        depth=1,
        version=1,  # >= 1 and wm=0 → cold_zero
    )
    node_unmapped = EntityNodeView(
        id=uuid.uuid4(),
        name="UnmappedNode",
        kind="service",
        source=src_unmapped,
        chunk_text=None,
        depth=1,
        version=999,  # very high, but src_unmapped not in map → PASS by design
    )

    graph_result = GraphResultView(
        seed=seed,
        related=[node_lag, node_cold, node_unmapped],
        edges=[
            GraphEdgeView(from_entity="Seed", to_entity="LagNode", edge_type="USES"),
            GraphEdgeView(from_entity="Seed", to_entity="ColdNode", edge_type="USES"),
            GraphEdgeView(from_entity="Seed", to_entity="UnmappedNode", edge_type="USES"),
        ],
    )

    per_source_wm = {
        src_seed: 10,  # seed within watermark
        src_lag: 3,  # LagNode at v7 > wm=3 (and wm>0) → lag drop
        src_cold: 0,  # ColdNode at v1 >= 1, wm=0 → cold_zero drop
        # src_unmapped intentionally absent → graph node passes
    }

    before_lag_node = _EPOCH_DROPS.labels(reason="lag", filter="node")._value.get()
    before_cold_node = _EPOCH_DROPS.labels(reason="cold_zero", filter="node")._value.get()
    before_unmapped_node = _EPOCH_DROPS.labels(reason="unmapped", filter="node")._value.get()

    filtered, seed_excluded = _apply_graph_watermark_filter(graph_result, per_source_wm)

    assert seed_excluded is False, "Seed (v3 <= wm=10) must not be excluded"

    remaining = {n.name for n in filtered.related}
    assert "LagNode" not in remaining, "LagNode (v7 > wm=3) must be excluded"
    assert "ColdNode" not in remaining, "ColdNode (v1 >= 1, wm=0) must be excluded"
    assert "UnmappedNode" in remaining, "UnmappedNode (unmapped source) must pass by design"

    # lag/node fires once for LagNode
    assert _EPOCH_DROPS.labels(reason="lag", filter="node")._value.get() == before_lag_node + 1, (
        "v13-AP1: lag/node must fire for LagNode (v7 > wm=3, wm>0)"
    )
    # cold_zero/node fires once for ColdNode
    assert (
        _EPOCH_DROPS.labels(reason="cold_zero", filter="node")._value.get() == before_cold_node + 1
    ), "v13-AP1: cold_zero/node must fire for ColdNode (v1 >= 1, wm=0)"
    # unmapped/node must NOT fire (graph nodes from unmapped sources pass by design)
    assert (
        _EPOCH_DROPS.labels(reason="unmapped", filter="node")._value.get() == before_unmapped_node
    ), (
        "v13-AP1: unmapped/node must NOT fire for graph nodes from unmapped sources "
        "(they pass by design — only vector-hit filter enforces unmapped drops)"
    )


def test_graph_node_seed_excluded_does_not_fire_node_counter() -> None:
    """v13-AP1: seed-excluded path (graph-ahead seed) must NOT increment filter=node counters.

    When the seed itself is graph-ahead, _apply_graph_watermark_filter returns
    (graph_result, True) immediately — it does NOT loop through related nodes
    and does NOT call _EPOCH_DROPS.  The caller (_run_anchor_stage) records the
    exclusion as an anchor-miss via the anchor outcome counter.
    """
    src = str(uuid.uuid4())
    seed_ahead = EntityNodeView(
        id=uuid.uuid4(),
        name="AheadSeed",
        kind="service",
        source=src,
        chunk_text=None,
        depth=0,
        version=10,  # > wm=7
    )
    graph_result = GraphResultView(seed=seed_ahead, related=[], edges=[])

    before_lag = _EPOCH_DROPS.labels(reason="lag", filter="node")._value.get()
    before_cold = _EPOCH_DROPS.labels(reason="cold_zero", filter="node")._value.get()

    _result, seed_excluded = _apply_graph_watermark_filter(graph_result, {src: 7})
    assert seed_excluded is True

    # No node counters must fire for the seed-excluded early return
    assert _EPOCH_DROPS.labels(reason="lag", filter="node")._value.get() == before_lag, (
        "v13-AP1: seed-excluded path must not fire lag/node"
    )
    assert _EPOCH_DROPS.labels(reason="cold_zero", filter="node")._value.get() == before_cold, (
        "v13-AP1: seed-excluded path must not fire cold_zero/node"
    )


# ---------------------------------------------------------------------------
# v14-D4: symmetric fail-closed for unmapped graph nodes (topology-epoch fix)
# ---------------------------------------------------------------------------


def test_unmapped_graph_node_excluded_under_strict_epoch() -> None:
    """v14-D4: unmapped-source graph node is EXCLUDED under strict_epoch=True.

    Scenario: Seed(src_a, in map) → FutureEnt(src_b, NOT in map, v999).
    Under strict_epoch=True the unmapped node must be excluded and
    _EPOCH_DROPS(reason="unmapped", filter="node") must increment once.
    Under strict_epoch=False the node passes (legacy behaviour).
    """
    src_a = str(uuid.uuid4())
    src_b = str(uuid.uuid4())  # unmapped — not in per_source_watermark

    seed = EntityNodeView(
        id=uuid.uuid4(),
        name="SeedA",
        kind="service",
        source=src_a,
        chunk_text=None,
        depth=0,
        version=5,
    )
    future_node = EntityNodeView(
        id=uuid.uuid4(),
        name="FutureEnt",
        kind="service",
        source=src_b,
        chunk_text=None,
        depth=1,
        version=999,  # far-future topology from unmapped src_b
    )
    graph_result_strict = GraphResultView(
        seed=seed,
        related=[future_node],
        edges=[GraphEdgeView(from_entity="SeedA", to_entity="FutureEnt", edge_type="USES")],
    )
    graph_result_lax = GraphResultView(
        seed=EntityNodeView(
            id=seed.id,
            name=seed.name,
            kind=seed.kind,
            source=seed.source,
            chunk_text=seed.chunk_text,
            depth=seed.depth,
            version=seed.version,
        ),
        related=[
            EntityNodeView(
                id=future_node.id,
                name=future_node.name,
                kind=future_node.kind,
                source=future_node.source,
                chunk_text=future_node.chunk_text,
                depth=future_node.depth,
                version=future_node.version,
            )
        ],
        edges=[GraphEdgeView(from_entity="SeedA", to_entity="FutureEnt", edge_type="USES")],
    )

    per_source_wm = {src_a: 10}  # src_b intentionally absent

    # --- strict_epoch=True: unmapped node must be excluded ---
    before_unmapped_node = _EPOCH_DROPS.labels(reason="unmapped", filter="node")._value.get()

    filtered_strict, seed_excluded = _apply_graph_watermark_filter(
        graph_result_strict, per_source_wm, strict_epoch=True
    )

    assert seed_excluded is False, "SeedA (v5 <= wm=10) must not be excluded"
    remaining_strict = {n.name for n in filtered_strict.related}
    assert "FutureEnt" not in remaining_strict, (
        "v14-D4: FutureEnt from unmapped src_b must be excluded under strict_epoch=True"
    )

    # Counter must increment exactly once for the unmapped node drop
    after_unmapped_node = _EPOCH_DROPS.labels(reason="unmapped", filter="node")._value.get()
    assert after_unmapped_node == before_unmapped_node + 1, (
        "v14-D4: _EPOCH_DROPS(reason='unmapped', filter='node') must fire once "
        f"(before={before_unmapped_node}, after={after_unmapped_node})"
    )

    # --- strict_epoch=False: unmapped node must PASS (legacy) ---
    before2 = _EPOCH_DROPS.labels(reason="unmapped", filter="node")._value.get()

    filtered_lax, seed_excl_lax = _apply_graph_watermark_filter(
        graph_result_lax, per_source_wm, strict_epoch=False
    )

    assert seed_excl_lax is False
    remaining_lax = {n.name for n in filtered_lax.related}
    assert "FutureEnt" in remaining_lax, (
        "v14-D4: strict_epoch=False must preserve legacy pass-through for unmapped nodes"
    )
    after2 = _EPOCH_DROPS.labels(reason="unmapped", filter="node")._value.get()
    assert after2 == before2, "v14-D4: strict_epoch=False must NOT increment unmapped/node counter"


def test_unmapped_node_phantom_path_pruned_by_reachability() -> None:
    """v14-D4: a node reachable ONLY via an excluded unmapped node is pruned.

    Graph: Seed(src_a) → UnmappedGateway(src_b) → Phantom(src_c, in map).
    Under strict_epoch=True:
    - UnmappedGateway is excluded (src_b unmapped).
    - Phantom is reachable only through UnmappedGateway → also excluded by
      _recompute_reachability BFS.
    """
    src_a = str(uuid.uuid4())
    src_b = str(uuid.uuid4())  # unmapped
    src_c = str(uuid.uuid4())  # in map, within watermark

    seed = EntityNodeView(
        id=uuid.uuid4(),
        name="Seed",
        kind="service",
        source=src_a,
        chunk_text=None,
        depth=0,
        version=3,
    )
    unmapped_gateway = EntityNodeView(
        id=uuid.uuid4(),
        name="UnmappedGateway",
        kind="service",
        source=src_b,
        chunk_text=None,
        depth=1,
        version=500,
    )
    phantom = EntityNodeView(
        id=uuid.uuid4(),
        name="Phantom",
        kind="service",
        source=src_c,
        chunk_text=None,
        depth=2,
        version=1,
    )

    graph_result = GraphResultView(
        seed=seed,
        related=[unmapped_gateway, phantom],
        edges=[
            GraphEdgeView(from_entity="Seed", to_entity="UnmappedGateway", edge_type="USES"),
            GraphEdgeView(from_entity="UnmappedGateway", to_entity="Phantom", edge_type="CALLS"),
        ],
    )

    per_source_wm = {src_a: 10, src_c: 5}  # src_b absent

    filtered, seed_excluded = _apply_graph_watermark_filter(
        graph_result, per_source_wm, strict_epoch=True
    )

    assert seed_excluded is False

    remaining = {n.name for n in filtered.related}
    assert "UnmappedGateway" not in remaining, (
        "v14-D4: UnmappedGateway from unmapped src_b must be excluded"
    )
    assert "Phantom" not in remaining, (
        "v14-D4: Phantom reachable only via UnmappedGateway must also be pruned "
        "by reachability recompute (no phantom path from excluded unmapped node)"
    )


def test_legitimate_cross_source_link_not_regressed() -> None:
    """v14-D4: a cross-source node whose source IS in the watermark map is NOT dropped.

    Scenario: Seed(src_a) → HealthyNode(src_b, in map, within watermark).
    Both sources are in the map → healthy link → HealthyNode survives.
    This guards against regressions to legitimate cross_ref linking (e.g. AWS↔K8s).
    """
    src_a = str(uuid.uuid4())
    src_b = str(uuid.uuid4())  # cross-source, but IN the map and within watermark

    seed = EntityNodeView(
        id=uuid.uuid4(),
        name="AWSEntity",
        kind="service",
        source=src_a,
        chunk_text=None,
        depth=0,
        version=2,
    )
    healthy_cross = EntityNodeView(
        id=uuid.uuid4(),
        name="K8sEntity",
        kind="deployment",
        source=src_b,
        chunk_text=None,
        depth=1,
        version=3,  # <= wm=5 → within watermark
    )

    graph_result = GraphResultView(
        seed=seed,
        related=[healthy_cross],
        edges=[
            GraphEdgeView(from_entity="AWSEntity", to_entity="K8sEntity", edge_type="cross_ref")
        ],
    )

    per_source_wm = {src_a: 10, src_b: 5}  # BOTH sources in map

    before_unmapped = _EPOCH_DROPS.labels(reason="unmapped", filter="node")._value.get()

    filtered, seed_excluded = _apply_graph_watermark_filter(
        graph_result, per_source_wm, strict_epoch=True
    )

    assert seed_excluded is False
    remaining = {n.name for n in filtered.related}
    assert "K8sEntity" in remaining, (
        "v14-D4: K8sEntity (src_b in map, v3 <= wm=5) must NOT be excluded — "
        "legitimate cross-source link must survive strict_epoch=True"
    )

    after_unmapped = _EPOCH_DROPS.labels(reason="unmapped", filter="node")._value.get()
    assert after_unmapped == before_unmapped, (
        "v14-D4: no unmapped/node drop must fire for a cross-source node whose source IS in map"
    )


def test_topology_leak_closed_affinity_not_boosted_under_strict_epoch() -> None:
    """v14-D4 end-to-end topology-epoch leak test.

    Setup:
    - src_a is the seed source (in map, wm=10).
    - src_b is an unmapped source with a future-epoch graph node (v999).
    - A vector hit from src_b has applied_version=None (no epoch claim, passes hit filter).

    Under strict_epoch=True (v14-D4 fix):
    - The src_b graph node is excluded from the candidate set.
    - src_b therefore has NO graph-affinity entry.
    - The av=None hit from src_b receives affinity=0.0 (un-boosted baseline).
    - merged_score = (1-MERGE_ALPHA)*vector_score + MERGE_ALPHA*0.0.

    Under strict_epoch=False (old behaviour / legacy):
    - The src_b graph node passes → src_b IS in candidate set → gets affinity bonus.
    - merged_score = (1-MERGE_ALPHA)*vector_score + MERGE_ALPHA*GRAPH_AFFINITY_BASE.

    This test verifies the score difference and that _EPOCH_DROPS fires correctly.
    """
    from omniscience_retrieval.graph_rag import (
        GRAPH_AFFINITY_BASE,
        MERGE_ALPHA,
        _build_affinity_map,
        _collect_candidates,
        _linear_blend,
    )

    src_a = str(uuid.uuid4())
    src_b = str(uuid.uuid4())  # unmapped — topology-epoch leak source

    seed = EntityNodeView(
        id=uuid.uuid4(),
        name="SeedEnt",
        kind="service",
        source=src_a,
        chunk_text=None,
        depth=0,
        version=2,
    )
    future_node = EntityNodeView(
        id=uuid.uuid4(),
        name="FutureSrcB",
        kind="service",
        source=src_b,
        chunk_text=None,
        depth=1,
        version=999,
    )

    # Build two graph results — one for strict, one for lax
    def _make_graph() -> GraphResultView:
        return GraphResultView(
            seed=EntityNodeView(
                id=seed.id,
                name=seed.name,
                kind=seed.kind,
                source=seed.source,
                chunk_text=seed.chunk_text,
                depth=seed.depth,
                version=seed.version,
            ),
            related=[
                EntityNodeView(
                    id=future_node.id,
                    name=future_node.name,
                    kind=future_node.kind,
                    source=future_node.source,
                    chunk_text=future_node.chunk_text,
                    depth=future_node.depth,
                    version=future_node.version,
                )
            ],
            edges=[GraphEdgeView(from_entity="SeedEnt", to_entity="FutureSrcB", edge_type="USES")],
        )

    per_source_wm = {src_a: 10}  # src_b intentionally absent
    vector_score = 0.4

    # --- strict_epoch=True: unmapped node excluded → no affinity bonus ---
    before_unmapped = _EPOCH_DROPS.labels(reason="unmapped", filter="node")._value.get()

    g_strict = _make_graph()
    filtered_strict, _ = _apply_graph_watermark_filter(g_strict, per_source_wm, strict_epoch=True)

    after_unmapped = _EPOCH_DROPS.labels(reason="unmapped", filter="node")._value.get()
    assert after_unmapped == before_unmapped + 1, (
        "v14-D4: unmapped/node counter must fire for future src_b node under strict_epoch=True"
    )

    # Collect candidates — src_b must NOT be in the candidate set
    candidates_strict, depths_strict, centralities_strict, parked_strict = _collect_candidates(
        filtered_strict
    )
    assert src_b not in candidates_strict, (
        "v14-D4: src_b must NOT be a candidate after unmapped-node exclusion"
    )

    # Build affinity map — src_b absent → affinity=0.0
    from omniscience_retrieval.graph_rag import _AnchorStageResult

    anchor_strict = _AnchorStageResult(
        anchor_requested=True,
        anchor_name="SeedEnt",
        anchor_hit=True,
        candidate_source_ids=candidates_strict,
        candidate_depths=depths_strict,
        candidate_centralities=centralities_strict,
        candidate_parked=parked_strict,
        duration_s=0.0,
    )
    affinity_map_strict = _build_affinity_map(anchor_strict)
    affinity_for_src_b_strict = affinity_map_strict.get(str(src_b), 0.0)
    assert affinity_for_src_b_strict == 0.0, (
        f"v14-D4: src_b affinity must be 0.0 (no topology bonus); got {affinity_for_src_b_strict}"
    )

    merged_strict = _linear_blend(vector_score=vector_score, graph_affinity=0.0)
    expected_no_boost = (1.0 - MERGE_ALPHA) * vector_score
    assert merged_strict == pytest.approx(expected_no_boost), (
        f"v14-D4: un-boosted score should be {expected_no_boost:.4f}; got {merged_strict:.4f}"
    )

    # --- strict_epoch=False: unmapped node passes → affinity bonus applies ---
    g_lax = _make_graph()
    filtered_lax, _ = _apply_graph_watermark_filter(g_lax, per_source_wm, strict_epoch=False)

    candidates_lax, depths_lax, centralities_lax, parked_lax = _collect_candidates(filtered_lax)
    assert src_b in candidates_lax, (
        "Legacy: src_b must be a candidate when strict_epoch=False (unmapped node passes)"
    )

    anchor_lax = _AnchorStageResult(
        anchor_requested=True,
        anchor_name="SeedEnt",
        anchor_hit=True,
        candidate_source_ids=candidates_lax,
        candidate_depths=depths_lax,
        candidate_centralities=centralities_lax,
        candidate_parked=parked_lax,
        duration_s=0.0,
    )
    affinity_map_lax = _build_affinity_map(anchor_lax)
    affinity_for_src_b_lax = affinity_map_lax.get(str(src_b), 0.0)
    expected_affinity = GRAPH_AFFINITY_BASE  # depth=1 → affinity=1.0
    assert affinity_for_src_b_lax == pytest.approx(expected_affinity), (
        f"Legacy: src_b affinity should be {expected_affinity} (depth=1); "
        f"got {affinity_for_src_b_lax}"
    )

    merged_lax = _linear_blend(vector_score=vector_score, graph_affinity=affinity_for_src_b_lax)
    expected_boosted = (1.0 - MERGE_ALPHA) * vector_score + MERGE_ALPHA * expected_affinity
    assert merged_lax == pytest.approx(expected_boosted), (
        f"Legacy: boosted score should be {expected_boosted:.4f} (0.4→{expected_boosted:.2f}); "
        f"got {merged_lax:.4f}"
    )

    # Confirm strict path score < legacy boosted score (the leak is closed)
    assert merged_strict < merged_lax, (
        f"v14-D4: strict score ({merged_strict:.4f}) must be < legacy boosted ({merged_lax:.4f})"
    )
