"""Unit tests for Stage 3: BM25/RRF hybrid search.

Covers:
  1. ``_tokenize_to_sparse``: correctness, stopword removal,
     normalisation, determinism, empty input.
  2. ``_rrf_merge``: single-list, two-list union, rank-fusion formula,
     dedup, workspace-scoped per-list.
  3. ``search(retrieval_strategy="keyword")``: calls sparse leg only.
  4. ``search(retrieval_strategy="hybrid")``: calls both legs; merges.
  5. ``search(retrieval_strategy="structural")``: calls dense only.
  6. ``text_matches`` in QueryStats reflects sparse-leg count.
  7. Collection bootstrap creates sparse vector config.
  8. Empty sparse query (all stopwords) returns empty result.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_index.stores.qdrant_config import QdrantConfig
from omniscience_index.stores.qdrant_constants import (
    NAMED_VECTOR_DENSE_PRIMARY,
    NAMED_VECTOR_SPARSE_BM25,
    RRF_K,
)
from omniscience_index.stores.qdrant_store import (
    QdrantVectorStore,
    _rrf_merge,
    _tokenize_to_sparse,
)
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qm

_WS = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _embedding_provider(*, dim: int = 4, model: str = "test-model") -> Any:
    m = MagicMock()
    m.dim = dim
    m.model_name = model
    m.provider_name = "mock"
    m.embed = AsyncMock(return_value=[[0.1] * dim])
    m.close = AsyncMock()
    return m


def _mock_client() -> Any:
    c = MagicMock(spec=AsyncQdrantClient)
    c.collection_exists = AsyncMock(return_value=True)
    c.create_collection = AsyncMock()
    c.create_payload_index = AsyncMock()
    c.upsert = AsyncMock()
    c.scroll = AsyncMock(return_value=([], None))
    c.delete = AsyncMock()
    c.set_payload = AsyncMock()
    c.query_points = AsyncMock()
    c.count = AsyncMock(return_value=MagicMock(count=0))
    c.close = AsyncMock()
    return c


def _scored(text: str, score: float = 0.9, pid: str | None = None) -> qm.ScoredPoint:
    p_id = pid or str(uuid.uuid4())
    return qm.ScoredPoint(
        id=p_id,
        version=1,
        score=score,
        payload={
            "workspace_id": str(_WS),
            "document_id": str(uuid.uuid4()),
            "source_id": str(uuid.uuid4()),
            "text": text,
            "uri": "mem://x",
            "title": None,
            "recorded_at": "2026-01-01T00:00:00+00:00",
            "doc_version": 1,
            "ingestion_run_id": None,
            "embedding_model": "stub",
            "embedding_provider": "stub",
            "parser_version": "1",
            "chunker_strategy": "fixed",
            "metadata": {},
        },
        vector=None,
    )


def _make_query_result(*texts: str) -> MagicMock:
    points = [_scored(t) for t in texts]
    r = MagicMock()
    r.points = points
    return r


# ---------------------------------------------------------------------------
# _tokenize_to_sparse unit tests
# ---------------------------------------------------------------------------


def test_tokenize_basic() -> None:
    sv = _tokenize_to_sparse("kubernetes pod")
    assert len(sv.indices) >= 1
    assert len(sv.values) == len(sv.indices)


def test_tokenize_stopwords_removed() -> None:
    """Common stopwords must not appear in the sparse vector."""
    sv_with = _tokenize_to_sparse("kubernetes is a pod")
    sv_without = _tokenize_to_sparse("kubernetes pod")
    # Both should produce the same set of non-stopword tokens.
    assert set(sv_with.indices) == set(sv_without.indices)


def test_tokenize_normalised_max_one() -> None:
    """All values should be in (0, 1]."""
    sv = _tokenize_to_sparse("error connection refused timeout timeout timeout")
    assert all(0 < v <= 1.0 for v in sv.values)
    assert max(sv.values) == 1.0


def test_tokenize_most_frequent_has_value_one() -> None:
    """The most-frequent token normalises to 1.0."""
    sv = _tokenize_to_sparse("foo foo foo bar baz")
    # "foo" appears 3x — highest freq → value 1.0.
    assert 1.0 in sv.values


def test_tokenize_deterministic() -> None:
    a = _tokenize_to_sparse("deployment nginx production")
    b = _tokenize_to_sparse("deployment nginx production")
    assert a.indices == b.indices
    assert a.values == b.values


def test_tokenize_indices_are_golden_crc32() -> None:
    """Index-time and query-time tokens must map to the SAME slots.

    Regression guard for the builtin hash() bug: string hashing is salted
    per-process (PYTHONHASHSEED), so hash()-based indices differed between
    the seed process and the search process, breaking sparse search entirely.
    These golden values come from crc32(token) % 2**20 and are stable across
    processes — if someone reverts to hash(), this assertion fails.
    """
    sv = _tokenize_to_sparse("cilium gateway loadbalancer")
    # crc32("cilium")=...%2**20=909609, "gateway"=974207, "loadbalancer"=602382
    assert sorted(sv.indices) == sorted([909609, 974207, 602382])


def test_tokenize_stable_across_processes() -> None:
    """A fresh interpreter (different PYTHONHASHSEED) yields identical indices.

    This is the cross-process check the in-process determinism test cannot
    make: builtin hash() would pass test_tokenize_deterministic but fail here.
    """
    import subprocess
    import sys

    code = (
        "from omniscience_index.stores.qdrant_store import _tokenize_to_sparse;"
        "sv=_tokenize_to_sparse('cilium gateway loadbalancer');"
        "print(sorted(sv.indices))"
    )
    runs = [
        subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env={"PYTHONHASHSEED": seed, "PATH": __import__("os").environ.get("PATH", "")},
            check=True,
        ).stdout.strip()
        for seed in ("0", "1", "12345")
    ]
    assert runs[0] == runs[1] == runs[2], f"indices vary across PYTHONHASHSEED: {runs}"


def test_tokenize_empty_string() -> None:
    sv = _tokenize_to_sparse("")
    assert sv.indices == []
    assert sv.values == []


def test_tokenize_all_stopwords() -> None:
    sv = _tokenize_to_sparse("is a the and or")
    assert sv.indices == []


def test_tokenize_single_char_filtered() -> None:
    sv = _tokenize_to_sparse("a b c kubernetes")
    # Only "kubernetes" (len > 1) should appear.
    assert len(sv.indices) == 1


# ---------------------------------------------------------------------------
# _rrf_merge unit tests
# ---------------------------------------------------------------------------


def test_rrf_merge_dense_only() -> None:
    """Single list — RRF score is 1/(RRF_K + 1)."""
    sp = _scored("dense-result", score=0.9, pid="pid-1")
    merged = _rrf_merge(dense=[sp], sparse=[], top_k=5)
    assert len(merged) == 1
    expected = 1.0 / (RRF_K + 1)
    assert abs(merged[0].score - expected) < 1e-9


def test_rrf_merge_sparse_only() -> None:
    sp = _scored("sparse-result", score=0.8, pid="pid-2")
    merged = _rrf_merge(dense=[], sparse=[sp], top_k=5)
    assert len(merged) == 1
    assert abs(merged[0].score - 1.0 / (RRF_K + 1)) < 1e-9


def test_rrf_merge_both_lists_boosts_shared_hit() -> None:
    """A hit appearing in both lists gets a higher RRF score."""
    shared_pid = str(uuid.uuid4())
    only_dense_pid = str(uuid.uuid4())
    dense = [
        _scored("shared", score=0.9, pid=shared_pid),
        _scored("only-dense", score=0.8, pid=only_dense_pid),
    ]
    sparse = [_scored("shared", score=0.7, pid=shared_pid)]
    merged = _rrf_merge(dense=dense, sparse=sparse, top_k=5)
    scores = {str(m.id): m.score for m in merged}
    # Shared hit has rank 1 in dense + rank 1 in sparse:
    # 1/(60+1) + 1/(60+1) = 2/61
    assert scores[shared_pid] > scores[only_dense_pid], (
        "shared hit (dense+sparse rank 1+1) must outscore dense-only hit"
    )


def test_rrf_merge_respects_top_k() -> None:
    dense = [_scored(f"d{i}", score=1.0 - i * 0.01) for i in range(20)]
    merged = _rrf_merge(dense=dense, sparse=[], top_k=5)
    assert len(merged) == 5


def test_rrf_merge_no_duplicates() -> None:
    """A point appearing multiple times via indexing must be deduped."""
    shared_pid = str(uuid.uuid4())
    sp = _scored("dup", score=0.9, pid=shared_pid)
    # Both lists have the same pid.
    merged = _rrf_merge(dense=[sp, sp], sparse=[sp], top_k=10)
    ids = [str(m.id) for m in merged]
    assert len(ids) == len(set(ids)), "dedup must prevent duplicate point ids"


# ---------------------------------------------------------------------------
# search() dispatch by retrieval_strategy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_keyword_uses_sparse_only() -> None:
    """keyword strategy → only sparse vector queried."""
    from omniscience_retrieval.models import SearchRequest

    client = _mock_client()
    sparse_result = _make_query_result("error connection refused")
    client.query_points = AsyncMock(return_value=sparse_result)
    store = QdrantVectorStore(
        config=QdrantConfig(),
        embedding_provider=_embedding_provider(),
        client=client,
    )
    req = SearchRequest(query="connection refused", top_k=5, retrieval_strategy="keyword")
    await store.search(request=req, workspace_id=_WS)
    # query_points should be called exactly once — the sparse leg.
    assert client.query_points.await_count == 1
    call_kwargs = client.query_points.call_args.kwargs
    assert call_kwargs.get("using") == NAMED_VECTOR_SPARSE_BM25, (
        "keyword strategy must query sparse vector"
    )
    # embed must NOT be called (no dense leg).
    store._embedding_provider.embed.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_search_structural_uses_dense_only() -> None:
    """structural strategy → only dense HNSW queried."""
    from omniscience_retrieval.models import SearchRequest

    client = _mock_client()
    dense_result = _make_query_result("deployment info")
    client.query_points = AsyncMock(return_value=dense_result)
    store = QdrantVectorStore(
        config=QdrantConfig(),
        embedding_provider=_embedding_provider(),
        client=client,
    )
    req = SearchRequest(query="nginx deployment", top_k=5, retrieval_strategy="structural")
    await store.search(request=req, workspace_id=_WS)
    # query_points should be called exactly once — the dense leg.
    assert client.query_points.await_count == 1
    call_kwargs = client.query_points.call_args.kwargs
    assert call_kwargs.get("using") == NAMED_VECTOR_DENSE_PRIMARY


@pytest.mark.asyncio
async def test_search_hybrid_calls_both_legs() -> None:
    """hybrid strategy → both dense and sparse queried."""
    from omniscience_retrieval.models import SearchRequest

    client = _mock_client()
    call_count = {"n": 0}

    async def _qp(**kwargs: Any) -> Any:
        call_count["n"] += 1
        r = MagicMock()
        r.points = []
        return r

    client.query_points = AsyncMock(side_effect=_qp)

    store = QdrantVectorStore(
        config=QdrantConfig(),
        embedding_provider=_embedding_provider(),
        client=client,
    )
    req = SearchRequest(query="kubernetes pod crash", top_k=5, retrieval_strategy="hybrid")
    await store.search(request=req, workspace_id=_WS)
    # Both legs: dense + sparse = 2 calls.
    assert call_count["n"] == 2, (
        f"hybrid must call query_points twice (dense + sparse), got {call_count['n']}"
    )


@pytest.mark.asyncio
async def test_search_auto_equals_hybrid() -> None:
    """auto strategy behaves identically to hybrid."""
    from omniscience_retrieval.models import SearchRequest

    client = _mock_client()
    call_count = {"n": 0}

    async def _qp(**kwargs: Any) -> Any:
        call_count["n"] += 1
        r = MagicMock()
        r.points = []
        return r

    client.query_points = AsyncMock(side_effect=_qp)
    store = QdrantVectorStore(
        config=QdrantConfig(),
        embedding_provider=_embedding_provider(),
        client=client,
    )
    req = SearchRequest(query="query text", top_k=5, retrieval_strategy="auto")
    await store.search(request=req, workspace_id=_WS)
    assert call_count["n"] == 2, "auto must call both dense and sparse"


@pytest.mark.asyncio
async def test_text_matches_reflects_sparse_count() -> None:
    """QueryStats.text_matches must equal the sparse-leg hit count."""
    from omniscience_retrieval.models import SearchRequest

    client = _mock_client()
    sparse_texts = ["error1", "error2", "error3"]
    dense_result = MagicMock()
    dense_result.points = []
    sparse_result = MagicMock()
    sparse_result.points = [_scored(t) for t in sparse_texts]

    call_idx = {"n": 0}

    async def _qp(**kwargs: Any) -> Any:
        result = dense_result if call_idx["n"] == 0 else sparse_result
        call_idx["n"] += 1
        return result

    client.query_points = AsyncMock(side_effect=_qp)

    store = QdrantVectorStore(
        config=QdrantConfig(),
        embedding_provider=_embedding_provider(),
        client=client,
    )
    req = SearchRequest(query="connection error", top_k=10, retrieval_strategy="hybrid")
    result = await store.search(request=req, workspace_id=_WS)
    assert result.query_stats.text_matches == len(sparse_texts), (
        f"text_matches must equal sparse hit count ({len(sparse_texts)}), "
        f"got {result.query_stats.text_matches}"
    )


@pytest.mark.asyncio
async def test_keyword_empty_query_returns_empty_result() -> None:
    """All-stopword query must return empty result without calling Qdrant."""
    from omniscience_retrieval.models import SearchRequest

    client = _mock_client()
    store = QdrantVectorStore(
        config=QdrantConfig(),
        embedding_provider=_embedding_provider(),
        client=client,
    )
    req = SearchRequest(query="is a the", top_k=5, retrieval_strategy="keyword")
    result = await store.search(request=req, workspace_id=_WS)
    assert result.hits == []
    assert result.query_stats.text_matches == 0
    # No Qdrant call issued (we short-circuit before query_points).
    client.query_points.assert_not_awaited()


# ---------------------------------------------------------------------------
# Collection bootstrap includes sparse vector config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_collection_creates_sparse_vector() -> None:
    """Bootstrap must create collection with BOTH dense and sparse vectors."""
    client = _mock_client()
    client.collection_exists = AsyncMock(return_value=False)
    store = QdrantVectorStore(
        config=QdrantConfig(),
        embedding_provider=_embedding_provider(dim=768, model="nomic"),
        client=client,
    )
    await store._ensure_collection()  # type: ignore[attr-defined]

    client.create_collection.assert_awaited_once()
    call_kwargs = client.create_collection.call_args.kwargs
    assert "sparse_vectors_config" in call_kwargs, (
        "create_collection must include sparse_vectors_config"
    )
    svc = call_kwargs["sparse_vectors_config"]
    assert NAMED_VECTOR_SPARSE_BM25 in svc, (
        f"sparse_vectors_config must contain '{NAMED_VECTOR_SPARSE_BM25}'"
    )
