"""AP3 / v10-AP1 / v10-AP2 conformance tests — no mixed-epoch evidence.

AP3 invariant: every hit in a SearchResult must have
``applied_version <= watermark[its_source]`` (or ``applied_version is None``).
No composed result may contain evidence from a future epoch that some
stores have not yet applied.

v10-AP1 invariant (per-source watermark): a cold/lagging source B must NOT
decimate hits from healthy source A.  Each hit is evaluated against the
watermark of ITS OWN source.

v10-AP2 invariant (graph-anchor pin): no composed result mixes a graph
node/edge with ``version > watermark[its_source]`` with evidence pinned
to that watermark.  The graph VIEW is pinned to the same per-source epoch
as the vector evidence.

These tests are REAL (not xfail) and must remain green throughout the
codebase lifetime.  They use mocks only — no containers required.

Test cases
----------
1. watermark=7, hits with applied_version {5,7,8,10} → result only ≤7;
   ``pinned_watermark==7``.
2. watermark None → all hits pass; ``pinned_watermark is None``.
3. converged watermark=10, all hits ≤10 → all pass; ``pinned_watermark==10``.
4. cold store watermark=0, hits applied_version=1 → result empty;
   ``pinned_watermark==0``.
5. ``_apply_watermark_filter`` pure-function boundary test (per-source map).
6. [v10-AP1] multi-source: source A@v10 healthy + source B@v0 cold →
   source A hits still returned; only source B hits dropped.
7. [v10-AP1] hit from source not in watermark map → passes (no blackout).
8. [v10-AP2] anchor node version > source watermark → excluded from anchor result
   (graph-ahead mixed-epoch prevented).
9. [v10-AP2] anchor seed node version > source watermark → treated as anchor miss.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_core.storage.graph import EntityNodeView, GraphEdgeView, GraphResultView
from omniscience_retrieval.graph_rag import (
    GraphRAGComposer,
    _apply_graph_watermark_filter,
    _apply_watermark_filter,
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
# Shared fixtures / factories
# ---------------------------------------------------------------------------

_WS = uuid.UUID("cccccccc-0000-0000-0000-000000000003")
_NOW = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)

_SRC_A = uuid.uuid4()
_SRC_B = uuid.uuid4()


def _make_hit(
    applied_version: int | None,
    text: str = "chunk",
    source_id: uuid.UUID | None = None,
) -> SearchHit:
    """Build a minimal SearchHit with the given applied_version."""
    sid = source_id if source_id is not None else uuid.uuid4()
    return SearchHit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        score=0.8,
        text=text,
        source=SourceInfo(id=sid, name="src", type="slack"),
        citation=Citation(uri="mem://x", title=None, indexed_at=_NOW, doc_version=1),
        lineage=ChunkLineage(
            ingestion_run_id=None,
            embedding_model="stub",
            embedding_provider="stub",
            parser_version="1",
            chunker_strategy="fixed",
        ),
        metadata={},
        applied_version=applied_version,
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


def _make_composer_with_reconciler(
    min_watermark: int | None,
    hits: list[SearchHit],
    per_source_watermark: dict[str, int] | None = None,
) -> GraphRAGComposer:
    """Build a GraphRAGComposer whose reconciler returns the given watermark.

    v10-AP1: per_source_watermark is passed to the reconciler mock.
    If None, a default per-source map is built from min_watermark and
    the hit source IDs (for backwards compatibility with single-source tests).
    """
    # Build default per-source map if not provided
    if per_source_watermark is None:
        if min_watermark is not None:
            per_source_watermark = {str(h.source.id): min_watermark for h in hits}
        else:
            per_source_watermark = {}

    # Reconciler mock: wait_for_convergence → (degraded, staleness, min_watermark, per_source_wm)
    mock_reconciler = MagicMock()
    mock_reconciler.wait_for_convergence = AsyncMock(
        return_value=([], None, min_watermark, per_source_watermark)
    )

    class _FakeVS:
        async def search(
            self,
            *,
            request: SearchRequest,
            workspace_id: uuid.UUID,
            as_of: datetime | None = None,
        ) -> SearchResult:
            return _make_result(hits)

    class _FakeLegacy:
        async def search(self, request: SearchRequest) -> SearchResult:
            return _make_result([])

    composer = GraphRAGComposer.__new__(GraphRAGComposer)
    composer._graphrag_active = True  # type: ignore[attr-defined]
    composer._global_reconciler = mock_reconciler  # type: ignore[attr-defined]
    composer._session_factory = None  # type: ignore[attr-defined]
    composer._is_entity_parked_fn = None  # type: ignore[attr-defined]
    composer._vector_store = _FakeVS()  # type: ignore[attr-defined]
    composer._graph_store = MagicMock()  # type: ignore[attr-defined]
    composer._graph_store.traverse = AsyncMock(side_effect=ValueError("entity_not_found"))
    composer._legacy_service = _FakeLegacy()  # type: ignore[attr-defined]
    return composer


# ---------------------------------------------------------------------------
# Test 1: watermark=7, hits with applied_version {5,7,8,10} → only ≤7 survive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watermark_filters_future_epoch_hits() -> None:
    """Hits with applied_version > watermark are dropped; pinned_watermark is stamped.

    Input versions: 5, 7, 8, 10. Watermark: 7.
    Expected survivors: versions 5 and 7.
    """
    src_id = uuid.uuid4()
    hits = [
        _make_hit(applied_version=5, text="v5", source_id=src_id),
        _make_hit(applied_version=7, text="v7", source_id=src_id),
        _make_hit(applied_version=8, text="v8", source_id=src_id),  # must be dropped
        _make_hit(applied_version=10, text="v10", source_id=src_id),  # must be dropped
    ]
    composer = _make_composer_with_reconciler(
        min_watermark=7,
        hits=hits,
        per_source_watermark={str(src_id): 7},
    )
    req = SearchRequest(query="test", top_k=10)
    result = await composer.search(req, workspace_id=_WS)

    # All surviving hits must be ≤ watermark
    surviving_versions = [h.applied_version for h in result.hits]
    assert all(v is None or v <= 7 for v in surviving_versions), (
        f"Future-epoch hits leaked into result: {surviving_versions}"
    )

    # Specifically: v8 and v10 must be gone
    assert 8 not in surviving_versions, "Hit with applied_version=8 must be dropped"
    assert 10 not in surviving_versions, "Hit with applied_version=10 must be dropped"

    # v5 and v7 must survive
    assert 5 in surviving_versions, "Hit with applied_version=5 must survive"
    assert 7 in surviving_versions, "Hit with applied_version=7 must survive"

    # pinned_watermark must be stamped on the result envelope
    assert result.pinned_watermark == 7, (
        f"pinned_watermark must be 7; got {result.pinned_watermark}"
    )


# ---------------------------------------------------------------------------
# Test 2: watermark None → all hits pass; pinned_watermark is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_none_watermark_passes_all_hits() -> None:
    """When watermark is None (cold store or no reconciler), no filtering occurs."""
    hits = [
        _make_hit(applied_version=1),
        _make_hit(applied_version=100),
        _make_hit(applied_version=None),
    ]
    composer = _make_composer_with_reconciler(
        min_watermark=None,
        hits=hits,
        per_source_watermark={},
    )
    req = SearchRequest(query="test", top_k=10)
    result = await composer.search(req, workspace_id=_WS)

    # All 3 hits must be present (order may differ after scoring)
    assert len(result.hits) == 3, (
        f"All 3 hits must pass when watermark is None; got {len(result.hits)}"
    )
    assert result.pinned_watermark is None, (
        "pinned_watermark must be None when no watermark available"
    )


# ---------------------------------------------------------------------------
# Test 3: converged watermark=10, all hits ≤10 → all pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_converged_watermark_passes_all_current_hits() -> None:
    """When all stores are converged and all hits are at or below watermark, nothing is dropped."""
    src_id = uuid.uuid4()
    hits = [
        _make_hit(applied_version=1, source_id=src_id),
        _make_hit(applied_version=5, source_id=src_id),
        _make_hit(applied_version=10, source_id=src_id),
        _make_hit(applied_version=None, source_id=src_id),
    ]
    composer = _make_composer_with_reconciler(
        min_watermark=10,
        hits=hits,
        per_source_watermark={str(src_id): 10},
    )
    req = SearchRequest(query="test", top_k=10)
    result = await composer.search(req, workspace_id=_WS)

    surviving_versions = [h.applied_version for h in result.hits]
    assert all(v is None or v <= 10 for v in surviving_versions), (
        f"Unexpected version found: {surviving_versions}"
    )
    assert len(result.hits) == 4, (
        f"All 4 hits should survive with watermark=10; got {len(result.hits)}"
    )
    assert result.pinned_watermark == 10


# ---------------------------------------------------------------------------
# Test 4: cold store watermark=0, hits applied_version=1 → result empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cold_store_watermark_zero_drops_all_versioned_hits() -> None:
    """Cold store with watermark=0: any hit with applied_version >= 1 is dropped.

    A watermark of 0 means no store has applied anything — returning any
    versioned chunk would be mixed-epoch.  Only None-versioned hits survive.
    """
    src_id = uuid.uuid4()
    hits = [
        _make_hit(applied_version=1, text="v1", source_id=src_id),  # must be dropped: 1 > 0
        _make_hit(applied_version=None, text="unversioned", source_id=src_id),  # must survive
    ]
    composer = _make_composer_with_reconciler(
        min_watermark=0,
        hits=hits,
        per_source_watermark={str(src_id): 0},
    )
    req = SearchRequest(query="test", top_k=10)
    result = await composer.search(req, workspace_id=_WS)

    # applied_version=1 must be dropped (1 > 0)
    versioned = [h for h in result.hits if h.applied_version is not None]
    assert versioned == [], (
        f"No versioned hit should survive with watermark=0; "
        f"got {[h.applied_version for h in versioned]}"
    )
    # pinned_watermark must be 0
    assert result.pinned_watermark == 0, (
        f"pinned_watermark must be 0; got {result.pinned_watermark}"
    )


# ---------------------------------------------------------------------------
# Test 5: _apply_watermark_filter pure-function boundary test (per-source map)
# ---------------------------------------------------------------------------


def test_apply_watermark_filter_pure_function() -> None:
    """Unit test of _apply_watermark_filter as a pure function (per-source map).

    Verifies all boundary conditions with per-source watermark dict:
    - Hits with applied_version > their source's watermark → dropped.
    - Hits with applied_version == watermark → retained.
    - Hits with applied_version < watermark → retained.
    - Hits with applied_version is None → always retained.
    - Per-source map None or empty → all hits retained (no filter).
    - Source not in map → hit passes (no blackout for unknown sources).
    - Two sources with different watermarks are evaluated independently.
    """
    src_x = uuid.uuid4()
    src_y = uuid.uuid4()
    src_unknown = uuid.uuid4()

    h_none = _make_hit(applied_version=None, source_id=src_x)
    h_v0 = _make_hit(applied_version=0, source_id=src_x)
    h_v5 = _make_hit(applied_version=5, source_id=src_x)
    h_v7 = _make_hit(applied_version=7, source_id=src_x)
    h_v8 = _make_hit(applied_version=8, source_id=src_x)
    h_v10 = _make_hit(applied_version=10, source_id=src_x)

    all_hits_x = [h_none, h_v0, h_v5, h_v7, h_v8, h_v10]

    # --- watermark map None: no filter ---
    result = _apply_watermark_filter(all_hits_x, None)
    assert result == all_hits_x, "watermark=None must return all hits unchanged"

    # --- watermark map empty: no filter ---
    result = _apply_watermark_filter(all_hits_x, {})
    assert result == all_hits_x, "empty watermark map must return all hits unchanged"

    # --- single source watermark = 7: drop v8 and v10 ---
    wm = {str(src_x): 7}
    result = _apply_watermark_filter(all_hits_x, wm)
    surviving = {h.applied_version for h in result}
    assert surviving == {None, 0, 5, 7}, f"Expected {{None,0,5,7}}; got {surviving}"

    # --- single source watermark = 0: drop v5, v7, v8, v10; keep None and v0 ---
    wm = {str(src_x): 0}
    result = _apply_watermark_filter(all_hits_x, wm)
    surviving = {h.applied_version for h in result}
    assert surviving == {None, 0}, f"Expected {{None,0}}; got {surviving}"

    # --- single source watermark = 10: all pass ---
    wm = {str(src_x): 10}
    result = _apply_watermark_filter(all_hits_x, wm)
    assert result == all_hits_x, "watermark=10 must retain all hits"

    # --- exact boundary: watermark == applied_version is retained ---
    result = _apply_watermark_filter([h_v7], {str(src_x): 7})
    assert len(result) == 1, "Hit at exactly the watermark must be retained"

    # --- one above boundary: dropped ---
    result = _apply_watermark_filter([h_v8], {str(src_x): 7})
    assert result == [], "Hit at watermark+1 must be dropped"

    # --- empty input: always returns empty ---
    assert _apply_watermark_filter([], {str(src_x): 5}) == []
    assert _apply_watermark_filter([], None) == []

    # --- source not in map: hit passes (no blackout) ---
    h_unknown = _make_hit(applied_version=999, source_id=src_unknown)
    result = _apply_watermark_filter([h_unknown], {str(src_x): 7})
    assert len(result) == 1, "Hit from source not in watermark map must pass (no blackout)"

    # --- two-source independence: src_x@wm=7, src_y@wm=3 ---
    h_y_v2 = _make_hit(applied_version=2, source_id=src_y)
    h_y_v5 = _make_hit(applied_version=5, source_id=src_y)  # > src_y watermark of 3
    h_x_v6 = _make_hit(applied_version=6, source_id=src_x)  # <= src_x watermark of 7
    h_x_v9 = _make_hit(applied_version=9, source_id=src_x)  # > src_x watermark of 7
    mixed_hits = [h_y_v2, h_y_v5, h_x_v6, h_x_v9]
    wm = {str(src_x): 7, str(src_y): 3}
    result = _apply_watermark_filter(mixed_hits, wm)
    surviving_set = {(str(h.source.id), h.applied_version) for h in result}
    assert (str(src_y), 2) in surviving_set, "src_y v2 <= wm=3 must survive"
    assert (str(src_y), 5) not in surviving_set, "src_y v5 > wm=3 must be dropped"
    assert (str(src_x), 6) in surviving_set, "src_x v6 <= wm=7 must survive"
    assert (str(src_x), 9) not in surviving_set, "src_x v9 > wm=7 must be dropped"


# ---------------------------------------------------------------------------
# Test 6 (v10-AP1): multi-source — cold source B does NOT blackout source A
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_source_cold_source_does_not_blackout_healthy_source() -> None:
    """Regression guard for v10-AP1: a cold source must not black out healthy sources.

    Workspace has two sources:
    - Source A at v10 (healthy): its hits should be returned.
    - Source B at v0 (cold): its future-epoch hits should be dropped.

    With the OLD global-min watermark (v9 bug): min(10, 0) = 0 → ALL hits dropped.
    With the NEW per-source watermark (v10-AP1 fix): A@10 returned, B@>0 dropped.
    """
    src_a = uuid.uuid4()
    src_b = uuid.uuid4()

    hits = [
        _make_hit(applied_version=10, text="A-v10", source_id=src_a),  # healthy A
        _make_hit(applied_version=5, text="B-v5", source_id=src_b),  # cold B, should drop
        _make_hit(applied_version=None, text="A-unversioned", source_id=src_a),  # always pass
    ]
    # Per-source watermark: A is healthy at v10, B is cold at v0
    per_source_wm = {str(src_a): 10, str(src_b): 0}
    composer = _make_composer_with_reconciler(
        min_watermark=0,  # global min (old v9 floor) — should NOT be used for filtering
        hits=hits,
        per_source_watermark=per_source_wm,
    )
    req = SearchRequest(query="test", top_k=10)
    result = await composer.search(req, workspace_id=_WS)

    # Source A's hit at v10 MUST survive (v10 <= A's watermark of 10).
    src_a_hits = [h for h in result.hits if h.source.id == src_a]
    assert len(src_a_hits) >= 1, (
        f"Source A hits must survive; cold source B must not blackout A. "
        f"Got src_a_hits={[h.applied_version for h in src_a_hits]}, "
        f"all result hits={[(str(h.source.id)[:8], h.applied_version) for h in result.hits]}"
    )
    versioned_a = [h for h in src_a_hits if h.applied_version is not None]
    assert any(h.applied_version == 10 for h in versioned_a), "Source A hit at v10 must survive"

    # Source B's hit at v5 MUST be dropped (v5 > B's watermark of 0).
    src_b_hits = [h for h in result.hits if h.source.id == src_b]
    versioned_b = [h for h in src_b_hits if h.applied_version is not None]
    assert versioned_b == [], (
        f"Source B has watermark=0; versioned hits must be dropped; got {versioned_b}"
    )


@pytest.mark.asyncio
async def test_multi_source_unknown_source_not_blacked_out() -> None:
    """A hit from a source not in the watermark map passes through (no blackout).

    This handles the case where a new source ingested data but the reconciler
    has not yet seen its checkpoint.  We must not drop the hit — only sources
    with an explicit watermark entry are filtered.
    """
    src_known = uuid.uuid4()
    src_new = uuid.uuid4()

    hits = [
        _make_hit(applied_version=5, text="known-v5", source_id=src_known),
        _make_hit(applied_version=100, text="new-v100", source_id=src_new),  # no map entry
    ]
    per_source_wm = {str(src_known): 7}  # src_new is NOT in the map
    composer = _make_composer_with_reconciler(
        min_watermark=7,
        hits=hits,
        per_source_watermark=per_source_wm,
    )
    req = SearchRequest(query="test", top_k=10)
    result = await composer.search(req, workspace_id=_WS)

    new_src_hits = [h for h in result.hits if h.source.id == src_new]
    assert len(new_src_hits) == 1, "Hit from source not in watermark map must pass through"
    assert new_src_hits[0].applied_version == 100


# ---------------------------------------------------------------------------
# Test 8 (v10-AP2): graph-ahead related node excluded from anchor candidates
# ---------------------------------------------------------------------------


def test_apply_graph_watermark_filter_excludes_graph_ahead_related_nodes() -> None:
    """_apply_graph_watermark_filter removes related nodes ahead of their source watermark.

    Graph has seed (src_a, v5) and two related nodes:
    - node_ok (src_b, v3) — within src_b watermark of 5 → kept
    - node_ahead (src_c, v8) — ABOVE src_c watermark of 5 → excluded

    Invariant: no composed result mixes a graph node with version > watermark[source].
    """
    src_a = str(uuid.uuid4())
    src_b = str(uuid.uuid4())
    src_c = str(uuid.uuid4())

    seed = EntityNodeView(
        id=uuid.uuid4(),
        name="SeedEntity",
        kind="service",
        source=src_a,
        chunk_text=None,
        depth=0,
        version=5,
    )
    node_ok = EntityNodeView(
        id=uuid.uuid4(),
        name="OkEntity",
        kind="team",
        source=src_b,
        chunk_text=None,
        depth=1,
        version=3,
    )
    node_ahead = EntityNodeView(
        id=uuid.uuid4(),
        name="AheadEntity",
        kind="repo",
        source=src_c,
        chunk_text=None,
        depth=1,
        version=8,  # > src_c watermark of 5
    )
    edge_ok = GraphEdgeView(
        from_entity="SeedEntity",
        to_entity="OkEntity",
        edge_type="OWNS",
    )
    edge_ahead = GraphEdgeView(
        from_entity="SeedEntity",
        to_entity="AheadEntity",
        edge_type="USES",
    )

    graph_result = GraphResultView(
        seed=seed,
        related=[node_ok, node_ahead],
        edges=[edge_ok, edge_ahead],
    )

    per_source_wm = {src_a: 10, src_b: 5, src_c: 5}
    filtered = _apply_graph_watermark_filter(graph_result, per_source_wm)

    # OkEntity (v3 <= src_b wm=5) must remain
    remaining_names = [n.name for n in filtered.related]
    assert "OkEntity" in remaining_names, "OkEntity within watermark must be kept"

    # AheadEntity (v8 > src_c wm=5) must be excluded
    assert "AheadEntity" not in remaining_names, (
        "AheadEntity version>watermark must be excluded from graph anchor result"
    )

    # Edge to AheadEntity must be pruned
    remaining_edge_targets = [e.to_entity for e in filtered.edges]
    assert "AheadEntity" not in remaining_edge_targets, "Edge to excluded node must be pruned"

    # Edge to OkEntity must remain
    assert "OkEntity" in remaining_edge_targets, "Edge to OkEntity must remain"


def test_apply_graph_watermark_filter_node_version_none_passes() -> None:
    """A node with version=None is not subject to version ceiling (passes through)."""
    src = str(uuid.uuid4())
    seed = EntityNodeView(
        id=uuid.uuid4(),
        name="Seed",
        kind="service",
        source=src,
        chunk_text=None,
        depth=0,
        version=None,  # no version info
    )
    node = EntityNodeView(
        id=uuid.uuid4(),
        name="Related",
        kind="team",
        source=src,
        chunk_text=None,
        depth=1,
        version=None,  # no version info
    )
    graph_result = GraphResultView(seed=seed, related=[node], edges=[])
    per_source_wm = {src: 0}  # even ceiling=0 must not drop None-versioned nodes
    filtered = _apply_graph_watermark_filter(graph_result, per_source_wm)
    assert len(filtered.related) == 1, "version=None node must not be excluded"


def test_apply_graph_watermark_filter_source_not_in_map_passes() -> None:
    """Node whose source is not in the watermark map passes through (no ceiling)."""
    src_mapped = str(uuid.uuid4())
    src_unmapped = str(uuid.uuid4())

    seed = EntityNodeView(
        id=uuid.uuid4(),
        name="Seed",
        kind="service",
        source=src_mapped,
        chunk_text=None,
        depth=0,
        version=3,
    )
    node_unmapped = EntityNodeView(
        id=uuid.uuid4(),
        name="Unmapped",
        kind="repo",
        source=src_unmapped,
        chunk_text=None,
        depth=1,
        version=999,  # very high version, but source not in map → passes
    )
    graph_result = GraphResultView(seed=seed, related=[node_unmapped], edges=[])
    per_source_wm = {src_mapped: 10}  # src_unmapped not in map
    filtered = _apply_graph_watermark_filter(graph_result, per_source_wm)
    assert len(filtered.related) == 1, "Unmapped source node must not be excluded"


# ---------------------------------------------------------------------------
# Test 9 (v10-AP2): seed node version > watermark → anchor treated as miss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_ahead_seed_node_treated_as_anchor_miss() -> None:
    """When the anchor seed node is graph-ahead (version > watermark), the anchor is a miss.

    This prevents a graph-ahead seed from polluting the candidate set and
    injecting graph-v10 entity context into a v7-evidence composed result.
    """
    src_id = uuid.uuid4()
    src_str = str(src_id)

    # Seed entity at version 10, but per-source watermark is 7.
    seed = EntityNodeView(
        id=uuid.uuid4(),
        name="AheadSeed",
        kind="service",
        source=src_str,
        chunk_text=None,
        depth=0,
        version=10,  # > watermark of 7 → should be excluded
    )
    graph_result = GraphResultView(seed=seed, related=[], edges=[])

    # Vector hits — should still be returned (no global blackout).
    hit_a = _make_hit(applied_version=7, text="v7-evidence", source_id=src_id)

    per_source_wm = {src_str: 7}

    mock_reconciler = MagicMock()
    mock_reconciler.wait_for_convergence = AsyncMock(return_value=([], None, 7, per_source_wm))

    class _FakeVS:
        async def search(
            self,
            *,
            request: SearchRequest,
            workspace_id: uuid.UUID,
            as_of: datetime | None = None,
        ) -> SearchResult:
            return _make_result([hit_a])

    class _FakeLegacy:
        async def search(self, request: SearchRequest) -> SearchResult:
            return _make_result([])

    composer = GraphRAGComposer.__new__(GraphRAGComposer)
    composer._graphrag_active = True  # type: ignore[attr-defined]
    composer._global_reconciler = mock_reconciler  # type: ignore[attr-defined]
    composer._session_factory = None  # type: ignore[attr-defined]
    composer._is_entity_parked_fn = None  # type: ignore[attr-defined]
    composer._vector_store = _FakeVS()  # type: ignore[attr-defined]
    # graph_store.traverse returns the graph-ahead seed
    graph_store_mock = MagicMock()
    graph_store_mock.traverse = AsyncMock(return_value=graph_result)
    composer._graph_store = graph_store_mock  # type: ignore[attr-defined]
    composer._legacy_service = _FakeLegacy()  # type: ignore[attr-defined]

    req = SearchRequest(
        query="test anchor",
        top_k=10,
        filters={"graphrag_anchor": "AheadSeed"},
    )
    result = await composer.search(req, workspace_id=_WS)

    # The anchor seed was graph-ahead → treated as miss → no candidate set from graph.
    # Vector evidence at v7 (within watermark) must still be returned.
    assert len(result.hits) >= 1, (
        "Vector evidence within watermark must still be returned even when anchor is graph-ahead"
    )
    surviving_versions = [h.applied_version for h in result.hits]
    assert all(v is None or v <= 7 for v in surviving_versions), (
        "No hit should exceed watermark=7 after graph-ahead anchor rejection: "
        f"{surviving_versions}"
    )
