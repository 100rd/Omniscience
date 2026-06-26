"""Contract tests: GraphRAGComposer hybrid/RRF path with a real Qdrant container.

fix/push-sources-and-graphrag-sparse — issue: GraphRAG vector stage was
effectively dense-only (text_matches=0 in all MCP responses) because the
Stage 3 sparse/RRF dispatch in QdrantVectorStore was not exercised through
the GraphRAG composition path.

These tests prove:

1. With ``retrieval_strategy="hybrid"`` the MCP search path through
   GraphRAGComposer returns ``QueryStats.text_matches > 0`` for a query
   whose tokens appear in the planted chunk text.

2. With ``retrieval_strategy="keyword"`` (pure sparse), text_matches also
   equals the hit count.

3. With ``retrieval_strategy="structural"`` the dense-only path still works
   and text_matches == 0 (regression guard).

4. ACL: workspace_id scoping is honoured — a different workspace sees no
   hits.

5. as_of=None applies the current-only filter (valid_to IS NULL) on BOTH
   the dense and sparse legs — end-dated points do not leak into hybrid
   results.

The tests run only when Docker is available (same guard as
``test_qdrant_contract.py``).  They do NOT need Neo4j — the anchor stage
is skipped by not supplying ``graphrag_anchor`` in ``filters``, so the
vector stage runs unscoped.  The ``force_graphrag`` fixture bypasses the
type-check that normally requires Neo4j+Qdrant, so the real
``QdrantVectorStore`` is exercised against the real container.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
import pytest_asyncio

pytest.importorskip("testcontainers.qdrant")

from omniscience_index.stores.qdrant_config import QdrantConfig
from omniscience_index.stores.qdrant_store import QdrantVectorStore
from omniscience_retrieval.graph_rag import GraphRAGComposer
from omniscience_retrieval.models import (
    QueryStats,
    SearchRequest,
    SearchResult,
)
from qdrant_client import AsyncQdrantClient


def _docker_available() -> bool:
    """Cheap docker reachability probe to skip on CI without Docker."""
    if os.environ.get("SKIP_TESTCONTAINERS") == "1":
        return False
    try:
        import docker  # type: ignore[import-not-found]

        docker.from_env().ping()  # type: ignore[no-untyped-call]
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker is not reachable; GraphRAG hybrid contract tests need testcontainers.",
)


# ---------------------------------------------------------------------------
# Shared embedding and Qdrant fixtures
# ---------------------------------------------------------------------------


class _StubEmbedding:
    """Deterministic unit-norm embedding provider (dim=8).

    Identical to the stub used by ``test_qdrant_contract.py`` — same
    hash-based vectors so queries on planted chunk texts find the
    planted points.
    """

    dim = 8
    model_name = "stub-embedding"
    provider_name = "stub"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import math as _math

        out: list[list[float]] = []
        for t in texts:
            h = hash(t)
            raw = [((h >> (i * 4)) & 0xF) / 15.0 for i in range(self.dim)]
            n = _math.sqrt(sum(x * x for x in raw)) or 1.0
            out.append([x / n for x in raw])
        return out

    async def close(self) -> None:
        return None


@pytest.fixture(scope="session")
def qdrant_container_graphrag() -> Any:
    """Start a Qdrant container for the whole test session.

    Separate fixture name from ``test_qdrant_contract.py`` so the two
    modules can each manage their own session-scoped container without
    interference.
    """
    from testcontainers.qdrant import QdrantContainer

    with QdrantContainer(image="qdrant/qdrant:v1.12.4") as c:
        yield c


@pytest_asyncio.fixture()
async def qdrant_store_graphrag(
    qdrant_container_graphrag: Any,
) -> AsyncIterator[QdrantVectorStore]:
    """Fresh adapter instance with a unique collection per test."""
    host = qdrant_container_graphrag.get_container_host_ip()
    http_port = int(qdrant_container_graphrag.get_exposed_port(6333))
    grpc_port = int(qdrant_container_graphrag.get_exposed_port(6334))
    config = QdrantConfig(
        host=host,
        grpc_port=grpc_port,
        http_port=http_port,
        api_key=None,
        prefer_grpc=False,
    )
    store = QdrantVectorStore(config=config, embedding_provider=_StubEmbedding())
    store._collection_name = f"omniscience__stub-embedding__d8__t{uuid.uuid4().hex[:8]}"  # type: ignore[attr-defined]
    await store.connect()
    try:
        yield store
    finally:
        if store._client is not None:  # type: ignore[attr-defined]
            client = cast("AsyncQdrantClient", store._client)  # type: ignore[attr-defined]
            with contextlib.suppress(Exception):
                await client.delete_collection(store.collection_name)
        await store.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_vector(text: str, dim: int = 8) -> list[float]:
    """Reproduce the _StubEmbedding hash-based vector."""
    import math as _math

    h = hash(text)
    raw = [((h >> (i * 4)) & 0xF) / 15.0 for i in range(dim)]
    n = _math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / n for x in raw]


def _chunk(*, text: str, ord_: int = 0, vector: list[float] | None = None) -> dict[str, Any]:
    from omniscience_core.storage import ChunkPayload

    payload: ChunkPayload = {
        "ord": ord_,
        "text": text,
        "embedding": vector if vector is not None else _stub_vector(text),
    }
    return payload  # type: ignore[return-value]


async def _plant(
    store: QdrantVectorStore,
    *,
    workspace: uuid.UUID,
    external_id: str,
    text: str,
    source_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Upsert a single-chunk document and return its source_id."""
    src = source_id or uuid.uuid4()
    await store.upsert_chunks(
        source_id=src,
        external_id=external_id,
        uri=f"mem://{external_id}",
        title=external_id,
        content_hash=f"hash-{external_id}",
        metadata={"workspace_id": str(workspace)},
        chunks=[_chunk(text=text)],
    )
    return src


class _NoopGraphStore:
    """Graph store stub that always raises entity_not_found.

    No anchor resolution; the vector stage runs unscoped.  Satisfies the
    ``GraphStore`` protocol for ``GraphRAGComposer.__init__``.
    """

    async def traverse(self, *, entity_name: str, workspace_id: uuid.UUID, **_: Any) -> None:  # type: ignore[override]
        raise ValueError(f"entity_not_found:{entity_name}")

    async def find_related(self, *, entity_name: str, workspace_id: uuid.UUID, **_: Any) -> None:  # type: ignore[override]
        raise ValueError(f"entity_not_found:{entity_name}")


class _NullLegacyService:
    """Legacy fallback that always returns an empty result."""

    async def search(self, request: SearchRequest) -> SearchResult:
        return SearchResult(
            hits=[],
            query_stats=QueryStats(
                total_matches_before_filters=0,
                vector_matches=0,
                text_matches=0,
                duration_ms=0.0,
            ),
        )


def _build_composer(store: QdrantVectorStore, monkeypatch: pytest.MonkeyPatch) -> GraphRAGComposer:
    """Build a GraphRAGComposer wired to the real Qdrant store.

    The type-check that normally requires Neo4j+Qdrant is bypassed so we
    can test the composed pipeline against the real vector store without
    spinning up a Neo4j container.

    strict_epoch=False: these tests exercise hybrid text-matching mechanics,
    NOT epoch pinning.  No global_reconciler is wired, so per_source_watermark
    stays {} (empty dict).  Under strict_epoch=True (default), an empty map
    drops ALL versioned hits — defeating the purpose of these retrieval tests.
    strict_epoch=False restores the pass-through behaviour for unmapped sources.
    The AP1 fail-closed invariant (strict_epoch=True with a real reconciler) is
    covered by tests/conformance/test_ap3_no_mixed_epoch.py.
    """
    import omniscience_retrieval.graph_rag as gr_module

    monkeypatch.setattr(gr_module, "_should_use_graphrag", lambda _g, _v: True)
    graph_store = cast("Any", _NoopGraphStore())
    return GraphRAGComposer(
        graph_store=graph_store,
        vector_store=store,
        legacy_service=_NullLegacyService(),
        # strict_epoch=False: tests retrieval mechanics only, not epoch pinning;
        # AP1 fail-closed invariant covered in tests/conformance/test_ap3_no_mixed_epoch.py
        strict_epoch=False,
    )


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hybrid_graphrag_text_matches_nonzero(
    qdrant_store_graphrag: QdrantVectorStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hybrid strategy through GraphRAGComposer returns text_matches > 0.

    This is the primary regression test: before fix/push-sources-and-graphrag-sparse,
    text_matches was always 0 because the GraphRAG vector stage was not
    dispatching to the RRF path.

    Scenario:
    - Plant a chunk containing the distinctive keyword "crashloopbackoff".
    - Search with query="crashloopbackoff" and retrieval_strategy="hybrid".
    - Assert text_matches > 0 (the sparse leg found the keyword).
    """
    ws = uuid.uuid4()
    await _plant(
        qdrant_store_graphrag,
        workspace=ws,
        external_id="pod-crash-doc",
        text="pod crashloopbackoff kubernetes restart error",
    )

    composer = _build_composer(qdrant_store_graphrag, monkeypatch)
    result = await composer.search(
        SearchRequest(
            query="crashloopbackoff",
            retrieval_strategy="hybrid",
            top_k=5,
        ),
        workspace_id=ws,
    )

    assert len(result.hits) >= 1, "Expected at least one hit for exact keyword query"
    assert result.query_stats.text_matches > 0, (
        f"Expected text_matches > 0 for hybrid query through GraphRAGComposer; "
        f"got text_matches={result.query_stats.text_matches}. "
        "This indicates the sparse leg was not exercised — "
        "retrieval_strategy was not forwarded correctly to QdrantVectorStore."
    )


@pytest.mark.asyncio
async def test_keyword_graphrag_text_matches_equals_hits(
    qdrant_store_graphrag: QdrantVectorStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """keyword strategy through GraphRAGComposer: text_matches == len(hits).

    With pure sparse search every returned point is a text match.
    """
    ws = uuid.uuid4()
    await _plant(
        qdrant_store_graphrag,
        workspace=ws,
        external_id="oom-doc",
        text="pod oomkilled memory limit exceeded kubernetes",
    )

    composer = _build_composer(qdrant_store_graphrag, monkeypatch)
    result = await composer.search(
        SearchRequest(
            query="oomkilled memory",
            retrieval_strategy="keyword",
            top_k=5,
        ),
        workspace_id=ws,
    )

    assert len(result.hits) >= 1
    assert result.query_stats.text_matches == len(result.hits), (
        "For keyword strategy, every hit is a sparse match; "
        f"text_matches={result.query_stats.text_matches} != hits={len(result.hits)}"
    )


@pytest.mark.asyncio
async def test_structural_graphrag_text_matches_zero(
    qdrant_store_graphrag: QdrantVectorStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """structural strategy through GraphRAGComposer: text_matches == 0.

    structural → dense-only → _search_dense which always returns text_matches=0.
    This is the expected behaviour for the graph-anchor-scoped dense path.
    """
    ws = uuid.uuid4()
    await _plant(
        qdrant_store_graphrag,
        workspace=ws,
        external_id="evicted-doc",
        text="pod evicted node pressure disk pressure memory",
    )

    composer = _build_composer(qdrant_store_graphrag, monkeypatch)
    result = await composer.search(
        SearchRequest(
            query="evicted node pressure",
            retrieval_strategy="structural",
            top_k=5,
        ),
        workspace_id=ws,
    )

    assert len(result.hits) >= 1
    assert result.query_stats.text_matches == 0, (
        "structural strategy must use dense-only path; "
        f"unexpected text_matches={result.query_stats.text_matches}"
    )


@pytest.mark.asyncio
async def test_hybrid_graphrag_workspace_isolation(
    qdrant_store_graphrag: QdrantVectorStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-workspace isolation holds on the hybrid GraphRAG path.

    Workspace A's chunk must not appear in workspace B's search results.
    """
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()

    # Plant the same keyword text in both workspaces.
    await _plant(
        qdrant_store_graphrag,
        workspace=ws_a,
        external_id="pvc-doc-a",
        text="persistentvolumeclaim storage pvc kubernetes bound",
    )
    await _plant(
        qdrant_store_graphrag,
        workspace=ws_b,
        external_id="pvc-doc-b",
        text="persistentvolumeclaim storage pvc kubernetes bound",
    )

    composer = _build_composer(qdrant_store_graphrag, monkeypatch)

    a_result = await composer.search(
        SearchRequest(query="persistentvolumeclaim", retrieval_strategy="hybrid", top_k=20),
        workspace_id=ws_a,
    )
    b_result = await composer.search(
        SearchRequest(query="persistentvolumeclaim", retrieval_strategy="hybrid", top_k=20),
        workspace_id=ws_b,
    )

    a_chunk_ids = {h.chunk_id for h in a_result.hits}
    b_chunk_ids = {h.chunk_id for h in b_result.hits}

    assert a_chunk_ids.isdisjoint(b_chunk_ids), (
        "Cross-workspace contamination: chunks from workspace A appeared in workspace B's "
        "hybrid GraphRAG results (or vice versa)."
    )
    assert len(a_result.hits) >= 1
    assert len(b_result.hits) >= 1


@pytest.mark.asyncio
async def test_as_of_none_current_only_hybrid(
    qdrant_store_graphrag: QdrantVectorStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """as_of=None applies current-only filter (valid_to IS NULL) on both legs.

    Regression guard for fix/push-sources-and-graphrag-sparse Stage 1:
    end-dated (historical) points must NOT appear in hybrid results when
    as_of=None.

    Scenario:
    - Plant chunk v1 (current: valid_to=None).
    - Update with different content → end-dates v1 (valid_to=now) and
      creates v2 (valid_to=None).
    - hybrid search must return only v2, not v1.
    """

    ws = uuid.uuid4()
    source_id = uuid.uuid4()
    external_id = "timeout-doc"

    # v1 — planted first.
    v1_text = "database connection timeout milliseconds latency"
    v1_embedding = _stub_vector(v1_text)

    await qdrant_store_graphrag.upsert_chunks(
        source_id=source_id,
        external_id=external_id,
        uri=f"mem://{external_id}",
        title=external_id,
        content_hash="hash-v1",
        metadata={"workspace_id": str(ws)},
        chunks=[_chunk(text=v1_text, vector=v1_embedding)],
    )

    # v2 — update with different content → v1 is end-dated.
    v2_text = "database connection timeout milliseconds latency updated pooling"
    v2_embedding = _stub_vector(v2_text)

    await qdrant_store_graphrag.upsert_chunks(
        source_id=source_id,
        external_id=external_id,
        uri=f"mem://{external_id}",
        title=external_id,
        content_hash="hash-v2",
        metadata={"workspace_id": str(ws)},
        chunks=[_chunk(text=v2_text, vector=v2_embedding)],
    )

    composer = _build_composer(qdrant_store_graphrag, monkeypatch)
    result = await composer.search(
        SearchRequest(
            query="database connection timeout",
            retrieval_strategy="hybrid",
            top_k=10,
        ),
        workspace_id=ws,
        as_of=None,
    )

    # Only v2 should appear (v1 has valid_to set).
    hit_texts = [h.text for h in result.hits]
    assert v2_text in hit_texts, f"Expected v2 text in results but got: {hit_texts}"
    assert v1_text not in hit_texts, (
        f"v1 (end-dated) text leaked into hybrid results: {hit_texts}. "
        "as_of=None current-only filter was NOT applied on the sparse/hybrid leg."
    )
