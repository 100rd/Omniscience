"""Tests for Issue #30 — Adaptive retrieval strategies.

Coverage:
- Query classifier: structural patterns
- Query classifier: keyword patterns (quoted strings, error codes)
- Query classifier: default hybrid
- Query classifier: edge cases (empty query, mixed case)
- StrategyRouter.select_strategy: resolves 'auto' via classifier
- StrategyRouter.select_strategy: passes through explicit strategies unchanged
- StrategyRouter.execute: routes 'keyword' to KeywordStrategy
- StrategyRouter.execute: routes 'structural' to StructuralStrategy
- StrategyRouter.execute: routes 'hybrid' to hybrid_fn
- StrategyRouter.execute: routes 'auto' after classification
- KeywordStrategy: BM25-only — no vector call
- KeywordStrategy: vector_matches always 0
- KeywordStrategy: returns ranked hits
- KeywordStrategy: empty results
- KeywordStrategy: top_k respected
- StructuralStrategy: finds seed entities and returns chunks
- StructuralStrategy: falls back to hybrid when no entities found
- StructuralStrategy: falls back to hybrid when entities have no chunks
- StructuralStrategy: traverses both outgoing and incoming edges
- StructuralStrategy: seed chunks score higher than neighbour chunks
- StructuralStrategy: subject extraction strips relationship verbs
- StructuralStrategy: handles entity with null chunk_id
- Auto strategy end-to-end: structural query dispatches to structural
- Auto strategy end-to-end: keyword query dispatches to keyword
- Auto strategy end-to-end: plain query dispatches to hybrid
- Auto strategy end-to-end: classifier result used in router select_strategy
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_retrieval import QueryStats, RetrievalService, SearchRequest, SearchResult
from omniscience_retrieval.strategies import (
    KeywordStrategy,
    StrategyRouter,
    StructuralStrategy,
    classify_query,
)
from omniscience_retrieval.strategies.structural import _extract_subject

# ---------------------------------------------------------------------------
# Shared constants and helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)
_EMBED_DIM = 768


def _make_chunk(
    chunk_id: uuid.UUID | None = None,
    text: str = "sample chunk text",
) -> MagicMock:
    c = MagicMock()
    c.id = chunk_id or uuid.uuid4()
    c.document_id = uuid.uuid4()
    c.text = text
    c.ingestion_run_id = uuid.uuid4()
    c.embedding_model = "text-embedding-004"
    c.embedding_provider = "google-ai"
    c.parser_version = "treesitter-python-0.21+oms-0.4.2"
    c.chunker_strategy = "code_symbol"
    c.chunk_metadata = {"language": "python"}
    return c


def _make_document() -> MagicMock:
    d = MagicMock()
    d.id = uuid.uuid4()
    d.source_id = uuid.uuid4()
    d.uri = "https://github.com/org/repo/blob/main/auth.py"
    d.title = "auth.py"
    d.indexed_at = _NOW
    d.tombstoned_at = None
    d.doc_version = 7
    return d


def _make_source() -> MagicMock:
    s = MagicMock()
    s.id = uuid.uuid4()
    s.name = "main-gitlab"
    s.type = "git"
    return s


def _make_entity(
    entity_id: uuid.UUID | None = None,
    name: str = "mymod.my_func",
    display_name: str = "my_func",
    chunk_id: uuid.UUID | None = None,
) -> MagicMock:
    e = MagicMock()
    e.id = entity_id or uuid.uuid4()
    e.name = name
    e.display_name = display_name
    e.chunk_id = chunk_id
    return e


def _make_embedding_provider() -> AsyncMock:
    provider = AsyncMock()
    provider.dim = _EMBED_DIM
    provider.model_name = "text-embedding-004"
    provider.provider_name = "google-ai"
    provider.embed = AsyncMock(return_value=[[0.1] * _EMBED_DIM])
    return provider


def _make_hybrid_session(
    vector_rows: list[tuple[uuid.UUID, float]] | None = None,
    text_rows: list[tuple[uuid.UUID, float]] | None = None,
    enriched_rows: list[tuple[Any, Any, Any]] | None = None,
) -> MagicMock:
    session = AsyncMock()
    call_results: list[MagicMock] = []

    vec_result = MagicMock()
    vec_rows_mock = []
    for cid, score in vector_rows or []:
        row = MagicMock()
        row.id = cid
        row.dist = 1.0 - score
        vec_rows_mock.append(row)
    vec_result.all.return_value = vec_rows_mock
    call_results.append(vec_result)

    txt_result = MagicMock()
    txt_rows_mock = []
    for cid, score in text_rows or []:
        row = MagicMock()
        row.id = cid
        row.rank = score
        txt_rows_mock.append(row)
    txt_result.all.return_value = txt_rows_mock
    call_results.append(txt_result)

    enriched_result = MagicMock()
    enriched_result.all.return_value = enriched_rows or []
    call_results.append(enriched_result)

    session.execute = AsyncMock(side_effect=call_results)
    return session


def _make_keyword_session(
    text_rows: list[tuple[uuid.UUID, float]] | None = None,
    enriched_rows: list[tuple[Any, Any, Any]] | None = None,
) -> MagicMock:
    session = AsyncMock()
    call_results: list[MagicMock] = []

    txt_result = MagicMock()
    txt_rows_mock = []
    for cid, score in text_rows or []:
        row = MagicMock()
        row.id = cid
        row.rank = score
        txt_rows_mock.append(row)
    txt_result.all.return_value = txt_rows_mock
    call_results.append(txt_result)

    enriched_result = MagicMock()
    enriched_result.all.return_value = enriched_rows or []
    call_results.append(enriched_result)

    session.execute = AsyncMock(side_effect=call_results)
    return session


def _make_structural_session(
    entity_rows: list[Any] | None = None,
    edge_rows: list[Any] | None = None,
    chunk_id_rows: list[Any] | None = None,
    enriched_rows: list[tuple[Any, Any, Any]] | None = None,
    fallback_calls: list[MagicMock] | None = None,
) -> MagicMock:
    """Return a session for structural strategy (1-4+ calls depending on results)."""
    session = AsyncMock()
    call_results: list[MagicMock] = []

    # 1) find_seed_entities → scalars().all()
    entities_result = MagicMock()
    entities_result.scalars.return_value.all.return_value = entity_rows or []
    call_results.append(entities_result)

    if entity_rows:
        # 2) collect_neighbours → .all()
        edges_result = MagicMock()
        edges_result.all.return_value = edge_rows or []
        call_results.append(edges_result)

        if chunk_id_rows is not None or edge_rows is not None:
            # 3) entity_chunk_ids → .all()
            chunk_ids_result = MagicMock()
            chunk_ids_result.all.return_value = chunk_id_rows or []
            call_results.append(chunk_ids_result)

            if chunk_id_rows:
                # 4) fetch_enriched → .all()
                enriched_result = MagicMock()
                enriched_result.all.return_value = enriched_rows or []
                call_results.append(enriched_result)
            else:
                # No chunks → fallback to hybrid (3 more calls)
                for fc in fallback_calls or []:
                    call_results.append(fc)
    else:
        # No entities → fallback to hybrid (3 more calls)
        for fc in fallback_calls or []:
            call_results.append(fc)

    session.execute = AsyncMock(side_effect=call_results)
    return session


def _make_session_factory(session: MagicMock) -> MagicMock:
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _make_service_for_strategy(session: MagicMock) -> RetrievalService:
    session_factory = _make_session_factory(session)
    provider = _make_embedding_provider()
    return RetrievalService(session_factory=session_factory, embedding_provider=provider)


# ---------------------------------------------------------------------------
# 1. Classifier tests
# ---------------------------------------------------------------------------


class TestClassifyQuery:
    def test_depends_on_returns_structural(self) -> None:
        assert classify_query("what depends on AuthService") == "structural"

    def test_imports_returns_structural(self) -> None:
        assert classify_query("what imports utils") == "structural"

    def test_calls_returns_structural(self) -> None:
        assert classify_query("what calls parse_token") == "structural"

    def test_references_returns_structural(self) -> None:
        assert classify_query("references to MyClass") == "structural"

    def test_inherits_from_returns_structural(self) -> None:
        assert classify_query("inherits from BaseModel") == "structural"

    def test_who_calls_returns_structural(self) -> None:
        assert classify_query("who calls authenticate") == "structural"

    def test_uses_returns_structural(self) -> None:
        assert classify_query("what uses Redis") == "structural"

    def test_double_quoted_string_returns_keyword(self) -> None:
        assert classify_query('"authenticate_token"') == "keyword"

    def test_single_quoted_string_returns_keyword(self) -> None:
        assert classify_query("'HTTP_404'") == "keyword"

    def test_error_code_screaming_case_returns_keyword(self) -> None:
        assert classify_query("ERR_CONN_REFUSED") == "keyword"

    def test_screaming_case_in_sentence_returns_keyword(self) -> None:
        assert classify_query("why does HTTP_403 occur") == "keyword"

    def test_plain_query_returns_hybrid(self) -> None:
        assert classify_query("how does authentication work") == "hybrid"

    def test_empty_query_returns_hybrid(self) -> None:
        assert classify_query("") == "hybrid"

    def test_structural_beats_keyword_precedence(self) -> None:
        # If both patterns present, structural wins (checked first)
        assert classify_query('depends on "ERR_CONN_REFUSED"') == "structural"

    def test_case_insensitive_structural_detection(self) -> None:
        assert classify_query("What Depends On AuthService") == "structural"

    def test_exact_function_name_no_quotes_returns_hybrid(self) -> None:
        # lowercase non-screaming names without quotes → hybrid
        assert classify_query("authenticate_token") == "hybrid"


# ---------------------------------------------------------------------------
# 2. StrategyRouter.select_strategy tests
# ---------------------------------------------------------------------------


class TestStrategyRouterSelectStrategy:
    def _make_router(self) -> StrategyRouter:
        session = _make_hybrid_session()
        session_factory = _make_session_factory(session)
        provider = _make_embedding_provider()
        hybrid_fn: Any = AsyncMock(return_value=MagicMock(spec=SearchResult))
        return StrategyRouter(
            session_factory=session_factory,
            embedding_provider=provider,
            hybrid_fn=hybrid_fn,
        )

    def test_explicit_hybrid_passes_through(self) -> None:
        router = self._make_router()
        req = SearchRequest(query="auth", retrieval_strategy="hybrid")
        assert router.select_strategy(req) == "hybrid"

    def test_explicit_keyword_passes_through(self) -> None:
        router = self._make_router()
        req = SearchRequest(query="fn_auth", retrieval_strategy="keyword")
        assert router.select_strategy(req) == "keyword"

    def test_explicit_structural_passes_through(self) -> None:
        router = self._make_router()
        req = SearchRequest(query="depends on X", retrieval_strategy="structural")
        assert router.select_strategy(req) == "structural"

    def test_auto_delegates_to_classifier_structural(self) -> None:
        router = self._make_router()
        req = SearchRequest(query="what depends on AuthService", retrieval_strategy="auto")
        assert router.select_strategy(req) == "structural"

    def test_auto_delegates_to_classifier_keyword(self) -> None:
        router = self._make_router()
        req = SearchRequest(query='"authenticate_token"', retrieval_strategy="auto")
        assert router.select_strategy(req) == "keyword"

    def test_auto_delegates_to_classifier_hybrid(self) -> None:
        router = self._make_router()
        req = SearchRequest(query="how does auth work", retrieval_strategy="auto")
        assert router.select_strategy(req) == "hybrid"


# ---------------------------------------------------------------------------
# 3. KeywordStrategy tests
# ---------------------------------------------------------------------------


class TestKeywordStrategy:
    def _make_keyword_strategy(
        self,
        text_rows: list[tuple[uuid.UUID, float]] | None = None,
        enriched_rows: list[tuple[Any, Any, Any]] | None = None,
    ) -> KeywordStrategy:
        session = _make_keyword_session(text_rows=text_rows, enriched_rows=enriched_rows)
        session_factory = _make_session_factory(session)
        return KeywordStrategy(session_factory=session_factory)

    @pytest.mark.asyncio
    async def test_returns_search_result(self) -> None:
        strategy = self._make_keyword_strategy()
        result = await strategy.execute(SearchRequest(query="fn_auth"))
        assert isinstance(result, SearchResult)

    @pytest.mark.asyncio
    async def test_vector_matches_always_zero(self) -> None:
        cid = uuid.uuid4()
        strategy = self._make_keyword_strategy(
            text_rows=[(cid, 0.9)],
            enriched_rows=[(_make_chunk(chunk_id=cid), _make_document(), _make_source())],
        )
        result = await strategy.execute(SearchRequest(query="fn_auth"))
        assert result.query_stats.vector_matches == 0

    @pytest.mark.asyncio
    async def test_text_matches_counted(self) -> None:
        ids = [uuid.uuid4(), uuid.uuid4()]
        enriched = [(_make_chunk(chunk_id=i), _make_document(), _make_source()) for i in ids]
        strategy = self._make_keyword_strategy(
            text_rows=[(i, 0.8) for i in ids],
            enriched_rows=enriched,
        )
        result = await strategy.execute(SearchRequest(query="fn_auth"))
        assert result.query_stats.text_matches == 2

    @pytest.mark.asyncio
    async def test_hits_ranked_by_bm25_score(self) -> None:
        ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
        # Scores not in order — result should be sorted descending
        text_rows = [(ids[0], 0.3), (ids[1], 0.9), (ids[2], 0.6)]
        enriched = [(_make_chunk(chunk_id=i), _make_document(), _make_source()) for i in ids]
        strategy = self._make_keyword_strategy(text_rows=text_rows, enriched_rows=enriched)
        result = await strategy.execute(SearchRequest(query="fn_auth", top_k=10))
        scores = [h.score for h in result.hits]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_empty_results(self) -> None:
        strategy = self._make_keyword_strategy(text_rows=[], enriched_rows=[])
        result = await strategy.execute(SearchRequest(query="nonexistent"))
        assert result.hits == []
        assert result.query_stats.text_matches == 0

    @pytest.mark.asyncio
    async def test_top_k_respected(self) -> None:
        ids = [uuid.uuid4() for _ in range(5)]
        text_rows = [(i, 0.9 - idx * 0.1) for idx, i in enumerate(ids)]
        enriched = [(_make_chunk(chunk_id=i), _make_document(), _make_source()) for i in ids]
        strategy = self._make_keyword_strategy(text_rows=text_rows, enriched_rows=enriched)
        result = await strategy.execute(SearchRequest(query="fn_auth", top_k=2))
        assert len(result.hits) <= 2


# ---------------------------------------------------------------------------
# 4. StructuralStrategy tests
# ---------------------------------------------------------------------------


class TestStructuralStrategy:
    def _make_strategy(
        self,
        entity_rows: list[Any] | None = None,
        edge_rows: list[Any] | None = None,
        chunk_id_rows: list[Any] | None = None,
        enriched_rows: list[tuple[Any, Any, Any]] | None = None,
        fallback_calls: list[MagicMock] | None = None,
    ) -> tuple[StructuralStrategy, AsyncMock]:
        fallback_fn: AsyncMock = AsyncMock()
        fallback_fn.return_value = SearchResult(
            hits=[],
            query_stats=QueryStats(
                total_matches_before_filters=0,
                vector_matches=0,
                text_matches=0,
                duration_ms=0.0,
            ),
        )

        session = _make_structural_session(
            entity_rows=entity_rows,
            edge_rows=edge_rows,
            chunk_id_rows=chunk_id_rows,
            enriched_rows=enriched_rows,
            fallback_calls=fallback_calls,
        )
        session_factory = _make_session_factory(session)
        strategy = StructuralStrategy(
            session_factory=session_factory,
            fallback_fn=fallback_fn,
        )
        return strategy, fallback_fn

    @pytest.mark.asyncio
    async def test_no_entities_falls_back_to_hybrid(self) -> None:
        strategy, fallback_fn = self._make_strategy(entity_rows=[])
        await strategy.execute(SearchRequest(query="depends on AuthService"))
        fallback_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_entities_with_no_chunks_falls_back_to_hybrid(self) -> None:
        entity = _make_entity(chunk_id=None)
        # chunk_id_rows is empty → no chunks attached
        chunk_row = MagicMock()
        chunk_row.chunk_id = None
        strategy, fallback_fn = self._make_strategy(
            entity_rows=[entity],
            edge_rows=[],
            chunk_id_rows=[],
        )
        await strategy.execute(SearchRequest(query="depends on AuthService"))
        fallback_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_entities_with_chunks_returns_hits(self) -> None:
        cid = uuid.uuid4()
        entity = _make_entity(chunk_id=cid)

        chunk_id_row = MagicMock()
        chunk_id_row.chunk_id = cid

        enriched = [(_make_chunk(chunk_id=cid), _make_document(), _make_source())]

        strategy, fallback_fn = self._make_strategy(
            entity_rows=[entity],
            edge_rows=[],
            chunk_id_rows=[chunk_id_row],
            enriched_rows=enriched,
        )
        result = await strategy.execute(SearchRequest(query="depends on AuthService"))
        fallback_fn.assert_not_called()
        assert len(result.hits) == 1
        assert result.hits[0].chunk_id == cid

    @pytest.mark.asyncio
    async def test_seed_chunk_scores_higher_than_neighbour(self) -> None:
        seed_cid = uuid.uuid4()
        neighbour_cid = uuid.uuid4()

        seed_entity = _make_entity(entity_id=uuid.uuid4(), chunk_id=seed_cid)
        neighbour_entity = _make_entity(entity_id=uuid.uuid4(), chunk_id=neighbour_cid)

        # Edge: seed_entity → neighbour_entity
        edge = MagicMock()
        edge.source_entity_id = seed_entity.id
        edge.target_entity_id = neighbour_entity.id

        # chunk_id_rows for both seed and neighbour entities combined
        seed_row = MagicMock()
        seed_row.chunk_id = seed_cid
        neighbour_row = MagicMock()
        neighbour_row.chunk_id = neighbour_cid

        enriched = [
            (_make_chunk(chunk_id=seed_cid), _make_document(), _make_source()),
            (_make_chunk(chunk_id=neighbour_cid), _make_document(), _make_source()),
        ]

        session = AsyncMock()
        call_results: list[MagicMock] = []

        # 1) find_seed_entities → [seed_entity]
        entities_result = MagicMock()
        entities_result.scalars.return_value.all.return_value = [seed_entity]
        call_results.append(entities_result)

        # 2) collect_neighbours → [edge]
        edges_result = MagicMock()
        edges_result.all.return_value = [edge]
        call_results.append(edges_result)

        # 3) entity_chunk_ids (both entities in scope)
        chunk_ids_result = MagicMock()
        chunk_ids_result.all.return_value = [seed_row, neighbour_row]
        call_results.append(chunk_ids_result)

        # 4) fetch_enriched
        enriched_result = MagicMock()
        enriched_result.all.return_value = enriched
        call_results.append(enriched_result)

        session.execute = AsyncMock(side_effect=call_results)
        session_factory = _make_session_factory(session)
        fallback_fn: AsyncMock = AsyncMock()
        strategy = StructuralStrategy(
            session_factory=session_factory,
            fallback_fn=fallback_fn,
        )
        result = await strategy.execute(SearchRequest(query="depends on AuthService", top_k=10))

        fallback_fn.assert_not_called()
        scores_by_id = {h.chunk_id: h.score for h in result.hits}
        # seed chunk should have a higher score than neighbour chunk
        assert scores_by_id[seed_cid] > scores_by_id[neighbour_cid]

    @pytest.mark.asyncio
    async def test_result_is_search_result_type(self) -> None:
        cid = uuid.uuid4()
        entity = _make_entity(chunk_id=cid)
        chunk_id_row = MagicMock()
        chunk_id_row.chunk_id = cid
        enriched = [(_make_chunk(chunk_id=cid), _make_document(), _make_source())]

        strategy, _ = self._make_strategy(
            entity_rows=[entity],
            edge_rows=[],
            chunk_id_rows=[chunk_id_row],
            enriched_rows=enriched,
        )
        result = await strategy.execute(SearchRequest(query="depends on X"))
        assert isinstance(result, SearchResult)


# ---------------------------------------------------------------------------
# 5. Subject extraction tests
# ---------------------------------------------------------------------------


class TestExtractSubject:
    def test_strips_depends_on(self) -> None:
        assert _extract_subject("depends on AuthService") == "AuthService"

    def test_strips_what_depends_on(self) -> None:
        assert _extract_subject("what depends on AuthService") == "AuthService"

    def test_strips_what_imports(self) -> None:
        assert _extract_subject("what imports utils") == "utils"

    def test_strips_calls(self) -> None:
        assert _extract_subject("calls parse_token") == "parse_token"

    def test_strips_who_calls(self) -> None:
        assert _extract_subject("who calls authenticate") == "authenticate"

    def test_strips_inherits_from(self) -> None:
        assert _extract_subject("inherits from BaseModel") == "BaseModel"

    def test_no_prefix_returns_query(self) -> None:
        subject = _extract_subject("authenticate_user")
        assert "authenticate_user" in subject


# ---------------------------------------------------------------------------
# 6. Auto-strategy end-to-end tests via RetrievalService
# ---------------------------------------------------------------------------


class TestAutoStrategyEndToEnd:
    @pytest.mark.asyncio
    async def test_structural_query_auto_reaches_structural_path(self) -> None:
        """'what depends on X' with auto → structural → no entities → fallback."""
        # Setup: structural calls (no entities) + hybrid fallback calls
        calls: list[MagicMock] = []

        # Structural: find_seed_entities → empty
        entities_result = MagicMock()
        entities_result.scalars.return_value.all.return_value = []
        calls.append(entities_result)

        # Hybrid fallback: vector, text, enriched
        vec_result = MagicMock()
        vec_result.all.return_value = []
        calls.append(vec_result)

        txt_result = MagicMock()
        txt_result.all.return_value = []
        calls.append(txt_result)

        enriched_result = MagicMock()
        enriched_result.all.return_value = []
        calls.append(enriched_result)

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=calls)
        service = _make_service_for_strategy(session)

        result = await service.search(
            SearchRequest(query="what depends on AuthService", retrieval_strategy="auto")
        )
        assert isinstance(result, SearchResult)
        # 4 execute calls = 1 structural + 3 hybrid fallback
        # 1 structural + 2 hybrid fallback (vector + text; no enriched since empty results)
        assert session.execute.call_count == 3

    @pytest.mark.asyncio
    async def test_keyword_query_auto_reaches_keyword_path(self) -> None:
        """Quoted query with auto → keyword → 2 execute calls (text + enriched)."""
        cid = uuid.uuid4()
        calls: list[MagicMock] = []

        # Keyword: text search
        txt_result = MagicMock()
        txt_row = MagicMock()
        txt_row.id = cid
        txt_row.rank = 0.9
        txt_result.all.return_value = [txt_row]
        calls.append(txt_result)

        # Keyword: enriched fetch
        enriched_result = MagicMock()
        enriched_result.all.return_value = [
            (_make_chunk(chunk_id=cid), _make_document(), _make_source())
        ]
        calls.append(enriched_result)

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=calls)
        service = _make_service_for_strategy(session)

        result = await service.search(
            SearchRequest(query='"authenticate_token"', retrieval_strategy="auto")
        )
        assert isinstance(result, SearchResult)
        # 2 execute calls = keyword path only (no vector)
        assert session.execute.call_count == 2
        # Keyword strategy never embeds
        assert service._embedding_provider.embed.call_count == 0  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_plain_query_auto_reaches_hybrid_path(self) -> None:
        """Plain query with auto → hybrid → 3 execute calls."""
        calls: list[MagicMock] = []

        vec_result = MagicMock()
        vec_result.all.return_value = []
        calls.append(vec_result)

        txt_result = MagicMock()
        txt_result.all.return_value = []
        calls.append(txt_result)

        enriched_result = MagicMock()
        enriched_result.all.return_value = []
        calls.append(enriched_result)

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=calls)
        service = _make_service_for_strategy(session)

        result = await service.search(
            SearchRequest(query="how does auth work", retrieval_strategy="auto")
        )
        assert isinstance(result, SearchResult)
        # 3 execute calls = hybrid path (vector + text + enriched)
        # vector + text (enriched skipped since chunk_ids is empty)
        assert session.execute.call_count == 2
        # Hybrid always embeds
        service._embedding_provider.embed.assert_called_once()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_router_select_strategy_reflects_classification(self) -> None:
        """StrategyRouter.select_strategy correctly uses the classifier for 'auto'."""
        session = _make_hybrid_session()
        session_factory = _make_session_factory(session)
        provider = _make_embedding_provider()
        hybrid_fn: Any = AsyncMock()

        router = StrategyRouter(
            session_factory=session_factory,
            embedding_provider=provider,
            hybrid_fn=hybrid_fn,
        )

        assert (
            router.select_strategy(
                SearchRequest(query="what imports utils", retrieval_strategy="auto")
            )
            == "structural"
        )
        assert (
            router.select_strategy(SearchRequest(query='"ERR_404"', retrieval_strategy="auto"))
            == "keyword"
        )
        assert (
            router.select_strategy(
                SearchRequest(query="explain caching", retrieval_strategy="auto")
            )
            == "hybrid"
        )
