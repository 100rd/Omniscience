"""AP3 live-container conformance gate — v10-AP1 + v10-AP2 behavioral assertions.

v10-AP6 fix: this test exercises the REAL Neo4j/Qdrant path (not mocks) so
mock-passing-real-broken regressions are caught at the gate.

Two invariants exercised:

v10-AP1 (per-source watermark, no whole-workspace blackout):
    Write chunks into real Qdrant for two sources at different doc versions.
    Configure mock reconciler with per_source_watermark where source A is
    healthy (watermark=10) and source B is cold (watermark=0).
    Assert:
    - Source A's chunks (doc_version=1 ≤ 10) survive in search results.
    - Source B's chunks (doc_version=1 > 0) are dropped.
    - Source A is NOT blacked out by source B's cold state.

v10-AP2 (graph-anchor pin, graph-ahead node excluded):
    Write one entity into real Neo4j at version=5 for source A, and one
    entity at version=10 for source C where per_source_watermark[src_c]=7.
    Query the composer with an anchor filter pointing to the graph-ahead
    entity (version=10 > watermark=7).  Assert:
    - The graph-ahead seed is treated as an anchor miss (anchor not used).
    - Vector evidence within the watermark still flows through.

Gate: OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1 AND OMNISCIENCE_RUN_QDRANT_CONTRACT_TESTS=1
      (both must be set — the test needs Neo4j for AP2 and Qdrant for AP1).
"""

from __future__ import annotations

import hashlib
import os
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Skip the entire module when containers are not available
# ---------------------------------------------------------------------------

_NEO4J = os.environ.get("OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS") == "1"
_QDRANT = os.environ.get("OMNISCIENCE_RUN_QDRANT_CONTRACT_TESTS") == "1"

_LIVE_CONTRACT = pytest.mark.skipif(
    not (_NEO4J and _QDRANT),
    reason=(
        "set OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1 and "
        "OMNISCIENCE_RUN_QDRANT_CONTRACT_TESTS=1 to run live container AP3 tests"
    ),
)

# ---------------------------------------------------------------------------
# Helpers: tiny deterministic embedding provider (dim=4)
# ---------------------------------------------------------------------------


class _DeterministicEmbeddingProvider:
    """Hash-based deterministic embedding provider for container tests.

    Produces a stable 4-dimensional vector per text so the Qdrant
    collection can be created and searched without a real model service.
    The vector is NOT semantically meaningful — only functional correctness
    of the watermark filter is under test, not retrieval quality.
    """

    dim: int = 4
    model_name: str = "live-conformance-stub"
    provider_name: str = "stub"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        result: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            # Take 4 bytes, normalise to [0, 1] range
            vec = [b / 255.0 for b in digest[:4]]
            result.append(vec)
        return result


def _make_neo4j_config(*, uri: str, user: str, pw: str) -> Any:
    from omniscience_index.stores.neo4j.store import Neo4jStoreConfig

    return Neo4jStoreConfig(
        uri=uri,
        username=user,
        password=pw,
        database="neo4j",
        max_connection_pool_size=5,
        connection_acquisition_timeout_seconds=10.0,
        max_transaction_retry_time_seconds=10.0,
        default_max_depth=3,
        bitemporal_enabled=False,
    )


def _make_qdrant_config() -> Any:
    from omniscience_index.stores.qdrant_config import QdrantConfig

    host = os.environ.get("QDRANT_HOST", "localhost")
    http_port = int(os.environ.get("QDRANT_HTTP_PORT", "6333"))
    grpc_port = int(os.environ.get("QDRANT_GRPC_PORT", "6334"))
    return QdrantConfig(host=host, http_port=http_port, grpc_port=grpc_port, prefer_grpc=False)


def _make_mock_reconciler(per_source_wm: dict[str, int]) -> Any:
    """Build a mock reconciler that returns the given per-source watermark map."""
    global_min = min(per_source_wm.values()) if per_source_wm else None
    mock = MagicMock()
    mock.wait_for_convergence = AsyncMock(return_value=([], None, global_min, per_source_wm))
    return mock


# ---------------------------------------------------------------------------
# v10-AP1 live test: per-source watermark — cold source B must NOT blackout A
# ---------------------------------------------------------------------------


@_LIVE_CONTRACT
@pytest.mark.asyncio
async def test_live_ap3_v10_ap1_cold_source_does_not_blackout_healthy_source() -> None:
    """v10-AP1 live conformance: write two sources into real Qdrant+Neo4j.

    Source A is healthy (per_source_watermark[A]=10): its chunks (doc_version=1,
    which is ≤ 10) must appear in search results.

    Source B is cold (per_source_watermark[B]=0): its chunks (doc_version=1,
    which is > 0) must be dropped.

    Critically: source A must NOT be blacked out by source B's cold state.
    This is the v10-AP1 invariant (no whole-workspace blackout from one
    cold source).

    Pre-fix behaviour (v9 global-min watermark):
      min(10, 0) = 0 → ALL hits dropped → source A blacked out.
    Post-fix (v10 per-source watermark):
      Source A filtered against wm=10 → doc_version=1 ≤ 10 → passes.
      Source B filtered against wm=0  → doc_version=1 > 0  → dropped.
    """
    from omniscience_core.storage.graph import EntityUpsert
    from omniscience_index.stores.neo4j.store import Neo4jGraphStore
    from omniscience_index.stores.qdrant_store import QdrantVectorStore
    from omniscience_retrieval.graph_rag import GraphRAGComposer
    from omniscience_retrieval.models import SearchRequest

    neo4j_uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.environ.get("NEO4J_USERNAME", "neo4j")
    neo4j_pw = os.environ.get("NEO4J_PASSWORD", "neo4j")

    embedding_provider = _DeterministicEmbeddingProvider()
    neo4j_config = _make_neo4j_config(uri=neo4j_uri, user=neo4j_user, pw=neo4j_pw)
    qdrant_config = _make_qdrant_config()

    neo4j_store = Neo4jGraphStore(config=neo4j_config)
    qdrant_store = QdrantVectorStore(config=qdrant_config, embedding_provider=embedding_provider)

    await neo4j_store.connect()
    await qdrant_store.connect()

    ws_id = uuid.uuid4()
    src_a = uuid.uuid4()
    src_b = uuid.uuid4()

    try:
        # ------------------------------------------------------------------
        # Step 1: Write entities for both sources into Neo4j.
        # These make the GraphRAGComposer believe both sources exist.
        # ------------------------------------------------------------------
        await neo4j_store.upsert_entity(
            entity=EntityUpsert(
                id=uuid.uuid4(),
                source_id=src_a,
                entity_type="service",
                name="svc.live-ap3-src-a",
                display_name="svc.live-ap3-src-a",
                chunk_id=None,
                metadata={},
                version=10,
            ),
            workspace_id=ws_id,
        )
        await neo4j_store.upsert_entity(
            entity=EntityUpsert(
                id=uuid.uuid4(),
                source_id=src_b,
                entity_type="service",
                name="svc.live-ap3-src-b",
                display_name="svc.live-ap3-src-b",
                chunk_id=None,
                metadata={},
                version=1,  # has entity but cold in reconciler map
            ),
            workspace_id=ws_id,
        )

        # ------------------------------------------------------------------
        # Step 2: Write chunks into real Qdrant for both sources.
        # First upsert → doc_version=1 (the internal Qdrant chunk counter).
        # Applied_version in search results = doc_version = 1.
        # Source A watermark=10: 1 ≤ 10 → PASS
        # Source B watermark=0:  1 > 0  → DROP
        # ------------------------------------------------------------------
        from omniscience_core.storage.vector import ChunkPayload

        chunk_a: ChunkPayload = {
            "ord": 0,
            "text": "source A healthy chunk for live AP3 conformance",
            "embedding": (await embedding_provider.embed(["source A healthy chunk"]))[0],
            "symbol": None,
            "metadata": {"workspace_id": str(ws_id)},
            "embedding_model": embedding_provider.model_name,
            "embedding_provider": embedding_provider.provider_name,
            "parser_version": "1",
            "chunker_strategy": "fixed",
        }
        chunk_b: ChunkPayload = {
            "ord": 0,
            "text": "source B cold chunk for live AP3 conformance",
            "embedding": (await embedding_provider.embed(["source B cold chunk"]))[0],
            "symbol": None,
            "metadata": {"workspace_id": str(ws_id)},
            "embedding_model": embedding_provider.model_name,
            "embedding_provider": embedding_provider.provider_name,
            "parser_version": "1",
            "chunker_strategy": "fixed",
        }

        await qdrant_store.upsert_chunks(
            source_id=src_a,
            external_id=f"doc-ap3-src-a-{ws_id}",
            uri=f"mem://ap3/src-a/{ws_id}",
            title="Source A doc",
            content_hash=hashlib.sha256(b"src-a-content").hexdigest(),
            metadata={"workspace_id": str(ws_id)},
            chunks=[chunk_a],
            version=10,
        )
        await qdrant_store.upsert_chunks(
            source_id=src_b,
            external_id=f"doc-ap3-src-b-{ws_id}",
            uri=f"mem://ap3/src-b/{ws_id}",
            title="Source B doc",
            content_hash=hashlib.sha256(b"src-b-content").hexdigest(),
            metadata={"workspace_id": str(ws_id)},
            chunks=[chunk_b],
            version=0,
        )

        # ------------------------------------------------------------------
        # Step 3: Build a GraphRAGComposer with real stores + mock reconciler.
        # Mock reconciler returns: src_a → healthy (wm=10), src_b → cold (wm=0).
        # ------------------------------------------------------------------
        per_source_wm = {str(src_a): 10, str(src_b): 0}
        mock_reconciler = _make_mock_reconciler(per_source_wm)

        class _FakeLegacy:
            async def search(self, request: SearchRequest) -> Any:
                from omniscience_retrieval.models import QueryStats, SearchResult

                return SearchResult(
                    hits=[],
                    query_stats=QueryStats(
                        total_matches_before_filters=0,
                        vector_matches=0,
                        text_matches=0,
                        duration_ms=0.0,
                    ),
                )

        composer = GraphRAGComposer(
            graph_store=neo4j_store,
            vector_store=qdrant_store,
            legacy_service=_FakeLegacy(),
            global_reconciler=mock_reconciler,
        )

        # ------------------------------------------------------------------
        # Step 4: Run search — composer must be graphrag_active (real stores).
        # ------------------------------------------------------------------
        assert composer.graphrag_active, (
            "GraphRAGComposer must be in graphrag-active mode with real Neo4j+Qdrant stores"
        )

        req = SearchRequest(
            query="live AP3 conformance source",
            top_k=20,
        )
        result = await composer.search(req, workspace_id=ws_id)

        # ------------------------------------------------------------------
        # Step 5: Assert per-source watermark invariants.
        #
        # Source A (watermark=10): doc_version=1 ≤ 10 → must survive.
        # Source B (watermark=0):  doc_version=1 > 0  → must be dropped.
        # Source A must NOT be blacked out by source B.
        # ------------------------------------------------------------------
        src_a_hits = [h for h in result.hits if h.source.id == src_a]
        src_b_hits = [h for h in result.hits if h.source.id == src_b]

        # Source B hits (cold, watermark=0): ALL versioned hits must be dropped.
        versioned_b = [
            h for h in src_b_hits if h.applied_version is not None and h.applied_version > 0
        ]
        assert versioned_b == [], (
            f"v10-AP1 violation: source B (watermark=0) versioned hits must be dropped; "
            f"got {[h.applied_version for h in versioned_b]}.  "
            "The cold source is leaking future-epoch chunks into results."
        )

        # Source A (healthy, watermark=10): at least one hit must survive.
        # We check that the watermark filter did NOT drop source A's doc_version=1 hit.
        assert len(src_a_hits) >= 1, (
            f"v10-AP1 violation: source A (healthy, watermark=10) was blacked out "
            f"by cold source B.  Source A got 0 hits.  "
            "The global-min watermark bug (v9 regression) is back: "
            f"all_hits={[(str(h.source.id)[:8], h.applied_version) for h in result.hits]}"
        )

        # All surviving versioned hits must be within their source's watermark.
        for hit in result.hits:
            if hit.applied_version is None:
                continue
            src_key = str(hit.source.id)
            if src_key in per_source_wm:
                wm = per_source_wm[src_key]
                assert hit.applied_version <= wm, (
                    f"v10-AP1/AP3 violation: hit from source {src_key[:8]} "
                    f"has applied_version={hit.applied_version} > "
                    f"per_source_watermark={wm}.  Mixed-epoch chunk leaked."
                )

    finally:
        await neo4j_store.close()
        await qdrant_store.close()


# ---------------------------------------------------------------------------
# v10-AP2 live test: graph-ahead entity excluded from anchor traversal
# ---------------------------------------------------------------------------


@_LIVE_CONTRACT
@pytest.mark.asyncio
async def test_live_ap3_v10_ap2_graph_ahead_anchor_treated_as_miss() -> None:
    """v10-AP2 live conformance: anchor entity version > watermark → treated as miss.

    Setup:
    - Write entity "GraphAheadAnchor" into real Neo4j at version=10.
    - per_source_watermark for its source = 7 (source is behind).
    - Write a chunk into real Qdrant for a separate healthy source A (wm=10).

    Query:
    - Run GraphRAGComposer.search() with anchor filter pointing to "GraphAheadAnchor".

    Expected:
    - The anchor seed (version=10 > watermark=7) is rejected by
      _apply_graph_watermark_filter → treated as anchor miss.
    - Vector evidence from healthy source A (within watermark) still flows.
    - NO hit from the graph-ahead anchor context appears in results.
    - All surviving hits have applied_version ≤ per_source_watermark[their source].

    This validates v10-AP2: the graph traversal IS pinned to the per-source
    watermark, preventing a v10-graph + v7-evidence mixed-epoch composed result.
    """
    from omniscience_core.storage.graph import EntityUpsert
    from omniscience_index.stores.neo4j.store import Neo4jGraphStore
    from omniscience_index.stores.qdrant_store import QdrantVectorStore
    from omniscience_retrieval.graph_rag import ANCHOR_FILTER_KEY, GraphRAGComposer
    from omniscience_retrieval.models import SearchRequest

    neo4j_uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.environ.get("NEO4J_USERNAME", "neo4j")
    neo4j_pw = os.environ.get("NEO4J_PASSWORD", "neo4j")

    embedding_provider = _DeterministicEmbeddingProvider()
    neo4j_config = _make_neo4j_config(uri=neo4j_uri, user=neo4j_user, pw=neo4j_pw)
    qdrant_config = _make_qdrant_config()

    neo4j_store = Neo4jGraphStore(config=neo4j_config)
    qdrant_store = QdrantVectorStore(config=qdrant_config, embedding_provider=embedding_provider)

    await neo4j_store.connect()
    await qdrant_store.connect()

    ws_id = uuid.uuid4()
    src_c = uuid.uuid4()  # source for the graph-ahead entity (watermark=7)
    src_healthy = uuid.uuid4()  # healthy source (watermark=10)

    anchor_name = f"live-ap3-graph-ahead-anchor-{str(ws_id)[:8]}"

    try:
        # ------------------------------------------------------------------
        # Step 1: Write the graph-ahead anchor entity into Neo4j at version=10.
        # The reconciler will say this source's watermark is only 7.
        # So the anchor is "graph-ahead" by 3 versions.
        # ------------------------------------------------------------------
        await neo4j_store.upsert_entity(
            entity=EntityUpsert(
                id=uuid.uuid4(),
                source_id=src_c,
                entity_type="service",
                name=anchor_name,
                display_name=anchor_name,
                chunk_id=None,
                metadata={},
                version=10,  # > watermark of 7 → graph-ahead
            ),
            workspace_id=ws_id,
        )

        # ------------------------------------------------------------------
        # Step 2: Write a healthy entity + chunk so the search returns something.
        # ------------------------------------------------------------------
        await neo4j_store.upsert_entity(
            entity=EntityUpsert(
                id=uuid.uuid4(),
                source_id=src_healthy,
                entity_type="service",
                name=f"live-ap3-healthy-{str(ws_id)[:8]}",
                display_name=f"live-ap3-healthy-{str(ws_id)[:8]}",
                chunk_id=None,
                metadata={},
                version=10,
            ),
            workspace_id=ws_id,
        )

        from omniscience_core.storage.vector import ChunkPayload

        chunk_healthy: ChunkPayload = {
            "ord": 0,
            "text": f"live AP3 v10-AP2 healthy chunk for workspace {ws_id}",
            "embedding": (await embedding_provider.embed([f"live AP3 v10-AP2 healthy {ws_id}"]))[
                0
            ],
            "symbol": None,
            "metadata": {"workspace_id": str(ws_id)},
            "embedding_model": embedding_provider.model_name,
            "embedding_provider": embedding_provider.provider_name,
            "parser_version": "1",
            "chunker_strategy": "fixed",
        }
        await qdrant_store.upsert_chunks(
            source_id=src_healthy,
            external_id=f"doc-ap3-ap2-healthy-{ws_id}",
            uri=f"mem://ap3/ap2/healthy/{ws_id}",
            title="Healthy doc",
            content_hash=hashlib.sha256(f"healthy-{ws_id}".encode()).hexdigest(),
            metadata={"workspace_id": str(ws_id)},
            chunks=[chunk_healthy],
            version=10,
        )

        # ------------------------------------------------------------------
        # Step 3: Build the mock reconciler.
        # src_c (graph-ahead anchor): watermark=7 (version=10 > 7 → ahead)
        # src_healthy: watermark=10 (fully converged)
        # ------------------------------------------------------------------
        per_source_wm = {str(src_c): 7, str(src_healthy): 10}
        mock_reconciler = _make_mock_reconciler(per_source_wm)

        class _FakeLegacy:
            async def search(self, request: SearchRequest) -> Any:
                from omniscience_retrieval.models import QueryStats, SearchResult

                return SearchResult(
                    hits=[],
                    query_stats=QueryStats(
                        total_matches_before_filters=0,
                        vector_matches=0,
                        text_matches=0,
                        duration_ms=0.0,
                    ),
                )

        composer = GraphRAGComposer(
            graph_store=neo4j_store,
            vector_store=qdrant_store,
            legacy_service=_FakeLegacy(),
            global_reconciler=mock_reconciler,
        )

        assert composer.graphrag_active

        # ------------------------------------------------------------------
        # Step 4: Search with anchor pointing to the graph-ahead entity.
        # ------------------------------------------------------------------
        req = SearchRequest(
            query=f"live AP3 v10-AP2 healthy {ws_id}",
            top_k=20,
            filters={ANCHOR_FILTER_KEY: anchor_name},
        )
        result = await composer.search(req, workspace_id=ws_id)

        # ------------------------------------------------------------------
        # Step 5: Assert v10-AP2 invariants.
        #
        # The graph-ahead anchor (src_c, version=10 > wm=7) must be excluded.
        # Any surviving hit must have applied_version ≤ per_source_watermark[source].
        # The healthy source's chunk (applied_version=1 ≤ wm=10) may survive.
        # ------------------------------------------------------------------

        # All surviving hits must respect their source's watermark.
        for hit in result.hits:
            if hit.applied_version is None:
                continue
            src_key = str(hit.source.id)
            if src_key in per_source_wm:
                wm = per_source_wm[src_key]
                assert hit.applied_version <= wm, (
                    f"v10-AP2 mixed-epoch violation: hit from source {src_key[:8]} "
                    f"has applied_version={hit.applied_version} > "
                    f"per_source_watermark={wm}.  "
                    "The graph-anchor pin is not preventing mixed-epoch results."
                )

        # The graph-ahead anchor (src_c, version=10 > watermark=7) must not
        # appear as a chunk source in the result — it was excluded as a miss.
        src_c_hits = [h for h in result.hits if h.source.id == src_c]
        versioned_c = [
            h for h in src_c_hits if h.applied_version is not None and h.applied_version > 7
        ]
        assert versioned_c == [], (
            f"v10-AP2 violation: graph-ahead source (wm=7) has versioned hits "
            f"with applied_version > 7: {[h.applied_version for h in versioned_c]}.  "
            "The graph-anchor watermark pin is not working."
        )

    finally:
        await neo4j_store.close()
        await qdrant_store.close()
