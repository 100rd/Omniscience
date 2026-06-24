"""Tests for GraphRAG pagination and char-budget truncation.

Covers:
  - Cursor round-trip: page 1 → next_cursor → page 2 with no overlap/no gaps.
  - Budget truncation emits next_cursor; end-of-results yields next_cursor=None.
  - Invalid cursor is handled safely (logs warning, starts at 0).
  - Empty results yield next_cursor=None.
  - Budget smaller than first hit: first hit is always included.
  - top_k cap emits next_cursor when more results exist.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from omniscience_retrieval.graph_rag import (
    GRAPHRAG_BUDGET_CHARS,
    GraphRAGComposer,
    _AnchorStageResult,
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

_NOW = datetime.now(UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_query_stats() -> QueryStats:
    return QueryStats(
        total_matches_before_filters=0,
        vector_matches=0,
        text_matches=0,
        duration_ms=0.0,
    )


def _make_hit(text: str, score: float = 1.0) -> SearchHit:
    return SearchHit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        score=score,
        text=text,
        source=SourceInfo(id=uuid.uuid4(), name="src", type="slack"),
        citation=Citation(uri="slack://foo", title=None, indexed_at=_NOW, doc_version=1),
        lineage=ChunkLineage(
            ingestion_run_id=None,
            embedding_model="stub",
            embedding_provider="stub",
            parser_version="1",
            chunker_strategy="fixed",
        ),
        metadata={},
    )


def _make_vector_result(hits: list[SearchHit]) -> SearchResult:
    return SearchResult(hits=hits, query_stats=_make_query_stats())


def _no_anchor() -> _AnchorStageResult:
    return _AnchorStageResult(
        anchor_requested=False,
        anchor_name="",
        anchor_hit=False,
        candidate_source_ids=(),
        candidate_depths=(),
        centralities={},
        candidate_parked=(),
        duration_s=0.0,
    )


def _make_composer() -> GraphRAGComposer:
    """Return a GraphRAGComposer wired for merge-stage-only tests."""
    composer = GraphRAGComposer.__new__(GraphRAGComposer)
    composer._graphrag_active = True  # type: ignore[attr-defined]
    composer._global_reconciler = None  # type: ignore[attr-defined]
    composer._session_factory = None  # type: ignore[attr-defined]
    composer._is_entity_parked_fn = None  # type: ignore[attr-defined]
    return composer


# ---------------------------------------------------------------------------
# Test 1: budget truncation — first page stops at char budget
# ---------------------------------------------------------------------------


def test_budget_truncation_emits_next_cursor() -> None:
    """Hits exceeding the char budget are deferred; next_cursor is set.

    With GRAPHRAG_BUDGET_CHARS=24000 and hits of 10000 chars each:
      Hit 1: budget=0+10000=10000  ≤ 24000 → included
      Hit 2: budget=10000+10000=20000 ≤ 24000 → included
      Hit 3: 20000+10000=30000 > 24000 AND top_hits>0 → break, next_cursor="2"
    So page 1 returns 2 hits, next_cursor="2".
    """
    composer = _make_composer()
    hits = [_make_hit("A" * 10000, score=1.0 - i * 0.01) for i in range(6)]
    req = SearchRequest(query="test", top_k=10)
    result = composer._run_merge_stage(
        request=req,
        vector_result=_make_vector_result(hits),
        anchor=_no_anchor(),
    )
    assert len(result.hits) == 2
    assert result.next_cursor == "2"


# ---------------------------------------------------------------------------
# Test 2: cursor round-trip — page 2 continues where page 1 left off
# ---------------------------------------------------------------------------


def test_cursor_round_trip_no_overlap_no_gaps() -> None:
    """Page 1 and page 2 together cover all hits with no overlap and no gaps."""
    composer = _make_composer()
    # 6 hits of 10000 chars; budget allows 2 per page.
    hits = [_make_hit("A" * 10000, score=1.0 - i * 0.01) for i in range(6)]
    anchor = _no_anchor()
    vector_result = _make_vector_result(hits)

    # Page 1
    req1 = SearchRequest(query="test", top_k=10)
    res1 = composer._run_merge_stage(request=req1, vector_result=vector_result, anchor=anchor)
    assert len(res1.hits) == 2
    assert res1.next_cursor == "2"

    # Page 2
    req2 = SearchRequest(query="test", top_k=10, cursor=res1.next_cursor)
    res2 = composer._run_merge_stage(request=req2, vector_result=vector_result, anchor=anchor)
    assert len(res2.hits) == 2
    assert res2.next_cursor == "4"

    # Page 3
    req3 = SearchRequest(query="test", top_k=10, cursor=res2.next_cursor)
    res3 = composer._run_merge_stage(request=req3, vector_result=vector_result, anchor=anchor)
    assert len(res3.hits) == 2
    assert res3.next_cursor is None, "Last page should have no next_cursor"

    # No overlap: each page covers a distinct slice
    page1_chunks = {h.chunk_id for h in res1.hits}
    page2_chunks = {h.chunk_id for h in res2.hits}
    page3_chunks = {h.chunk_id for h in res3.hits}
    assert not (page1_chunks & page2_chunks), "Pages 1 and 2 must not overlap"
    assert not (page2_chunks & page3_chunks), "Pages 2 and 3 must not overlap"

    # No gaps: all 6 original hits appear across the three pages
    all_returned = page1_chunks | page2_chunks | page3_chunks
    original_chunks = {h.chunk_id for h in hits}
    assert all_returned == original_chunks, "All hits must appear across pages"


# ---------------------------------------------------------------------------
# Test 3: end-of-results yields next_cursor=None
# ---------------------------------------------------------------------------


def test_end_of_results_no_cursor() -> None:
    """When all hits fit in budget, next_cursor must be None."""
    composer = _make_composer()
    # 3 hits of 100 chars each → well under 24000 budget
    hits = [_make_hit("x" * 100, score=1.0 - i * 0.01) for i in range(3)]
    req = SearchRequest(query="test", top_k=10)
    result = composer._run_merge_stage(
        request=req,
        vector_result=_make_vector_result(hits),
        anchor=_no_anchor(),
    )
    assert len(result.hits) == 3
    assert result.next_cursor is None


# ---------------------------------------------------------------------------
# Test 4: empty results → next_cursor=None, hits=[]
# ---------------------------------------------------------------------------


def test_empty_results_no_cursor() -> None:
    """Empty vector result produces an empty result with no cursor."""
    composer = _make_composer()
    req = SearchRequest(query="test", top_k=10)
    result = composer._run_merge_stage(
        request=req,
        vector_result=_make_vector_result([]),
        anchor=_no_anchor(),
    )
    assert result.hits == []
    assert result.next_cursor is None


# ---------------------------------------------------------------------------
# Test 5: budget smaller than first hit → first hit always included
# ---------------------------------------------------------------------------


def test_first_hit_always_included_even_if_over_budget() -> None:
    """A single hit that exceeds the budget is still returned (no infinite loop).

    The guard ``and len(top_hits) > 0`` ensures the first hit is never
    dropped even if it overflows the budget, preserving the invariant
    that we always make progress.
    """
    composer = _make_composer()
    # Single hit larger than the entire budget
    oversized_text = "Z" * (GRAPHRAG_BUDGET_CHARS + 1)
    hits = [_make_hit(oversized_text)]
    req = SearchRequest(query="test", top_k=10)
    result = composer._run_merge_stage(
        request=req,
        vector_result=_make_vector_result(hits),
        anchor=_no_anchor(),
    )
    assert len(result.hits) == 1, "Oversized first hit must still be returned"
    assert result.next_cursor is None, "No more hits → no cursor"


# ---------------------------------------------------------------------------
# Test 6: top_k cap emits next_cursor
# ---------------------------------------------------------------------------


def test_top_k_cap_emits_cursor() -> None:
    """When results exceed top_k (regardless of budget), next_cursor is set."""
    composer = _make_composer()
    # 10 hits of 10 chars each — well within budget, but top_k=3
    hits = [_make_hit("x" * 10, score=1.0 - i * 0.01) for i in range(10)]
    req = SearchRequest(query="test", top_k=3)
    result = composer._run_merge_stage(
        request=req,
        vector_result=_make_vector_result(hits),
        anchor=_no_anchor(),
    )
    assert len(result.hits) == 3
    assert result.next_cursor == "3"

    # Page 2 from that cursor
    req2 = SearchRequest(query="test", top_k=3, cursor="3")
    result2 = composer._run_merge_stage(
        request=req2,
        vector_result=_make_vector_result(hits),
        anchor=_no_anchor(),
    )
    assert len(result2.hits) == 3
    assert result2.next_cursor == "6"


# ---------------------------------------------------------------------------
# Test 7: invalid cursor is handled safely
# ---------------------------------------------------------------------------


def test_invalid_cursor_starts_at_zero() -> None:
    """An invalid cursor (non-integer) should fall back to offset 0 safely."""
    composer = _make_composer()
    hits = [_make_hit("x" * 100, score=1.0 - i * 0.01) for i in range(3)]
    req = SearchRequest(query="test", top_k=10, cursor="not-a-number")
    result = composer._run_merge_stage(
        request=req,
        vector_result=_make_vector_result(hits),
        anchor=_no_anchor(),
    )
    # Should return all 3 hits (starting from index 0) with no error
    assert len(result.hits) == 3
    assert result.next_cursor is None
