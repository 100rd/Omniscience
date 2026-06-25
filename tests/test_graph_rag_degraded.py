"""Tests for Stage 2: degraded-detector correctness after Stage 1 end-dating fix.

PROBLEM (Stage 2, refactor/bitemporal-vector-hybrid):
  Before Stage 1, the degraded detector (``degraded_response =
  "as_of_before_recorded_history"``) could fire as a FALSE POSITIVE when:
  - An anchor entity EXISTS in the graph at ``as_of``, AND
  - Its document content was UPDATED after ``as_of``, AND
  - Old Qdrant chunks were DELETED (not end-dated).
  → Vector layer returned empty → degraded=True even though the entity had history.

SOLUTION (Stage 1): old points are end-dated, so vector returns the old chunk.
  Stage 2 here verifies the degraded flag does NOT fire for the content-rewrite
  scenario (and still DOES fire for the genuine pre-history scenario).

Tests:
  1. Content-rewrite does NOT produce degraded=True when as_of precedes the
     update but the entity's anchor stage finds a hit.
  2. Genuine pre-history (as_of precedes any entity data) STILL produces
     degraded=True when anchor stage also finds no hit.
  3. No-as_of queries never produce degraded=True.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from omniscience_core.storage.graph import (
    EntityNodeView,
    GraphResultView,
)
from omniscience_retrieval.graph_rag import (
    ANCHOR_FILTER_KEY,
    DEGRADED_PRE_HISTORY,
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

_WS = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_T_PAST = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
_T_NOW = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------


def _make_hit(text: str = "content") -> SearchHit:
    return SearchHit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        score=0.9,
        text=text,
        source=SourceInfo(id=uuid.uuid4(), name="src", type="qdrant"),
        citation=Citation(uri="mem://x", title=None, indexed_at=_T_NOW, doc_version=1),
        lineage=ChunkLineage(
            ingestion_run_id=None,
            embedding_model="stub",
            embedding_provider="stub",
            parser_version="1",
            chunker_strategy="fixed",
        ),
        metadata={},
    )


def _make_empty_result() -> SearchResult:
    return SearchResult(
        hits=[],
        query_stats=QueryStats(
            total_matches_before_filters=0,
            vector_matches=0,
            text_matches=0,
            duration_ms=5.0,
        ),
    )


def _make_result_with_hit() -> SearchResult:
    return SearchResult(
        hits=[_make_hit()],
        query_stats=QueryStats(
            total_matches_before_filters=1,
            vector_matches=1,
            text_matches=0,
            duration_ms=5.0,
        ),
    )


def _make_graph_result(
    name: str = "entity-a", source_id: uuid.UUID | None = None
) -> GraphResultView:
    src = source_id or uuid.uuid4()
    return GraphResultView(
        seed=EntityNodeView(
            id=uuid.uuid4(),
            name=name,
            kind="service",
            source=str(src),
            chunk_text=None,
            valid_from=_T_PAST,
            valid_to=None,
            recorded_at=_T_PAST,
        ),
        related=[],
    )


class _FakeGraphStoreHit:
    """Graph store fake that always returns a hit."""

    async def traverse(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        return _make_graph_result(name=entity_name)

    async def find_related(self, **_: Any) -> GraphResultView:
        return _make_graph_result()


class _FakeGraphStoreMiss:
    """Graph store fake that always raises ValueError (entity not found)."""

    async def traverse(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        raise ValueError(f"entity_not_found:{entity_name}")

    async def find_related(self, **_: Any) -> GraphResultView:
        raise ValueError("entity_not_found")


class _FakeVectorStore:
    """Vector store fake with configurable result."""

    def __init__(self, result: SearchResult) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def search(
        self,
        *,
        request: SearchRequest,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> SearchResult:
        self.calls.append({"request": request, "workspace_id": workspace_id, "as_of": as_of})
        return self._result


class _FakeLegacyService:
    async def search(self, request: SearchRequest) -> SearchResult:
        return _make_empty_result()


def _make_composer(graph_hit: bool, vector_result: SearchResult) -> GraphRAGComposer:
    """Build a GraphRAGComposer with the canonical Neo4j+Qdrant stack mocked."""

    graph = _FakeGraphStoreHit() if graph_hit else _FakeGraphStoreMiss()
    vector = _FakeVectorStore(vector_result)

    # Patch isinstance checks so the composer takes the graphrag path.
    composer = GraphRAGComposer.__new__(GraphRAGComposer)
    composer._graph_store = graph  # type: ignore[attr-defined]
    composer._vector_store = vector  # type: ignore[attr-defined]
    composer._legacy_service = _FakeLegacyService()  # type: ignore[attr-defined]
    composer._graphrag_active = True  # type: ignore[attr-defined]
    composer._strict_epoch = True  # type: ignore[attr-defined]
    # AP3: no reconciler in these degraded-detector tests; convergence is not under test here.
    composer._global_reconciler = None  # type: ignore[attr-defined]
    composer._session_factory = None  # type: ignore[attr-defined]
    composer._is_entity_parked_fn = None  # type: ignore[attr-defined]
    return composer


# ---------------------------------------------------------------------------
# Test 1: content rewrite → anchor hit → degraded=False (Stage 2 fix verified)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_rewrite_does_not_produce_degraded() -> None:
    """Stage 2: when anchor HIT (entity exists at as_of) and vector returns hits,
    degraded must be False even when as_of < update time.

    This was a false positive before Stage 1: old points were deleted,
    so vector returned empty even though entity existed at as_of.
    After Stage 1 end-dating, old points are preserved, so vector returns
    the old chunk — and degraded stays False.
    """
    composer = _make_composer(
        graph_hit=True,
        vector_result=_make_result_with_hit(),  # vector returns old chunk (Stage 1 preserved it)
    )
    request = SearchRequest(
        query="content",
        top_k=5,
        filters={ANCHOR_FILTER_KEY: "entity-a"},
    )
    result = await composer.search(request, workspace_id=_WS, as_of=_T_PAST)
    assert result.meta is None or result.meta.get("degraded_response") != DEGRADED_PRE_HISTORY, (
        "degraded must NOT fire when anchor hit and vector returns results"
    )
    assert result.hits, "Result must have hits (the old chunk preserved by Stage 1 end-dating)"


# ---------------------------------------------------------------------------
# Test 2: genuine pre-history → anchor miss → degraded=True (still correct)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_genuine_pre_history_produces_degraded() -> None:
    """Stage 2: real pre-history (anchor entity not found at as_of) → degraded=True."""
    composer = _make_composer(
        graph_hit=False,  # entity didn't exist yet at as_of
        vector_result=_make_empty_result(),  # vector also empty (truly no data at T)
    )
    request = SearchRequest(
        query="content",
        top_k=5,
        filters={ANCHOR_FILTER_KEY: "new-entity"},
    )
    result = await composer.search(request, workspace_id=_WS, as_of=_T_PAST)
    assert result.meta is not None, "degraded response should have a meta block"
    assert result.meta.get("degraded_response") == DEGRADED_PRE_HISTORY, (
        "Genuine pre-history must still produce degraded_response"
    )


# ---------------------------------------------------------------------------
# Test 3: no as_of → never degraded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_as_of_never_degraded() -> None:
    """No as_of means current-state query; degraded flag must never fire."""
    # Even if graph misses and vector returns empty, no as_of → not degraded.
    composer = _make_composer(
        graph_hit=False,
        vector_result=_make_empty_result(),
    )
    request = SearchRequest(
        query="content",
        top_k=5,
        filters={ANCHOR_FILTER_KEY: "entity-x"},
    )
    result = await composer.search(request, workspace_id=_WS)  # no as_of
    assert result.meta is None or result.meta.get("degraded_response") != DEGRADED_PRE_HISTORY, (
        "Without as_of, degraded must never be True"
    )


# ---------------------------------------------------------------------------
# Test 4: anchor hit + vector empty (without as_of) → not degraded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anchor_hit_vector_empty_no_as_of_not_degraded() -> None:
    """Anchor hit + vector empty (no as_of) → not degraded, just empty results."""
    composer = _make_composer(
        graph_hit=True,
        vector_result=_make_empty_result(),
    )
    request = SearchRequest(
        query="nothing matches",
        top_k=5,
        filters={ANCHOR_FILTER_KEY: "entity-a"},
    )
    result = await composer.search(request, workspace_id=_WS)
    meta = result.meta or {}
    assert meta.get("degraded_response") != DEGRADED_PRE_HISTORY, (
        "No as_of → not degraded, just empty results"
    )
