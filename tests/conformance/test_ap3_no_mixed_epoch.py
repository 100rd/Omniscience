"""AP3 / v10-AP1 / v10-AP2 / v11-AP1 / v13-AP1/AP2 conformance tests — no mixed-epoch evidence.

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

v11-AP1 invariant (fail-closed epoch policy): a hit whose source has NO
entry in the per_source_watermark map and has a non-None applied_version is
DROPPED (strict_epoch=True, the default).  Only applied_version=None hits
from unmapped sources pass through.  The old fail-open behaviour (pass-through
for unmapped sources) is available via strict_epoch=False (not recommended
for production — see ADR-0017).

v13-AP2 invariant (canonical no-reconciler signal): a composer with
_global_reconciler=None must initialise per_source_watermark=None so the
None-bypass in _apply_watermark_filter fires and ALL hits pass.  The old
default of {} (empty dict) was wrong: it caused strict_epoch=True to drop
all versioned hits even when no reconciler was wired.

v13-AP1 invariant (reason-labeled observability): _EPOCH_DROPS counter
increments with correct (reason, filter) labels at every drop site.
Tests use DELTA assertions (before/after) to avoid accumulation across
test runs on the shared default registry.

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
7. [v11-AP1 INVERTED from v10] unmapped-source hit with applied_version=100
   MUST be DROPPED (fail-closed, strict_epoch=True).  Only applied_version=None
   from unmapped sources passes through.
8. [v10-AP2] anchor node version > source watermark → excluded from anchor result
   (graph-ahead mixed-epoch prevented).
9. [v10-AP2] anchor seed node version > source watermark → treated as anchor miss.
10. [v13-AP2] no reconciler → per_source_watermark=None → all hits pass, no _EPOCH_DROPS.
11. [v13-AP1/AP2] cold workspace {} → versioned hit dropped + empty_map_blackout +1
    AND unmapped +1.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_core.storage.graph import EntityNodeView, GraphEdgeView, GraphResultView
from omniscience_retrieval.graph_rag import (
    _EPOCH_DROPS,
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


_SENTINEL: object = object()  # sentinel object for "not provided" default


def _make_composer_with_reconciler(
    min_watermark: int | None,
    hits: list[SearchHit],
    per_source_watermark: dict[str, int] | None = _SENTINEL,  # type: ignore[arg-type,assignment]
    *,
    strict_epoch: bool = True,
) -> GraphRAGComposer:
    """Build a GraphRAGComposer whose reconciler returns the given watermark.

    v10-AP1: per_source_watermark is passed to the reconciler mock.
    If not explicitly provided (sentinel), a default per-source map is built
    from min_watermark and the hit source IDs (for backwards compatibility
    with single-source tests).

    v11-AP1: strict_epoch (default True) is forwarded to the composer so
    tests can assert both fail-closed (default) and fail-open behaviour.

    v12-AP1: per_source_watermark=None is now a valid explicit value meaning
    "no reconciler" (pass None to the mock).  The sentinel distinguishes
    "not provided" from explicit None.
    """
    # Build default per-source map only when per_source_watermark was not
    # explicitly provided by the caller (sentinel detection).
    if per_source_watermark is _SENTINEL:
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
    composer._strict_epoch = strict_epoch  # type: ignore[attr-defined]
    composer._vector_store = _FakeVS()  # type: ignore[attr-defined]
    composer._graph_store = MagicMock()  # type: ignore[attr-defined]
    composer._graph_store.traverse = AsyncMock(side_effect=ValueError("entity_not_found"))
    composer._legacy_service = _FakeLegacy()  # type: ignore[attr-defined]
    return composer


def _make_composer_no_reconciler(
    hits: list[SearchHit],
    *,
    strict_epoch: bool = True,
) -> GraphRAGComposer:
    """Build a GraphRAGComposer with NO global reconciler.

    v13-AP2: _global_reconciler=None → per_source_watermark stays None in search()
    → _apply_watermark_filter None-bypass → ALL hits pass regardless of
    applied_version.  This is the canonical no-reconciler path.

    No mock reconciler is wired; _global_reconciler is explicitly None.
    """

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
    composer._global_reconciler = None  # type: ignore[attr-defined]  # v13-AP2: explicit None
    composer._session_factory = None  # type: ignore[attr-defined]
    composer._is_entity_parked_fn = None  # type: ignore[attr-defined]
    composer._strict_epoch = strict_epoch  # type: ignore[attr-defined]
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
    """per_source_watermark=None (no reconciler wired) passes all hits unchanged.

    v12-AP1 clarification: this test uses per_source_watermark=None, which is the
    legitimate "no reconciler" sentinel.  The old test used per_source_watermark={}
    (empty dict) — that was the AP1 bug: an empty map silently bypassed the fail-closed
    filter, allowing all versioned hits to pass even when the reconciler had run but
    found no sources.  The empty-map case is now covered by
    test_empty_map_fail_closed_drops_versioned_hits (v12-AP1 regression test).

    v13-AP2: use _make_composer_no_reconciler so _global_reconciler=None and the
    None initialisation path in search() is exercised (not just the pure function).
    """
    hits = [
        _make_hit(applied_version=1),
        _make_hit(applied_version=100),
        _make_hit(applied_version=None),
    ]
    # v13-AP2: use the no-reconciler helper (sets _global_reconciler=None)
    composer = _make_composer_no_reconciler(hits)
    req = SearchRequest(query="test", top_k=10)
    result = await composer.search(req, workspace_id=_WS)

    # All 3 hits must be present (order may differ after scoring)
    assert len(result.hits) == 3, (
        f"All 3 hits must pass when per_source_watermark is None; got {len(result.hits)}"
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
    - Per-source map None → all hits retained (no reconciler wired — live mode).
    - Per-source map empty ({}) + strict_epoch=True → only av=None hits pass
      (fail-closed; all versioned hits from unmapped sources are DROPPED).
    - Per-source map empty ({}) + strict_epoch=False → all hits pass (legacy).
    - Source not in map + strict_epoch=True → versioned hit DROPPED (fail-closed).
    - Source not in map + applied_version=None → always passes (no version claim).
    - Source not in map + strict_epoch=False → hit passes (legacy fail-open).
    - Two sources with different watermarks are evaluated independently.

    v13-AP1 additions: delta-assert that _EPOCH_DROPS fires on unmapped-drop
    and does NOT fire on pass-through or None-watermark paths.

    Semantics summary (v11/v12/v13 fail-closed):
    - None map → all hits pass (bypass: no reconciler).
    - Empty map ({}) + strict → only av=None passes (cold workspace fail-closed).
    - Source not in map + versioned + strict → DROPPED (unmapped fail-closed).
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

    # --- watermark map None: no filter (no reconciler wired) ---
    before_unmapped = _EPOCH_DROPS.labels(reason="unmapped", filter="hit")._value.get()
    result = _apply_watermark_filter(all_hits_x, None)
    assert result == all_hits_x, "watermark=None must return all hits unchanged"
    # No counter increments on None path.
    assert _EPOCH_DROPS.labels(reason="unmapped", filter="hit")._value.get() == before_unmapped, (
        "None watermark path must not increment _EPOCH_DROPS"
    )

    # --- watermark map empty (v12-AP1 fix): strict_epoch=True → only None-versioned pass ---
    # Empty dict means "reconciler ran but found no sources in any store".  Under
    # strict_epoch=True (default), every versioned hit is from an unmapped source and
    # must be DROPPED fail-closed.  Only applied_version=None hits pass through.
    # (Old behavior: all hits passed — this was the AP1 empty-map bypass bug.)
    result_strict_empty = _apply_watermark_filter(all_hits_x, {}, strict_epoch=True)
    surviving_strict = {h.applied_version for h in result_strict_empty}
    assert surviving_strict == {None}, (
        f"v12-AP1: empty map + strict_epoch=True must only pass None-versioned hits; "
        f"got {surviving_strict}"
    )
    # Under strict_epoch=False (legacy): all hits pass (no ceiling for unmapped sources)
    result_open_empty = _apply_watermark_filter(all_hits_x, {}, strict_epoch=False)
    assert result_open_empty == all_hits_x, (
        "empty map + strict_epoch=False (legacy) must return all hits unchanged"
    )

    # --- single source watermark = 7: drop v8 and v10 ---
    wm = {str(src_x): 7}
    result = _apply_watermark_filter(all_hits_x, wm)
    surviving = {h.applied_version for h in result}
    assert surviving == {None, 0, 5, 7}, f"Expected {{None,0,5,7}}; got {surviving}"

    # --- single source watermark = 0: drop v5, v7, v8, v10; keep None and v0 ---
    # BOUNDARY: applied_version=0, wm=0 → 0 <= 0 → PASS (epoch-0 consistent read).
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

    # --- source not in map, strict_epoch=True (default): versioned hit DROPPED (fail-closed) ---
    h_unknown = _make_hit(applied_version=999, source_id=src_unknown)
    before_unmapped2 = _EPOCH_DROPS.labels(reason="unmapped", filter="hit")._value.get()
    result_strict = _apply_watermark_filter([h_unknown], {str(src_x): 7}, strict_epoch=True)
    assert result_strict == [], (
        "v11-AP1 fail-closed: versioned hit from unmapped source must be dropped"
    )
    # v13-AP1: unmapped drop must increment counter.
    assert (
        _EPOCH_DROPS.labels(reason="unmapped", filter="hit")._value.get() == before_unmapped2 + 1
    ), "v13-AP1: unmapped drop must increment _EPOCH_DROPS(reason=unmapped, filter=hit)"

    # --- source not in map, applied_version=None: always passes (no version info) ---
    h_unknown_none = _make_hit(applied_version=None, source_id=src_unknown)
    before_unmapped3 = _EPOCH_DROPS.labels(reason="unmapped", filter="hit")._value.get()
    result_none = _apply_watermark_filter([h_unknown_none], {str(src_x): 7}, strict_epoch=True)
    assert len(result_none) == 1, "applied_version=None from unmapped source must always pass"
    # No counter increment for None-version pass.
    assert _EPOCH_DROPS.labels(reason="unmapped", filter="hit")._value.get() == before_unmapped3, (
        "None-version pass must not increment _EPOCH_DROPS"
    )

    # --- source not in map, strict_epoch=False (legacy fail-open): versioned hit passes ---
    result_open = _apply_watermark_filter([h_unknown], {str(src_x): 7}, strict_epoch=False)
    assert len(result_open) == 1, (
        "strict_epoch=False (legacy): versioned hit from unmapped source must pass"
    )

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
async def test_unmapped_source_versioned_hit_dropped_fail_closed() -> None:
    """v11-AP1 fail-closed: an unmapped-source hit with applied_version=100 MUST be DROPPED.

    Intent (v11-AP1 inversion of old test 7): the old behaviour allowed a
    versioned hit from a source absent from the per_source_watermark map to
    pass through (fail-open).  v11-AP1 closes this leak — with strict_epoch=True
    (the default), a versioned hit from an unmapped source is DROP-ed to prevent
    mixed-epoch evidence.  Only applied_version=None hits from unmapped sources
    are allowed through (no version info → no ceiling to enforce).

    Regression guard: this test MUST remain green.  Reverting to fail-open
    (strict_epoch=False) is a deliberate opt-in — not the default.
    """
    src_known = uuid.uuid4()
    src_new = uuid.uuid4()

    hits = [
        _make_hit(applied_version=5, text="known-v5", source_id=src_known),
        _make_hit(applied_version=100, text="new-v100", source_id=src_new),  # no map entry
        _make_hit(applied_version=None, text="new-unversioned", source_id=src_new),  # must pass
    ]
    per_source_wm = {str(src_known): 7}  # src_new is NOT in the map
    composer = _make_composer_with_reconciler(
        min_watermark=7,
        hits=hits,
        per_source_watermark=per_source_wm,
        strict_epoch=True,  # v11-AP1 default: fail-closed
    )
    req = SearchRequest(query="test", top_k=10)
    result = await composer.search(req, workspace_id=_WS)

    # The versioned hit at v=100 from an unmapped source MUST be DROPPED (fail-closed).
    new_src_versioned_hits = [
        h for h in result.hits if h.source.id == src_new and h.applied_version is not None
    ]
    assert new_src_versioned_hits == [], (
        "v11-AP1 fail-closed: versioned hit from unmapped source (applied_version=100) "
        f"must be DROPPED; got {[(h.applied_version,) for h in new_src_versioned_hits]}"
    )

    # Unversioned hit from unmapped source MUST still pass (no version → no ceiling).
    new_src_none_hits = [
        h for h in result.hits if h.source.id == src_new and h.applied_version is None
    ]
    assert len(new_src_none_hits) == 1, (
        "applied_version=None from unmapped source must pass through (no version to pin)"
    )


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
    # v11-AP2: _apply_graph_watermark_filter now returns (filtered_result, seed_excluded)
    filtered, seed_excluded = _apply_graph_watermark_filter(graph_result, per_source_wm)
    assert not seed_excluded, "Seed is within watermark (v5 <= wm=10); must not be excluded"

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
    # v11-AP2: returns (filtered_result, seed_excluded)
    filtered, seed_excluded = _apply_graph_watermark_filter(graph_result, per_source_wm)
    assert not seed_excluded, "version=None seed must not be excluded"
    assert len(filtered.related) == 1, "version=None node must not be excluded"


def test_apply_graph_watermark_filter_source_not_in_map_passes() -> None:
    """Node whose source is not in the watermark map passes through (no ceiling).

    v13-AP1 design note: graph nodes from unmapped sources intentionally PASS.
    Only _apply_watermark_filter (vector-hit filter) enforces fail-closed drops
    for unmapped sources.  The graph filter only applies version ceilings for
    sources with known watermarks.  This is by design — do NOT add an 'unmapped'
    counter for graph nodes.
    """
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
    # v11-AP2: returns (filtered_result, seed_excluded)
    # Note: _apply_graph_watermark_filter (graph-node filter) does not drop unmapped
    # graph nodes — only _apply_watermark_filter (vector-hit filter) does the strict_epoch
    # drop.  Graph nodes from unmapped sources pass through (no ceiling info for them).
    filtered, seed_excluded = _apply_graph_watermark_filter(graph_result, per_source_wm)
    assert not seed_excluded, "Seed source is in map and within watermark"
    assert len(filtered.related) == 1, "Unmapped source graph node must not be excluded"


# ---------------------------------------------------------------------------
# Test 9 (v10-AP2): seed node version > watermark → anchor treated as miss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_ahead_seed_node_treated_as_anchor_miss() -> None:
    """When the anchor seed node is graph-ahead (version > watermark), the anchor is a miss.

    This prevents a graph-ahead seed from polluting the candidate set and
    injecting graph-v10 entity context into a v7-evidence composed result.

    v13-AP1: assert _EPOCH_DROPS counter does NOT increment for the seed-exclusion
    path (the seed is excluded by returning True from _apply_graph_watermark_filter;
    the counter is not fired there — the anchor-miss outcome is the observable signal).
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
    composer._strict_epoch = True  # type: ignore[attr-defined]
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
    # v13-AP1: seed exclusion must NOT increment _EPOCH_DROPS(filter="node")
    before_node_lag = _EPOCH_DROPS.labels(reason="lag", filter="node")._value.get()
    before_node_cold = _EPOCH_DROPS.labels(reason="cold_zero", filter="node")._value.get()

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
    # Seed-excluded path does NOT fire node-filter counters.
    assert _EPOCH_DROPS.labels(reason="lag", filter="node")._value.get() == before_node_lag, (
        "v13-AP1: seed-excluded path must not increment lag/node counter"
    )
    assert (
        _EPOCH_DROPS.labels(reason="cold_zero", filter="node")._value.get() == before_node_cold
    ), "v13-AP1: seed-excluded path must not increment cold_zero/node counter"


# ---------------------------------------------------------------------------
# v12-AP1 regression: empty map + strict_epoch=True → versioned hits DROPPED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_map_fail_closed_drops_versioned_hits() -> None:
    """v12-AP1 regression: empty per_source_watermark + strict_epoch=True is FAIL-CLOSED.

    An empty dict ({}) means the reconciler RAN but found no sources in any store
    (cold start / no data yet).  Before the v12-AP1 fix, ``if not per_source_watermark``
    short-circuited on both None AND {} identically, allowing ALL versioned hits to
    pass — bypassing the fail-closed contract silently on cold start.

    After the fix, ``per_source_watermark is None`` is the only bypass.  An empty
    map flows through the per-hit logic: every hit from a source not in the map
    (i.e., every hit, since the map is empty) is evaluated:
    - applied_version is None → passes (no version info, no ceiling to enforce).
    - applied_version is not None → DROPPED (fail-closed: source unmapped).

    v13-AP1: delta-assert _EPOCH_DROPS(reason="unmapped", filter="hit") fires on drop.

    Variants tested:
    1. empty map + strict_epoch=True + versioned hit → DROPPED.
    2. empty map + strict_epoch=True + None-versioned hit → passes.
    3. per_source_watermark=None (no reconciler) + versioned hit → passes.
    """
    src_id = uuid.uuid4()
    hit_versioned = _make_hit(applied_version=42, source_id=src_id)
    hit_none_versioned = _make_hit(applied_version=None, source_id=src_id)

    # --- Variant 1: empty map + strict_epoch=True + versioned hit → DROPPED ---
    before_unmapped = _EPOCH_DROPS.labels(reason="unmapped", filter="hit")._value.get()
    result = _apply_watermark_filter([hit_versioned], {}, strict_epoch=True)
    assert result == [], (
        "v12-AP1 regression: empty per_source_watermark + strict_epoch=True + "
        "versioned hit must be DROPPED (fail-closed cold-start bypass was the bug)"
    )
    # v13-AP1: unmapped drop must fire counter.
    assert (
        _EPOCH_DROPS.labels(reason="unmapped", filter="hit")._value.get() == before_unmapped + 1
    ), "v13-AP1: empty-map drop must increment _EPOCH_DROPS(reason=unmapped, filter=hit)"

    # --- Variant 2: empty map + strict_epoch=True + None-versioned hit → passes ---
    before_unmapped2 = _EPOCH_DROPS.labels(reason="unmapped", filter="hit")._value.get()
    result = _apply_watermark_filter([hit_none_versioned], {}, strict_epoch=True)
    assert len(result) == 1, (
        "v12-AP1: applied_version=None from unmapped source must always pass "
        "(no version info → no ceiling to enforce)"
    )
    # None-version pass must not fire counter.
    assert _EPOCH_DROPS.labels(reason="unmapped", filter="hit")._value.get() == before_unmapped2, (
        "v13-AP1: None-version pass must not increment counter"
    )

    # --- Variant 3: None map (no reconciler) + versioned hit → passes ---
    before_unmapped3 = _EPOCH_DROPS.labels(reason="unmapped", filter="hit")._value.get()
    result = _apply_watermark_filter([hit_versioned], None)
    assert len(result) == 1, (
        "per_source_watermark=None (no reconciler wired) must pass all hits unchanged — "
        "this is the legitimate no-reconciler path"
    )
    # None-watermark path must not fire counter.
    assert _EPOCH_DROPS.labels(reason="unmapped", filter="hit")._value.get() == before_unmapped3, (
        "v13-AP1: None watermark path must not increment counter"
    )


@pytest.mark.asyncio
async def test_empty_map_fail_closed_end_to_end() -> None:
    """v12-AP1 end-to-end: composer with empty per_source_watermark drops versioned hits.

    Exercises the full composer path (not just the pure function) to verify the
    empty-map fix propagates correctly through _run_merge_stage.

    Setup: reconciler returns per_source_watermark={} (empty map), strict_epoch=True.
    Three hits: versioned from src_a, versioned from src_b, None-versioned from src_a.

    Expected: both versioned hits DROPPED; None-versioned hit passes.

    v13-AP1: delta-assert empty_map_blackout fires ONCE per request +
    unmapped fires per dropped versioned hit.
    """
    src_a = uuid.uuid4()
    src_b = uuid.uuid4()

    hits = [
        _make_hit(applied_version=5, text="versioned-a", source_id=src_a),
        _make_hit(applied_version=10, text="versioned-b", source_id=src_b),
        _make_hit(applied_version=None, text="unversioned-a", source_id=src_a),
    ]
    composer = _make_composer_with_reconciler(
        min_watermark=None,
        hits=hits,
        per_source_watermark={},  # reconciler ran, no sources found
        strict_epoch=True,
    )
    req = SearchRequest(query="test", top_k=10)

    # v13-AP1: capture counters before call.
    before_blackout = _EPOCH_DROPS.labels(reason="empty_map_blackout", filter="hit")._value.get()
    before_unmapped = _EPOCH_DROPS.labels(reason="unmapped", filter="hit")._value.get()

    result = await composer.search(req, workspace_id=_WS)

    # All versioned hits must be dropped (fail-closed, empty map).
    versioned_hits = [h for h in result.hits if h.applied_version is not None]
    assert versioned_hits == [], (
        f"v12-AP1 end-to-end: empty map + strict_epoch=True must drop ALL versioned hits; "
        f"got {[(h.applied_version, h.text) for h in versioned_hits]}"
    )
    # None-versioned hit must still pass.
    none_versioned = [h for h in result.hits if h.applied_version is None]
    assert len(none_versioned) == 1, (
        f"v12-AP1: None-versioned hit must pass through empty-map fail-closed filter; "
        f"got {none_versioned}"
    )

    # v13-AP1: empty_map_blackout fires ONCE per request.
    assert (
        _EPOCH_DROPS.labels(reason="empty_map_blackout", filter="hit")._value.get()
        == before_blackout + 1
    ), "v13-AP1: empty_map_blackout must fire exactly once per request with empty map"

    # v13-AP1: unmapped fires once per dropped versioned hit (2 versioned hits dropped).
    assert (
        _EPOCH_DROPS.labels(reason="unmapped", filter="hit")._value.get() == before_unmapped + 2
    ), "v13-AP1: unmapped must fire once per dropped versioned hit (2 hits dropped)"


# ---------------------------------------------------------------------------
# v12-AP3 regression: undirected BFS matches undirected Neo4j traverse semantics
# ---------------------------------------------------------------------------


def test_recompute_reachability_undirected_keeps_reverse_edge_nodes() -> None:
    """v12-AP3: _recompute_reachability BFS is undirected — matches Neo4j traverse.

    Neo4j traverse() uses the Cypher pattern (seed)-[rels*]-(neighbour) WITHOUT
    an arrowhead, so it follows edges in BOTH directions.  The BFS in
    _recompute_reachability must therefore also be undirected, or nodes
    reachable only via reverse-direction edges would be incorrectly pruned here
    after being legitimately included by traverse().

    This test verifies: a node reachable ONLY via a reverse edge (i.e., the
    stored edge direction is B→Seed, but BFS from Seed follows it backwards to B)
    IS retained when the BFS is undirected, as required.

    Topology:
        Seed → A  (forward edge: from_entity=Seed, to_entity=A)
        B → Seed  (reverse edge: from_entity=B, to_entity=Seed; only reachable
                   from Seed by following edge in reverse — undirected BFS does this)

    Expected after _recompute_reachability: both A and B are retained.

    If BFS were directed (only Seed→A, not Seed←B), B would be incorrectly pruned.
    This is the AP3 directed-vs-undirected scenario; we verify the correct
    undirected behavior matches what traverse() would return.
    """
    from omniscience_core.storage.graph import EntityNodeView, GraphEdgeView, GraphResultView
    from omniscience_retrieval.graph_rag import _recompute_reachability

    seed = EntityNodeView(
        id=uuid.uuid4(),
        name="Seed",
        kind="service",
        source=str(uuid.uuid4()),
        chunk_text=None,
        depth=0,
        version=None,
    )
    node_a = EntityNodeView(
        id=uuid.uuid4(),
        name="A",
        kind="repo",
        source=str(uuid.uuid4()),
        chunk_text=None,
        depth=1,
        version=None,
    )
    node_b = EntityNodeView(
        id=uuid.uuid4(),
        name="B",
        kind="team",
        source=str(uuid.uuid4()),
        chunk_text=None,
        depth=1,
        version=None,
    )
    # Forward edge: Seed → A
    edge_forward = GraphEdgeView(from_entity="Seed", to_entity="A", edge_type="USES")
    # Reverse edge: B → Seed (B is only reachable from Seed via reverse direction)
    edge_reverse = GraphEdgeView(from_entity="B", to_entity="Seed", edge_type="OWNED_BY")

    graph_result = GraphResultView(
        seed=seed,
        related=[node_a, node_b],
        edges=[edge_forward, edge_reverse],
    )

    result = _recompute_reachability(graph_result)

    remaining_names = {n.name for n in result.related}
    assert "A" in remaining_names, "Node A (forward-edge neighbour of Seed) must be retained"
    assert "B" in remaining_names, (
        "v12-AP3: Node B (reachable only via reverse edge B→Seed) must be retained "
        "because _recompute_reachability uses undirected BFS to match Neo4j traverse() "
        "undirected semantics.  If this fails, BFS became directed and would produce "
        "a reachability set inconsistent with traverse()."
    )


def test_recompute_reachability_directed_would_drop_reverse_edge_node() -> None:
    """v12-AP3 negative: a DIRECTED BFS would incorrectly drop reverse-edge nodes.

    This test documents the AP3 failure mode: if _recompute_reachability used a
    directed adjacency (only from_entity → to_entity), then node B (reachable
    only via the reverse edge B→Seed) would NOT appear in the adjacency of Seed
    and would be pruned — inconsistent with what traverse() returned.

    We verify the CURRENT implementation (undirected) correctly retains B,
    and also verify that a hypothetical DIRECTED adjacency would NOT retain B
    (so we know the test is testing the right thing).

    This is the proof that the undirected BFS is necessary and correct.
    """
    from omniscience_core.storage.graph import EntityNodeView, GraphEdgeView, GraphResultView
    from omniscience_retrieval.graph_rag import _recompute_reachability

    seed = EntityNodeView(
        id=uuid.uuid4(),
        name="Seed",
        kind="service",
        source=str(uuid.uuid4()),
        chunk_text=None,
        depth=0,
        version=None,
    )
    node_b = EntityNodeView(
        id=uuid.uuid4(),
        name="B",
        kind="team",
        source=str(uuid.uuid4()),
        chunk_text=None,
        depth=1,
        version=None,
    )
    # ONLY reverse edge: B → Seed (no forward edge from Seed to B)
    edge_reverse = GraphEdgeView(from_entity="B", to_entity="Seed", edge_type="OWNED_BY")

    graph_result = GraphResultView(seed=seed, related=[node_b], edges=[edge_reverse])

    result = _recompute_reachability(graph_result)

    # With undirected BFS: B is reachable (Seed → B via reverse traversal of B→Seed)
    remaining_names = {n.name for n in result.related}
    assert "B" in remaining_names, (
        "v12-AP3: with undirected BFS, B must be reachable from Seed via the "
        "reverse of the B→Seed edge (matching Neo4j undirected traverse behavior)"
    )

    # Verify: a DIRECTED adjacency (from_entity→to_entity only) would NOT find B.
    # Build the directed adjacency and check that B is NOT reachable from Seed.
    directed_adj: dict[str, set[str]] = {}
    for edge in [edge_reverse]:
        directed_adj.setdefault(edge.from_entity, set()).add(edge.to_entity)
    # BFS from Seed using directed adjacency
    directed_reachable: set[str] = {"Seed"}
    frontier = ["Seed"]
    while frontier:
        nxt = []
        for n in frontier:
            for nb in directed_adj.get(n, set()):
                if nb not in directed_reachable:
                    directed_reachable.add(nb)
                    nxt.append(nb)
        frontier = nxt
    assert "B" not in directed_reachable, (
        "v12-AP3 proof: directed BFS from Seed via B→Seed edge does NOT reach B — "
        "this confirms why undirected BFS is required to match traverse() semantics"
    )


# ---------------------------------------------------------------------------
# v13-AP2: canonical no-reconciler signal — _global_reconciler=None → all hits pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_reconciler_passes_all_hits() -> None:
    """v13-AP2: a composer with _global_reconciler=None passes ALL hits, no _EPOCH_DROPS.

    When no reconciler is wired, per_source_watermark stays None throughout search().
    _apply_watermark_filter sees None → None-bypass → return all hits unchanged.
    No _EPOCH_DROPS counter must fire for any reason/filter combination.

    This tests the canonical no-reconciler path introduced in v13-AP2 to remove the
    pre-v13 ambiguity where {} was the default for both "no reconciler" and "cold workspace".
    """
    src_a = uuid.uuid4()
    src_b = uuid.uuid4()

    hits = [
        _make_hit(applied_version=1, text="versioned-a", source_id=src_a),
        _make_hit(applied_version=999, text="high-versioned-b", source_id=src_b),
        _make_hit(applied_version=None, text="unversioned-a", source_id=src_a),
    ]

    # Use _make_composer_no_reconciler — _global_reconciler=None, no mock.
    composer = _make_composer_no_reconciler(hits, strict_epoch=True)

    # Capture all relevant counter values before the call.
    before_unmapped = _EPOCH_DROPS.labels(reason="unmapped", filter="hit")._value.get()
    before_lag_hit = _EPOCH_DROPS.labels(reason="lag", filter="hit")._value.get()
    before_cold_zero_hit = _EPOCH_DROPS.labels(reason="cold_zero", filter="hit")._value.get()
    before_blackout = _EPOCH_DROPS.labels(reason="empty_map_blackout", filter="hit")._value.get()

    req = SearchRequest(query="no-reconciler-test", top_k=10)
    result = await composer.search(req, workspace_id=_WS)

    # All 3 hits must pass (None-bypass fires when per_source_watermark=None).
    assert len(result.hits) == 3, (
        f"v13-AP2: all hits must pass when _global_reconciler=None; got {len(result.hits)}"
    )

    # No _EPOCH_DROPS counter must increment for any reason on the no-reconciler path.
    assert _EPOCH_DROPS.labels(reason="unmapped", filter="hit")._value.get() == before_unmapped, (
        "v13-AP2: no-reconciler path must not increment unmapped/hit counter"
    )
    assert _EPOCH_DROPS.labels(reason="lag", filter="hit")._value.get() == before_lag_hit, (
        "v13-AP2: no-reconciler path must not increment lag/hit counter"
    )
    assert (
        _EPOCH_DROPS.labels(reason="cold_zero", filter="hit")._value.get() == before_cold_zero_hit
    ), "v13-AP2: no-reconciler path must not increment cold_zero/hit counter"
    assert (
        _EPOCH_DROPS.labels(reason="empty_map_blackout", filter="hit")._value.get()
        == before_blackout
    ), "v13-AP2: no-reconciler path must not increment empty_map_blackout/hit counter"


@pytest.mark.asyncio
async def test_cold_workspace_empty_map_is_fail_closed_and_counted() -> None:
    """v13-AP1/AP2: reconciler returns {} → versioned hit dropped + counters fire.

    Scenario: reconciler ran (not None) but returned an empty map ({}) —
    cold workspace with no sources yet.  This is the canonical cold-start path.

    Expected:
    - Versioned hits DROPPED (fail-closed under strict_epoch=True).
    - None-versioned hits PASS.
    - empty_map_blackout counter fires ONCE per request.
    - unmapped counter fires once per DROPPED versioned hit.

    This test distinguishes the cold-workspace case (per_source_watermark={})
    from the no-reconciler case (per_source_watermark=None).  The former is
    fail-closed; the latter is pass-all.
    """
    src_id = uuid.uuid4()

    hit_versioned = _make_hit(applied_version=7, text="versioned", source_id=src_id)
    hit_none_versioned = _make_hit(applied_version=None, text="unversioned", source_id=src_id)

    # Reconciler returns {} (cold workspace — no sources found).
    composer = _make_composer_with_reconciler(
        min_watermark=None,
        hits=[hit_versioned, hit_none_versioned],
        per_source_watermark={},
        strict_epoch=True,
    )

    before_blackout = _EPOCH_DROPS.labels(reason="empty_map_blackout", filter="hit")._value.get()
    before_unmapped = _EPOCH_DROPS.labels(reason="unmapped", filter="hit")._value.get()

    req = SearchRequest(query="cold-workspace", top_k=10)
    result = await composer.search(req, workspace_id=_WS)

    # Versioned hit must be dropped.
    versioned_results = [h for h in result.hits if h.applied_version is not None]
    assert versioned_results == [], (
        f"v13-AP2: cold workspace ({{}}) must drop versioned hits; "
        f"got {[(h.applied_version, h.text) for h in versioned_results]}"
    )

    # None-versioned hit must pass.
    none_results = [h for h in result.hits if h.applied_version is None]
    assert len(none_results) == 1, (
        "v13-AP2: None-versioned hit must pass through cold-workspace fail-closed filter"
    )

    # empty_map_blackout fires once per request.
    assert (
        _EPOCH_DROPS.labels(reason="empty_map_blackout", filter="hit")._value.get()
        == before_blackout + 1
    ), "v13-AP1: empty_map_blackout must fire exactly once for cold-workspace request"

    # unmapped fires once per dropped versioned hit (1 versioned hit dropped).
    assert (
        _EPOCH_DROPS.labels(reason="unmapped", filter="hit")._value.get() == before_unmapped + 1
    ), "v13-AP1: unmapped must fire once for the dropped versioned hit"
