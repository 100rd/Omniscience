"""AP3 conformance tests — no mixed-epoch evidence in composed results.

AP3 invariant: every hit in a SearchResult must have
``applied_version <= pinned_watermark`` (or ``applied_version is None``).
No composed result may contain evidence from a future epoch that some
stores have not yet applied.

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
5. ``_apply_watermark_filter`` pure-function boundary test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_retrieval.graph_rag import GraphRAGComposer, _apply_watermark_filter
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


def _make_hit(applied_version: int | None, text: str = "chunk") -> SearchHit:
    """Build a minimal SearchHit with the given applied_version."""
    return SearchHit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        score=0.8,
        text=text,
        source=SourceInfo(id=uuid.uuid4(), name="src", type="slack"),
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
    min_watermark: int | None, hits: list[SearchHit]
) -> GraphRAGComposer:
    """Build a GraphRAGComposer whose reconciler returns the given watermark."""

    # Reconciler mock: wait_for_convergence → (degraded, staleness, min_watermark)
    mock_reconciler = MagicMock()
    mock_reconciler.wait_for_convergence = AsyncMock(return_value=([], None, min_watermark))

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
    hits = [
        _make_hit(applied_version=5, text="v5"),
        _make_hit(applied_version=7, text="v7"),
        _make_hit(applied_version=8, text="v8"),  # must be dropped
        _make_hit(applied_version=10, text="v10"),  # must be dropped
    ]
    composer = _make_composer_with_reconciler(min_watermark=7, hits=hits)
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
    composer = _make_composer_with_reconciler(min_watermark=None, hits=hits)
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
    hits = [
        _make_hit(applied_version=1),
        _make_hit(applied_version=5),
        _make_hit(applied_version=10),
        _make_hit(applied_version=None),
    ]
    composer = _make_composer_with_reconciler(min_watermark=10, hits=hits)
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
    hits = [
        _make_hit(applied_version=1, text="v1"),  # must be dropped: 1 > 0
        _make_hit(applied_version=None, text="unversioned"),  # must survive
    ]
    composer = _make_composer_with_reconciler(min_watermark=0, hits=hits)
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
# Test 5: _apply_watermark_filter pure-function boundary test
# ---------------------------------------------------------------------------


def test_apply_watermark_filter_pure_function() -> None:
    """Unit test of _apply_watermark_filter as a pure function.

    Verifies all boundary conditions:
    - Hits with applied_version > watermark → dropped.
    - Hits with applied_version == watermark → retained.
    - Hits with applied_version < watermark → retained.
    - Hits with applied_version is None → always retained.
    - Watermark None → all hits retained (no filter).
    - Watermark 0 → only None-versioned hits survive.
    """
    h_none = _make_hit(applied_version=None)
    h_v0 = _make_hit(applied_version=0)
    h_v5 = _make_hit(applied_version=5)
    h_v7 = _make_hit(applied_version=7)
    h_v8 = _make_hit(applied_version=8)
    h_v10 = _make_hit(applied_version=10)

    all_hits = [h_none, h_v0, h_v5, h_v7, h_v8, h_v10]

    # --- watermark = None: no filter ---
    result = _apply_watermark_filter(all_hits, None)
    assert result == all_hits, "watermark=None must return all hits unchanged"

    # --- watermark = 7: drop v8 and v10 ---
    result = _apply_watermark_filter(all_hits, 7)
    surviving = {h.applied_version for h in result}
    assert surviving == {None, 0, 5, 7}, f"Expected {{None,0,5,7}}; got {surviving}"

    # --- watermark = 0: drop v5, v7, v8, v10; keep None and v0 ---
    result = _apply_watermark_filter(all_hits, 0)
    surviving = {h.applied_version for h in result}
    assert surviving == {None, 0}, f"Expected {{None,0}}; got {surviving}"

    # --- watermark = 10: all pass ---
    result = _apply_watermark_filter(all_hits, 10)
    assert result == all_hits, "watermark=10 must retain all hits"

    # --- exact boundary: watermark == applied_version is retained ---
    result = _apply_watermark_filter([h_v7], 7)
    assert len(result) == 1, "Hit at exactly the watermark must be retained"

    # --- one above boundary: dropped ---
    result = _apply_watermark_filter([h_v8], 7)
    assert result == [], "Hit at watermark+1 must be dropped"

    # --- empty input: always returns empty ---
    assert _apply_watermark_filter([], 5) == []
    assert _apply_watermark_filter([], None) == []
