"""Tests for Issue #59 — Cross-encoder re-ranker.

Coverage:
- Reranker Protocol: OllamaReranker and NoopReranker are runtime-checkable
- OllamaReranker.rerank: returns one score per text
- OllamaReranker.rerank: empty texts returns empty list
- OllamaReranker.rerank: scores are floats in [-1, 1]
- OllamaReranker.rerank: identical query and text yields high similarity
- OllamaReranker.rerank: orthogonal vectors yield zero similarity
- OllamaReranker.rerank: batching splits large input across multiple requests
- OllamaReranker.rerank: HTTP error propagates as httpx.HTTPStatusError
- OllamaReranker.close: calls aclose on the underlying client
- OllamaReranker: custom model is sent in POST payload
- OllamaReranker: default model is nomic-embed-text
- NoopReranker.rerank: returns decreasing placeholder scores
- NoopReranker.rerank: length matches input texts
- NoopReranker.rerank: empty texts returns empty list
- NoopReranker.rerank: first score is always 1.0
- NoopReranker.close: completes without error
- _cosine_similarity: zero vector returns 0.0
- _cosine_similarity: unit vectors return correct value
- RetrievalService: no reranker -> NoopReranker assigned internally
- RetrievalService: OllamaReranker wired -> re-scores hits after retrieval
- RetrievalService: re-ranking re-orders hits by new scores
- RetrievalService: re-ranking respects top_k slice
- RetrievalService: NoopReranker does not trigger _apply_reranker
- RetrievalService: reranker sees at most _RERANK_CANDIDATE_LIMIT candidates
- Settings: reranker_enabled default is False
- Settings: reranker_model default is nomic-embed-text
- Settings: reranker_enabled can be set to True via env
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from omniscience_core.config import Settings
from omniscience_retrieval import (
    NoopReranker,
    OllamaReranker,
    QueryStats,
    Reranker,
    RetrievalService,
    SearchHit,
    SearchRequest,
)
from omniscience_retrieval.reranker import _cosine_similarity
from omniscience_retrieval.search import _RERANK_CANDIDATE_LIMIT

# ---------------------------------------------------------------------------
# Shared helpers
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


def _make_document(
    doc_id: uuid.UUID | None = None,
) -> MagicMock:
    d = MagicMock()
    d.id = doc_id or uuid.uuid4()
    d.source_id = uuid.uuid4()
    d.uri = "https://github.com/org/repo/blob/main/auth.py"
    d.title = "auth.py"
    d.indexed_at = _NOW
    d.tombstoned_at = None
    d.doc_version = 1
    return d


def _make_source() -> MagicMock:
    s = MagicMock()
    s.id = uuid.uuid4()
    s.name = "main-repo"
    s.type = "git"
    return s


def _make_embedding_provider(dim: int = _EMBED_DIM) -> AsyncMock:
    provider = AsyncMock()
    provider.dim = dim
    provider.model_name = "text-embedding-004"
    provider.provider_name = "google-ai"
    provider.embed = AsyncMock(return_value=[[0.1] * dim])
    return provider


def _make_query_stats() -> QueryStats:
    return QueryStats(
        total_matches_before_filters=5,
        vector_matches=3,
        text_matches=4,
        duration_ms=12.3,
    )


def _make_search_hit(
    chunk_id: uuid.UUID | None = None,
    score: float = 0.5,
    text: str = "some text",
) -> SearchHit:
    cid = chunk_id or uuid.uuid4()
    from omniscience_retrieval import ChunkLineage, Citation, SourceInfo

    return SearchHit(
        chunk_id=cid,
        document_id=uuid.uuid4(),
        score=score,
        text=text,
        source=SourceInfo(id=uuid.uuid4(), name="src", type="git"),
        citation=Citation(
            uri="https://example.com/file.py",
            title="file.py",
            indexed_at=_NOW,
            doc_version=1,
        ),
        lineage=ChunkLineage(
            ingestion_run_id=uuid.uuid4(),
            embedding_model="nomic-embed-text",
            embedding_provider="ollama",
            parser_version="1.0",
            chunker_strategy="fixed",
        ),
        metadata={},
    )


def _build_ollama_response(vectors: list[list[float]]) -> dict[str, Any]:
    return {"embeddings": vectors}


# ---------------------------------------------------------------------------
# Mock HTTPX client factory
# ---------------------------------------------------------------------------


def _mock_httpx_client(responses: list[dict[str, Any]]) -> MagicMock:
    """Return a mock httpx.AsyncClient that returns JSON responses in order."""
    client = AsyncMock(spec=httpx.AsyncClient)
    mock_responses = []
    for payload in responses:
        resp = MagicMock(spec=httpx.Response)
        resp.json.return_value = payload
        resp.raise_for_status = MagicMock()
        mock_responses.append(resp)
    client.post = AsyncMock(side_effect=mock_responses)
    client.aclose = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# Reranker Protocol conformance
# ---------------------------------------------------------------------------


class TestRerankerProtocol:
    def test_ollama_reranker_satisfies_protocol(self) -> None:
        """OllamaReranker is a runtime-checkable Reranker."""
        assert isinstance(OllamaReranker(), Reranker)

    def test_noop_reranker_satisfies_protocol(self) -> None:
        """NoopReranker is a runtime-checkable Reranker."""
        assert isinstance(NoopReranker(), Reranker)


# ---------------------------------------------------------------------------
# OllamaReranker tests
# ---------------------------------------------------------------------------


class TestOllamaReranker:
    @pytest.mark.asyncio
    async def test_rerank_returns_one_score_per_text(self) -> None:
        """rerank() must return exactly len(texts) scores."""
        texts = ["alpha chunk", "beta chunk", "gamma chunk"]
        # 4 vectors: query + 3 texts
        vecs = [[1.0] + [0.0] * (_EMBED_DIM - 1)] * (len(texts) + 1)
        reranker = OllamaReranker()
        reranker._client = _mock_httpx_client([_build_ollama_response(vecs)])

        scores = await reranker.rerank("query", texts)

        assert len(scores) == len(texts)

    @pytest.mark.asyncio
    async def test_rerank_empty_texts_returns_empty(self) -> None:
        """rerank() short-circuits on empty input, no HTTP call made."""
        reranker = OllamaReranker()
        reranker._client = _mock_httpx_client([])

        scores = await reranker.rerank("query", [])

        assert scores == []

    @pytest.mark.asyncio
    async def test_rerank_scores_are_floats(self) -> None:
        """All returned scores must be plain Python floats."""
        # query: [1, 0, 0, 0], text: [0, 1, 0, 0] -> cosine = 0.0
        vecs = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
        reranker = OllamaReranker()
        reranker._client = _mock_httpx_client([_build_ollama_response(vecs)])

        scores = await reranker.rerank("q", ["some text"])

        assert all(isinstance(s, float) for s in scores)

    @pytest.mark.asyncio
    async def test_identical_query_and_text_yields_high_similarity(self) -> None:
        """Same vector for query and text should produce similarity close to 1.0."""
        vec = [0.5] * 4
        vecs = [vec, vec]  # query + 1 candidate with same vector
        reranker = OllamaReranker()
        reranker._client = _mock_httpx_client([_build_ollama_response(vecs)])

        scores = await reranker.rerank("q", ["same"])

        assert scores[0] == pytest.approx(1.0, abs=1e-6)

    @pytest.mark.asyncio
    async def test_orthogonal_vectors_yield_zero_similarity(self) -> None:
        """Orthogonal query and text vectors should produce similarity of 0.0."""
        vecs = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
        reranker = OllamaReranker()
        reranker._client = _mock_httpx_client([_build_ollama_response(vecs)])

        scores = await reranker.rerank("q", ["orthogonal"])

        assert scores[0] == pytest.approx(0.0, abs=1e-6)

    @pytest.mark.asyncio
    async def test_batching_splits_large_input(self) -> None:
        """With batch_size=2 and 3 texts (+ query = 4), two HTTP calls are made."""
        batch1_vecs = [[1.0, 0.0], [0.5, 0.5]]  # query + text[0]
        batch2_vecs = [[0.3, 0.7], [0.9, 0.1]]  # text[1] + text[2]
        reranker = OllamaReranker(batch_size=2)
        reranker._client = _mock_httpx_client(
            [
                _build_ollama_response(batch1_vecs),
                _build_ollama_response(batch2_vecs),
            ]
        )

        scores = await reranker.rerank("q", ["a", "b", "c"])

        # 2 batches posted
        assert reranker._client.post.call_count == 2
        assert len(scores) == 3

    @pytest.mark.asyncio
    async def test_http_error_propagates(self) -> None:
        """An HTTP 500 from Ollama surfaces as httpx.HTTPStatusError."""
        client = AsyncMock(spec=httpx.AsyncClient)
        resp = MagicMock(spec=httpx.Response)
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500",
            request=MagicMock(),
            response=resp,
        )
        client.post = AsyncMock(return_value=resp)
        reranker = OllamaReranker()
        reranker._client = client

        with pytest.raises(httpx.HTTPStatusError):
            await reranker.rerank("q", ["text"])

    @pytest.mark.asyncio
    async def test_close_calls_aclose(self) -> None:
        """close() must delegate to the underlying client's aclose()."""
        reranker = OllamaReranker()
        reranker._client = AsyncMock(spec=httpx.AsyncClient)

        await reranker.close()

        reranker._client.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_custom_model_sent_in_payload(self) -> None:
        """The configured model name is forwarded in each POST payload."""
        vec = [1.0, 0.0]
        reranker = OllamaReranker(model="bge-large-en-v1.5")
        reranker._client = _mock_httpx_client([_build_ollama_response([vec, vec])])

        await reranker.rerank("q", ["text"])

        call_kwargs = reranker._client.post.call_args
        payload = call_kwargs[1]["json"]
        assert payload["model"] == "bge-large-en-v1.5"

    def test_default_model_is_nomic_embed_text(self) -> None:
        """OllamaReranker defaults to nomic-embed-text."""
        reranker = OllamaReranker()
        assert reranker._model == "nomic-embed-text"

    @pytest.mark.asyncio
    async def test_rerank_multiple_texts_scores_ordered_by_similarity(self) -> None:
        """Higher cosine similarity texts appear with higher scores."""
        # query vec [1,0], text0 vec [1,0] (sim=1.0), text1 vec [0,1] (sim=0.0)
        vecs = [
            [1.0, 0.0],  # query
            [1.0, 0.0],  # text0 -- identical to query
            [0.0, 1.0],  # text1 -- orthogonal
        ]
        reranker = OllamaReranker()
        reranker._client = _mock_httpx_client([_build_ollama_response(vecs)])

        scores = await reranker.rerank("q", ["identical", "orthogonal"])

        assert scores[0] > scores[1]


# ---------------------------------------------------------------------------
# NoopReranker tests
# ---------------------------------------------------------------------------


class TestNoopReranker:
    @pytest.mark.asyncio
    async def test_returns_decreasing_placeholder_scores(self) -> None:
        """NoopReranker scores must strictly decrease."""
        reranker = NoopReranker()
        scores = await reranker.rerank("q", ["a", "b", "c"])
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_length_matches_input(self) -> None:
        reranker = NoopReranker()
        texts = ["x"] * 7
        scores = await reranker.rerank("q", texts)
        assert len(scores) == 7

    @pytest.mark.asyncio
    async def test_empty_texts_returns_empty(self) -> None:
        reranker = NoopReranker()
        scores = await reranker.rerank("q", [])
        assert scores == []

    @pytest.mark.asyncio
    async def test_first_score_is_1(self) -> None:
        reranker = NoopReranker()
        scores = await reranker.rerank("q", ["first", "second"])
        assert scores[0] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_close_completes_without_error(self) -> None:
        reranker = NoopReranker()
        await reranker.close()  # must not raise


# ---------------------------------------------------------------------------
# _cosine_similarity unit tests
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_zero_vector_a_returns_zero(self) -> None:
        assert _cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_zero_vector_b_returns_zero(self) -> None:
        assert _cosine_similarity([1.0, 0.0], [0.0, 0.0]) == 0.0

    def test_both_zero_vectors_returns_zero(self) -> None:
        assert _cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0

    def test_identical_unit_vectors(self) -> None:
        assert _cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_opposite_unit_vectors(self) -> None:
        assert _cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_orthogonal_vectors(self) -> None:
        assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# RetrievalService + reranker integration tests
# ---------------------------------------------------------------------------


def _make_session_for_reranker(
    chunk_ids: list[uuid.UUID],
    texts: list[str],
) -> MagicMock:
    """Return a mock session that yields chunks as vector/text and enriched results."""
    session = AsyncMock()

    # Vector search result
    vec_result = MagicMock()
    vec_rows = []
    for i, cid in enumerate(chunk_ids):
        row = MagicMock()
        row.id = cid
        row.dist = 0.1 * i  # ascending distance -> descending similarity
        vec_rows.append(row)
    vec_result.all.return_value = vec_rows

    # Text search result (empty to keep test simple)
    txt_result = MagicMock()
    txt_result.all.return_value = []

    # Enriched fetch result
    doc = _make_document()
    src = _make_source()
    enriched_rows = [
        (_make_chunk(chunk_id=cid, text=t), doc, src)
        for cid, t in zip(chunk_ids, texts, strict=True)
    ]
    enriched_result = MagicMock()
    enriched_result.all.return_value = enriched_rows

    session.execute = AsyncMock(side_effect=[vec_result, txt_result, enriched_result])
    return session


def _make_service_with_reranker(
    chunk_ids: list[uuid.UUID],
    texts: list[str],
    reranker: Reranker | None,
) -> RetrievalService:
    session = _make_session_for_reranker(chunk_ids, texts)
    session_factory = MagicMock()
    session_factory.return_value.__aenter__ = AsyncMock(return_value=session)
    session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    provider = _make_embedding_provider()
    return RetrievalService(
        session_factory=session_factory,
        embedding_provider=provider,
        reranker=reranker,
    )


class TestRetrievalServiceReranker:
    @pytest.mark.asyncio
    async def test_no_reranker_assigns_noop_internally(self) -> None:
        """When reranker=None, service falls back to NoopReranker."""
        from omniscience_retrieval.reranker import NoopReranker as _NoopReranker

        cid = uuid.uuid4()
        service = _make_service_with_reranker([cid], ["text"], reranker=None)
        assert isinstance(service._reranker, _NoopReranker)

    @pytest.mark.asyncio
    async def test_noop_reranker_does_not_trigger_apply_reranker(self) -> None:
        """When NoopReranker is in use, _apply_reranker must not be called."""
        cid = uuid.uuid4()
        service = _make_service_with_reranker([cid], ["text"], reranker=None)

        with patch.object(service, "_apply_reranker", new_callable=AsyncMock) as mock_apply:
            await service.search(SearchRequest(query="anything"))
            mock_apply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ollama_reranker_wired_rescores_hits(self) -> None:
        """When a real OllamaReranker is supplied, hits are re-scored."""
        chunk_ids = [uuid.uuid4(), uuid.uuid4()]
        texts = ["unrelated text", "highly relevant text"]

        # Simulate reranker: text[1] gets higher score than text[0]
        mock_reranker = AsyncMock(spec=OllamaReranker)
        mock_reranker.rerank = AsyncMock(return_value=[0.1, 0.9])

        service = _make_service_with_reranker(chunk_ids, texts, reranker=mock_reranker)
        result = await service.search(SearchRequest(query="relevant", top_k=2))

        # The hit with score 0.9 should appear first
        assert result.hits[0].score == pytest.approx(0.9)
        assert result.hits[1].score == pytest.approx(0.1)

    @pytest.mark.asyncio
    async def test_reranking_reorders_hits_by_new_scores(self) -> None:
        """Re-ranking must override the original RRF order."""
        chunk_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
        texts = ["low", "medium", "high"]

        # Reverse order: RRF has texts[0] first, reranker says texts[2] is best
        mock_reranker = AsyncMock(spec=OllamaReranker)
        mock_reranker.rerank = AsyncMock(return_value=[0.1, 0.5, 0.95])

        service = _make_service_with_reranker(chunk_ids, texts, reranker=mock_reranker)
        result = await service.search(SearchRequest(query="q", top_k=3))

        scores = [h.score for h in result.hits]
        assert scores == sorted(scores, reverse=True)
        assert result.hits[0].text == "high"

    @pytest.mark.asyncio
    async def test_reranking_respects_top_k(self) -> None:
        """After re-ranking, only top_k hits are returned."""
        chunk_ids = [uuid.uuid4() for _ in range(5)]
        texts = [f"text {i}" for i in range(5)]

        mock_reranker = AsyncMock(spec=OllamaReranker)
        mock_reranker.rerank = AsyncMock(return_value=[0.5, 0.4, 0.9, 0.2, 0.7])

        service = _make_service_with_reranker(chunk_ids, texts, reranker=mock_reranker)
        result = await service.search(SearchRequest(query="q", top_k=3))

        assert len(result.hits) == 3

    @pytest.mark.asyncio
    async def test_reranker_receives_at_most_candidate_limit_texts(self) -> None:
        """rerank() is called with at most _RERANK_CANDIDATE_LIMIT texts."""
        # Build more candidates than the limit
        n = _RERANK_CANDIDATE_LIMIT + 10
        chunk_ids = [uuid.uuid4() for _ in range(n)]
        texts = [f"text {i}" for i in range(n)]

        captured_texts: list[str] = []

        async def _capture_rerank(query: str, t: list[str]) -> list[float]:
            captured_texts.extend(t)
            return [1.0 / (i + 1) for i in range(len(t))]

        mock_reranker = AsyncMock(spec=OllamaReranker)
        mock_reranker.rerank = AsyncMock(side_effect=_capture_rerank)

        # Build a bigger session mock for n chunks
        session = AsyncMock()

        vec_result = MagicMock()
        vec_rows = [MagicMock(id=cid, dist=0.0) for cid in chunk_ids]
        vec_result.all.return_value = vec_rows

        txt_result = MagicMock()
        txt_result.all.return_value = []

        doc = _make_document()
        src = _make_source()
        enriched_result = MagicMock()
        enriched_result.all.return_value = [
            (_make_chunk(chunk_id=cid, text=t), doc, src)
            for cid, t in zip(chunk_ids, texts, strict=True)
        ]

        session.execute = AsyncMock(side_effect=[vec_result, txt_result, enriched_result])
        session_factory = MagicMock()
        session_factory.return_value.__aenter__ = AsyncMock(return_value=session)
        session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        provider = _make_embedding_provider()
        service = RetrievalService(
            session_factory=session_factory,
            embedding_provider=provider,
            reranker=mock_reranker,
        )

        await service.search(SearchRequest(query="q", top_k=5))

        assert len(captured_texts) <= _RERANK_CANDIDATE_LIMIT


# ---------------------------------------------------------------------------
# Settings tests
# ---------------------------------------------------------------------------


class TestRerankerSettings:
    def test_reranker_enabled_default_is_false(self) -> None:
        settings = Settings()
        assert settings.reranker_enabled is False

    def test_reranker_model_default(self) -> None:
        settings = Settings()
        assert settings.reranker_model == "nomic-embed-text"

    def test_reranker_enabled_can_be_overridden(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RERANKER_ENABLED", "true")
        settings = Settings()
        assert settings.reranker_enabled is True

    def test_reranker_model_can_be_overridden(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RERANKER_MODEL", "bge-large-en-v1.5")
        settings = Settings()
        assert settings.reranker_model == "bge-large-en-v1.5"
