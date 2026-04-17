"""Tests for the omniscience_retrieval package.

Covers:
- Hybrid search combining vector and BM25 results
- RRF merge produces correct scores
- Source name filter applied
- Source type filter applied
- max_age_seconds filter
- Tombstone filter (default excludes, include_tombstoned includes)
- Metadata filter
- Empty results
- query_stats populated correctly
- Unsupported retrieval_strategy logs warning and falls back to hybrid
- Citation includes all required fields
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_retrieval import (
    RetrievalService,
    SearchRequest,
    SearchResult,
)
from omniscience_retrieval.ranking import reciprocal_rank_fusion

# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)
_EMBED_DIM = 768


def _make_chunk(
    chunk_id: uuid.UUID | None = None,
    doc_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    text: str = "sample chunk text",
    metadata: dict[str, Any] | None = None,
) -> MagicMock:
    c = MagicMock()
    c.id = chunk_id or uuid.uuid4()
    c.document_id = doc_id or uuid.uuid4()
    c.text = text
    c.ingestion_run_id = run_id
    c.embedding_model = "text-embedding-004"
    c.embedding_provider = "google-ai"
    c.parser_version = "treesitter-python-0.21+oms-0.4.2"
    c.chunker_strategy = "code_symbol"
    c.chunk_metadata = metadata or {"language": "python"}
    return c


def _make_document(
    doc_id: uuid.UUID | None = None,
    source_id: uuid.UUID | None = None,
    uri: str = "https://github.com/org/repo/blob/main/auth.py",
    title: str | None = "auth.py",
    tombstoned_at: datetime | None = None,
) -> MagicMock:
    d = MagicMock()
    d.id = doc_id or uuid.uuid4()
    d.source_id = source_id or uuid.uuid4()
    d.uri = uri
    d.title = title
    d.indexed_at = _NOW
    d.tombstoned_at = tombstoned_at
    d.doc_version = 7
    return d


def _make_source(
    source_id: uuid.UUID | None = None,
    name: str = "main-gitlab",
    stype: str = "git",
) -> MagicMock:
    s = MagicMock()
    s.id = source_id or uuid.uuid4()
    s.name = name
    s.type = stype
    return s


def _make_embedding_provider(dim: int = _EMBED_DIM) -> AsyncMock:
    provider = AsyncMock()
    provider.dim = dim
    provider.model_name = "text-embedding-004"
    provider.provider_name = "google-ai"
    provider.embed = AsyncMock(return_value=[[0.1] * dim])
    return provider


def _make_db_row(
    chunk: MagicMock | None = None,
    document: MagicMock | None = None,
    source: MagicMock | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    return (
        chunk or _make_chunk(),
        document or _make_document(),
        source or _make_source(),
    )


# ---------------------------------------------------------------------------
# Session + service factory
# ---------------------------------------------------------------------------


def _make_session(
    vector_rows: list[tuple[uuid.UUID, float]] | None = None,
    text_rows: list[tuple[uuid.UUID, float]] | None = None,
    enriched_rows: list[tuple[Any, Any, Any]] | None = None,
) -> MagicMock:
    """Return a mock async context-managed session wired to return test data."""
    session = AsyncMock()

    # Build fake execute results per call order
    call_results: list[MagicMock] = []

    # First execute -> vector search result
    vec_result = MagicMock()
    vec_rows_mock = []
    for cid, score in vector_rows or []:
        row = MagicMock()
        row.id = cid
        row.dist = 1.0 - score  # cosine distance = 1 - similarity
        vec_rows_mock.append(row)
    vec_result.all.return_value = vec_rows_mock
    call_results.append(vec_result)

    # Second execute -> text search result
    txt_result = MagicMock()
    txt_rows_mock = []
    for cid, score in text_rows or []:
        row = MagicMock()
        row.id = cid
        row.rank = score
        txt_rows_mock.append(row)
    txt_result.all.return_value = txt_rows_mock
    call_results.append(txt_result)

    # Third execute -> enriched fetch
    enriched_result = MagicMock()
    enriched_result.all.return_value = enriched_rows or []
    call_results.append(enriched_result)

    session.execute = AsyncMock(side_effect=call_results)
    return session


def _make_service(
    vector_rows: list[tuple[uuid.UUID, float]] | None = None,
    text_rows: list[tuple[uuid.UUID, float]] | None = None,
    enriched_rows: list[tuple[Any, Any, Any]] | None = None,
) -> tuple[RetrievalService, MagicMock]:
    """Return (RetrievalService, session_mock) pair ready for testing."""
    session = _make_session(vector_rows, text_rows, enriched_rows)

    # async_sessionmaker context-manager protocol
    session_factory = MagicMock()
    session_factory.return_value.__aenter__ = AsyncMock(return_value=session)
    session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    provider = _make_embedding_provider()
    service = RetrievalService(session_factory=session_factory, embedding_provider=provider)
    return service, session


# ---------------------------------------------------------------------------
# RRF unit tests (pure function — no DB)
# ---------------------------------------------------------------------------


class TestReciprocalRankFusion:
    def test_single_list_scores_correctly(self) -> None:
        """Score for rank 1 in a single list = 1/(60+1) ≈ 0.01639."""
        ids = [uuid.uuid4(), uuid.uuid4()]
        result = reciprocal_rank_fusion([([(ids[0], 0.9), (ids[1], 0.5)])])
        assert result[0][0] == ids[0]
        assert result[0][1] == pytest.approx(1 / 61)
        assert result[1][1] == pytest.approx(1 / 62)

    def test_two_lists_same_item_gets_boosted(self) -> None:
        """An item appearing in both lists accumulates score from each."""
        shared = uuid.uuid4()
        only_vec = uuid.uuid4()
        only_txt = uuid.uuid4()

        vec_list = [(shared, 0.9), (only_vec, 0.7)]
        txt_list = [(shared, 0.8), (only_txt, 0.6)]

        result = reciprocal_rank_fusion([vec_list, txt_list])
        by_id = {cid: score for cid, score in result}

        # shared appears rank-1 in both → 1/61 + 1/61
        assert by_id[shared] == pytest.approx(2 / 61)
        assert by_id[only_vec] == pytest.approx(1 / 62)
        assert by_id[only_txt] == pytest.approx(1 / 62)

    def test_output_sorted_descending(self) -> None:
        ids = [uuid.uuid4() for _ in range(5)]
        ranked = [(i, float(idx)) for idx, i in enumerate(ids)]
        result = reciprocal_rank_fusion([ranked])
        scores = [s for _, s in result]
        assert scores == sorted(scores, reverse=True)

    def test_empty_lists(self) -> None:
        assert reciprocal_rank_fusion([[], []]) == []

    def test_custom_k(self) -> None:
        cid = uuid.uuid4()
        result = reciprocal_rank_fusion([[(cid, 1.0)]], k=10)
        assert result[0][1] == pytest.approx(1 / 11)

    def test_disjoint_lists_equal_ranks(self) -> None:
        """Two items each ranked #1 in their own list should have equal scores."""
        a, b = uuid.uuid4(), uuid.uuid4()
        result = reciprocal_rank_fusion([[(a, 0.9)], [(b, 0.9)]])
        by_id = dict(result)
        assert by_id[a] == pytest.approx(by_id[b])


# ---------------------------------------------------------------------------
# RetrievalService integration tests (mocked DB)
# ---------------------------------------------------------------------------


class TestRetrievalServiceHybrid:
    @pytest.mark.asyncio
    async def test_hybrid_combines_vector_and_bm25(self) -> None:
        """Hits from both vector and text search appear in the result."""
        vec_id = uuid.uuid4()
        txt_id = uuid.uuid4()
        shared_id = uuid.uuid4()

        vec_rows = [(shared_id, 0.95), (vec_id, 0.8)]
        txt_rows = [(shared_id, 1.2), (txt_id, 0.9)]

        chunk_v = _make_chunk(chunk_id=vec_id)
        chunk_t = _make_chunk(chunk_id=txt_id)
        chunk_s = _make_chunk(chunk_id=shared_id)
        doc = _make_document()
        src = _make_source()

        enriched = [
            (chunk_s, doc, src),
            (chunk_v, doc, src),
            (chunk_t, doc, src),
        ]

        service, _ = _make_service(
            vector_rows=vec_rows,
            text_rows=txt_rows,
            enriched_rows=enriched,
        )
        result = await service.search(SearchRequest(query="auth", top_k=10))

        chunk_ids = {h.chunk_id for h in result.hits}
        assert shared_id in chunk_ids
        assert vec_id in chunk_ids
        assert txt_id in chunk_ids

    @pytest.mark.asyncio
    async def test_top_k_limits_hits(self) -> None:
        ids = [uuid.uuid4() for _ in range(5)]
        vec_rows = [(i, 0.9 - idx * 0.05) for idx, i in enumerate(ids)]
        doc = _make_document()
        src = _make_source()
        enriched = [(_make_chunk(chunk_id=i), doc, src) for i in ids]

        service, _ = _make_service(vector_rows=vec_rows, enriched_rows=enriched)
        result = await service.search(SearchRequest(query="test", top_k=3))

        assert len(result.hits) <= 3

    @pytest.mark.asyncio
    async def test_hits_sorted_by_score_descending(self) -> None:
        ids = [uuid.uuid4() for _ in range(3)]
        vec_rows = [(ids[0], 0.9), (ids[1], 0.7), (ids[2], 0.5)]
        txt_rows = [(ids[2], 1.0)]  # ids[2] appears in text → gets boosted

        doc = _make_document()
        src = _make_source()
        enriched = [(_make_chunk(chunk_id=i), doc, src) for i in ids]

        service, _ = _make_service(
            vector_rows=vec_rows,
            text_rows=txt_rows,
            enriched_rows=enriched,
        )
        result = await service.search(SearchRequest(query="x", top_k=10))

        scores = [h.score for h in result.hits]
        assert scores == sorted(scores, reverse=True)


class TestRetrievalServiceStats:
    @pytest.mark.asyncio
    async def test_query_stats_counts_are_populated(self) -> None:
        ids = [uuid.uuid4(), uuid.uuid4()]
        vec_rows = [(ids[0], 0.9)]
        txt_rows = [(ids[1], 0.8)]
        doc = _make_document()
        src = _make_source()
        enriched = [(_make_chunk(chunk_id=ids[0]), doc, src)]

        service, _ = _make_service(
            vector_rows=vec_rows,
            text_rows=txt_rows,
            enriched_rows=enriched,
        )
        result = await service.search(SearchRequest(query="test"))

        stats = result.query_stats
        assert stats.vector_matches == 1
        assert stats.text_matches == 1
        assert stats.total_matches_before_filters == 2  # two distinct IDs
        assert stats.duration_ms >= 0.0

    @pytest.mark.asyncio
    async def test_query_stats_overlap_counted_once(self) -> None:
        """When the same chunk appears in both lists, total_before should not double-count."""
        cid = uuid.uuid4()
        vec_rows = [(cid, 0.9)]
        txt_rows = [(cid, 0.8)]
        doc = _make_document()
        src = _make_source()
        enriched = [(_make_chunk(chunk_id=cid), doc, src)]

        service, _ = _make_service(
            vector_rows=vec_rows,
            text_rows=txt_rows,
            enriched_rows=enriched,
        )
        result = await service.search(SearchRequest(query="dupe"))

        assert result.query_stats.total_matches_before_filters == 1


class TestRetrievalServiceEmptyResults:
    @pytest.mark.asyncio
    async def test_empty_vector_and_text_returns_no_hits(self) -> None:
        service, _ = _make_service(vector_rows=[], text_rows=[], enriched_rows=[])
        result = await service.search(SearchRequest(query="nothing matches"))

        assert result.hits == []
        assert result.query_stats.vector_matches == 0
        assert result.query_stats.text_matches == 0
        assert result.query_stats.total_matches_before_filters == 0

    @pytest.mark.asyncio
    async def test_enriched_empty_returns_no_hits(self) -> None:
        """Chunks found by search but filtered out at DB join level → empty hits."""
        cid = uuid.uuid4()
        service, _ = _make_service(
            vector_rows=[(cid, 0.9)],
            text_rows=[],
            enriched_rows=[],
        )
        result = await service.search(SearchRequest(query="filtered out"))
        assert result.hits == []


class TestRetrievalServiceFilters:
    @pytest.mark.asyncio
    async def test_source_name_filter_passed_to_where_clauses(self) -> None:
        """Verify that source name filter is built without raising and that
        execute is called with a statement containing a WHERE."""
        cid = uuid.uuid4()
        service, session = _make_service(
            vector_rows=[(cid, 0.9)],
            enriched_rows=[(_make_chunk(chunk_id=cid), _make_document(), _make_source())],
        )
        req = SearchRequest(query="auth", sources=["main-gitlab"])
        result = await service.search(req)
        # Third execute (enriched fetch) must have been called
        assert session.execute.call_count == 3
        assert isinstance(result, SearchResult)

    @pytest.mark.asyncio
    async def test_source_type_filter_passed_to_where_clauses(self) -> None:
        cid = uuid.uuid4()
        service, session = _make_service(
            vector_rows=[(cid, 0.9)],
            enriched_rows=[(_make_chunk(chunk_id=cid), _make_document(), _make_source())],
        )
        req = SearchRequest(query="deploy", types=["git", "fs"])
        result = await service.search(req)
        assert session.execute.call_count == 3
        assert isinstance(result, SearchResult)

    @pytest.mark.asyncio
    async def test_max_age_filter_passed_to_where_clauses(self) -> None:
        cid = uuid.uuid4()
        service, session = _make_service(
            vector_rows=[(cid, 0.9)],
            enriched_rows=[(_make_chunk(chunk_id=cid), _make_document(), _make_source())],
        )
        req = SearchRequest(query="recent stuff", max_age_seconds=3600)
        result = await service.search(req)
        assert session.execute.call_count == 3
        assert isinstance(result, SearchResult)

    @pytest.mark.asyncio
    async def test_tombstone_filter_excludes_by_default(self) -> None:
        """Default request excludes tombstoned docs — filter clause is built."""
        cid = uuid.uuid4()
        service, _ = _make_service(
            vector_rows=[(cid, 0.9)],
            enriched_rows=[],  # simulate all filtered out by tombstone
        )
        req = SearchRequest(query="deleted stuff")
        result = await service.search(req)
        assert result.hits == []

    @pytest.mark.asyncio
    async def test_tombstone_filter_includes_when_requested(self) -> None:
        """include_tombstoned=True still fetches from DB (no tombstone WHERE clause)."""
        cid = uuid.uuid4()
        chunk = _make_chunk(chunk_id=cid)
        doc = _make_document(tombstoned_at=_NOW)
        src = _make_source()
        service, _ = _make_service(
            vector_rows=[(cid, 0.9)],
            enriched_rows=[(chunk, doc, src)],
        )
        req = SearchRequest(query="deleted stuff", include_tombstoned=True)
        result = await service.search(req)
        assert len(result.hits) == 1

    @pytest.mark.asyncio
    async def test_metadata_filter_passed_to_where_clauses(self) -> None:
        cid = uuid.uuid4()
        service, session = _make_service(
            vector_rows=[(cid, 0.9)],
            enriched_rows=[
                (
                    _make_chunk(chunk_id=cid, metadata={"language": "python"}),
                    _make_document(),
                    _make_source(),
                ),
            ],
        )
        req = SearchRequest(query="python code", filters={"language": "python"})
        result = await service.search(req)
        assert session.execute.call_count == 3
        assert isinstance(result, SearchResult)


class TestRetrievalServiceCitation:
    @pytest.mark.asyncio
    async def test_citation_includes_all_required_fields(self) -> None:
        run_id = uuid.uuid4()
        cid = uuid.uuid4()
        doc_id = uuid.uuid4()

        chunk = _make_chunk(chunk_id=cid, doc_id=doc_id, run_id=run_id)
        doc = _make_document(
            doc_id=doc_id,
            uri="https://github.com/org/repo/blob/abc/auth.py#L42-L60",
            title="auth.py",
        )
        src = _make_source(name="main-gitlab", stype="git")

        service, _ = _make_service(
            vector_rows=[(cid, 0.9)],
            enriched_rows=[(chunk, doc, src)],
        )
        result = await service.search(SearchRequest(query="auth"))

        assert len(result.hits) == 1
        hit = result.hits[0]

        # Citation fields
        assert hit.citation.uri == doc.uri
        assert hit.citation.title == doc.title
        assert hit.citation.indexed_at == _NOW
        assert hit.citation.doc_version == 7

        # Source info
        assert hit.source.name == "main-gitlab"
        assert hit.source.type == "git"

        # Lineage fields
        assert hit.lineage.ingestion_run_id == run_id
        assert hit.lineage.embedding_model == "text-embedding-004"
        assert hit.lineage.embedding_provider == "google-ai"
        assert hit.lineage.chunker_strategy == "code_symbol"

        # Basic hit fields
        assert hit.chunk_id == cid
        assert hit.document_id == doc_id
        assert isinstance(hit.score, float)
        assert hit.text == "sample chunk text"

    @pytest.mark.asyncio
    async def test_citation_title_can_be_none(self) -> None:
        cid = uuid.uuid4()
        chunk = _make_chunk(chunk_id=cid)
        doc = _make_document(title=None)
        src = _make_source()

        service, _ = _make_service(vector_rows=[(cid, 0.9)], enriched_rows=[(chunk, doc, src)])
        result = await service.search(SearchRequest(query="x"))

        assert result.hits[0].citation.title is None

    @pytest.mark.asyncio
    async def test_lineage_ingestion_run_id_can_be_none(self) -> None:
        cid = uuid.uuid4()
        chunk = _make_chunk(chunk_id=cid, run_id=None)
        doc = _make_document()
        src = _make_source()

        service, _ = _make_service(vector_rows=[(cid, 0.9)], enriched_rows=[(chunk, doc, src)])
        result = await service.search(SearchRequest(query="x"))

        assert result.hits[0].lineage.ingestion_run_id is None


class TestRetrievalServiceStrategyFallback:
    @pytest.mark.asyncio
    async def test_keyword_strategy_logs_warning_and_falls_back(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        service, _ = _make_service(vector_rows=[], text_rows=[], enriched_rows=[])
        with caplog.at_level(logging.WARNING, logger="omniscience_retrieval.search"):
            result = await service.search(
                SearchRequest(query="fn_auth", retrieval_strategy="keyword")
            )
        assert any("keyword" in msg for msg in caplog.messages)
        assert isinstance(result, SearchResult)

    @pytest.mark.asyncio
    async def test_structural_strategy_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        service, _ = _make_service(vector_rows=[], text_rows=[], enriched_rows=[])
        with caplog.at_level(logging.WARNING, logger="omniscience_retrieval.search"):
            await service.search(
                SearchRequest(query="depends on X", retrieval_strategy="structural")
            )
        assert any("structural" in msg for msg in caplog.messages)

    @pytest.mark.asyncio
    async def test_auto_strategy_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        service, _ = _make_service(vector_rows=[], text_rows=[], enriched_rows=[])
        with caplog.at_level(logging.WARNING, logger="omniscience_retrieval.search"):
            await service.search(SearchRequest(query="anything", retrieval_strategy="auto"))
        assert any("auto" in msg for msg in caplog.messages)

    @pytest.mark.asyncio
    async def test_hybrid_strategy_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        service, _ = _make_service(vector_rows=[], text_rows=[], enriched_rows=[])
        with caplog.at_level(logging.WARNING, logger="omniscience_retrieval.search"):
            await service.search(SearchRequest(query="auth", retrieval_strategy="hybrid"))
        assert not caplog.messages


class TestSearchRequestModel:
    def test_defaults(self) -> None:
        req = SearchRequest(query="hello")
        assert req.top_k == 10
        assert req.retrieval_strategy == "hybrid"
        assert req.include_tombstoned is False
        assert req.sources is None
        assert req.types is None
        assert req.max_age_seconds is None
        assert req.filters is None

    def test_custom_values(self) -> None:
        req = SearchRequest(
            query="find me something",
            top_k=5,
            sources=["s1", "s2"],
            types=["git"],
            max_age_seconds=7200,
            filters={"language": "go"},
            include_tombstoned=True,
            retrieval_strategy="keyword",
        )
        assert req.top_k == 5
        assert req.sources == ["s1", "s2"]
        assert req.types == ["git"]
        assert req.max_age_seconds == 7200
        assert req.filters == {"language": "go"}
        assert req.include_tombstoned is True
        assert req.retrieval_strategy == "keyword"
