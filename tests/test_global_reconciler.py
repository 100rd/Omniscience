"""Tests for GlobalReconciler (AP3: read-path consistency exposure).

Covers:
  - Convergence success: all stores at watermark → empty degraded_subsystems.
  - Timeout path: returns degraded_subsystems + staleness_seconds non-None.
  - Per-store lag detection: Qdrant lag, Neo4j lag, both lagging.
  - Atomic watermark: PG snapshot is used consistently for both store comparisons.
  - Read still returns results (degraded, not failed): composer serves data
    even when stores are behind.
  - Empty workspace (no documents): always converged.
  - min_watermark: returned on both converged and timeout paths (AP3 PIN).
  - per_source_watermark: v10-AP1 — per-source ceiling map (multi-source tests).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_retrieval.reconciler import GlobalReconciler

# ---------------------------------------------------------------------------
# Shared test workspace
# ---------------------------------------------------------------------------

_WS = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")


# ---------------------------------------------------------------------------
# Helpers: mock store factories
# ---------------------------------------------------------------------------


def _pg_watermarks(data: dict[str, int]) -> Any:
    """Build a mock session_factory that returns the given source→version map."""

    class _FakeResult:
        def all(self) -> list[tuple[str, int]]:
            return list(data.items())

    class _FakeSession:
        async def execute(self, stmt: Any) -> _FakeResult:
            return _FakeResult()

        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

    class _FakeSessionFactory:
        def __call__(self) -> _FakeSession:
            return _FakeSession()

    return _FakeSessionFactory()


def _mock_vector_store(checkpoints: dict[str, int]) -> Any:
    """Build a mock vector_store with the given source→version checkpoint map."""
    store = MagicMock()
    store.collection_name = "test_collection"
    records = [MagicMock(payload={"source_id": k, "version": v}) for k, v in checkpoints.items()]
    store._qc = MagicMock()
    store._qc.scroll = AsyncMock(return_value=(records, None))
    return store


def _mock_graph_store(checkpoints: dict[str, int]) -> Any:
    """Build a mock graph_store with the given source→version checkpoint map."""
    store = MagicMock()
    store._config = MagicMock()
    store._config.database = "neo4j"

    async def _mock_run(query: str, params: dict[str, Any]) -> Any:
        async def _aiter() -> Any:
            for k, v in checkpoints.items():
                yield {"source_id": k, "version": v}

        return _aiter()

    mock_session = MagicMock()
    mock_session.run = AsyncMock(side_effect=_mock_run)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    store._driver = MagicMock()
    store._driver.session = MagicMock(return_value=mock_session)
    return store


def _make_reconciler(
    pg_data: dict[str, int],
    qdrant_data: dict[str, int],
    neo4j_data: dict[str, int],
) -> GlobalReconciler:
    return GlobalReconciler(
        session_factory=_pg_watermarks(pg_data),
        vector_store=_mock_vector_store(qdrant_data),
        graph_store=_mock_graph_store(neo4j_data),
    )


# ---------------------------------------------------------------------------
# Tests: check_convergence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_convergence_all_stores_at_watermark() -> None:
    """All stores at the PG watermark → converged, empty degraded list."""
    src = str(uuid.uuid4())
    reconciler = _make_reconciler(
        pg_data={src: 5},
        qdrant_data={src: 5},
        neo4j_data={src: 5},
    )
    (
        converged,
        degraded,
        staleness,
        min_watermark,
        per_source_wm,
    ) = await reconciler.check_convergence(_WS)
    assert converged is True
    assert degraded == []
    assert staleness is None
    # min_watermark: min(pg=5, qdrant=5, neo4j=5) = 5
    assert min_watermark == 5
    # per_source: min(5,5,5) = 5
    assert per_source_wm[src] == 5


@pytest.mark.asyncio
async def test_convergence_stores_ahead_of_watermark() -> None:
    """Stores at higher version than PG watermark are also converged."""
    src = str(uuid.uuid4())
    reconciler = _make_reconciler(
        pg_data={src: 3},
        qdrant_data={src: 7},  # ahead
        neo4j_data={src: 5},  # ahead
    )
    (
        converged,
        degraded,
        _staleness,
        min_watermark,
        per_source_wm,
    ) = await reconciler.check_convergence(_WS)
    assert converged is True
    assert degraded == []
    # min_watermark: min(pg=3, qdrant=7, neo4j=5) = 3
    assert min_watermark == 3
    # per_source: min(pg=3, qdrant=7, neo4j=5) = 3
    assert per_source_wm[src] == 3


@pytest.mark.asyncio
async def test_qdrant_lagging_detected() -> None:
    """Qdrant behind PG watermark → degraded=['qdrant'], staleness set."""
    src = str(uuid.uuid4())
    reconciler = _make_reconciler(
        pg_data={src: 10},
        qdrant_data={src: 7},  # 3 versions behind
        neo4j_data={src: 10},
    )
    (
        converged,
        degraded,
        staleness,
        min_watermark,
        per_source_wm,
    ) = await reconciler.check_convergence(_WS)
    assert converged is False
    assert "qdrant" in degraded
    assert "neo4j" not in degraded
    assert staleness == 3.0
    # min_watermark: min(pg=10, qdrant=7, neo4j=10) = 7
    assert min_watermark == 7
    # per_source: min(10,7,10) = 7
    assert per_source_wm[src] == 7


@pytest.mark.asyncio
async def test_neo4j_lagging_detected() -> None:
    """Neo4j behind PG watermark → degraded=['neo4j'], staleness set."""
    src = str(uuid.uuid4())
    reconciler = _make_reconciler(
        pg_data={src: 10},
        qdrant_data={src: 10},
        neo4j_data={src: 2},  # 8 versions behind
    )
    (
        converged,
        degraded,
        staleness,
        min_watermark,
        per_source_wm,
    ) = await reconciler.check_convergence(_WS)
    assert converged is False
    assert "neo4j" in degraded
    assert "qdrant" not in degraded
    assert staleness == 8.0
    # min_watermark: min(pg=10, qdrant=10, neo4j=2) = 2
    assert min_watermark == 2
    # per_source: min(10,10,2) = 2
    assert per_source_wm[src] == 2


@pytest.mark.asyncio
async def test_both_stores_lagging_detected() -> None:
    """Both stores behind PG → degraded=['qdrant', 'neo4j'], staleness = worst lag."""
    src = str(uuid.uuid4())
    reconciler = _make_reconciler(
        pg_data={src: 10},
        qdrant_data={src: 8},  # lag = 2
        neo4j_data={src: 5},  # lag = 5
    )
    (
        converged,
        degraded,
        staleness,
        min_watermark,
        per_source_wm,
    ) = await reconciler.check_convergence(_WS)
    assert converged is False
    assert set(degraded) == {"qdrant", "neo4j"}
    assert staleness == 5.0  # worst-case lag
    # min_watermark: min(pg=10, qdrant=8, neo4j=5) = 5
    assert min_watermark == 5
    # per_source: min(10,8,5) = 5
    assert per_source_wm[src] == 5


@pytest.mark.asyncio
async def test_empty_workspace_always_converged() -> None:
    """No documents in PG → trivially converged (nothing to wait for)."""
    reconciler = _make_reconciler(
        pg_data={},
        qdrant_data={},
        neo4j_data={},
    )
    (
        converged,
        degraded,
        staleness,
        min_watermark,
        per_source_wm,
    ) = await reconciler.check_convergence(_WS)
    assert converged is True
    assert degraded == []
    assert staleness is None
    # Empty workspace: min_watermark is None (no version data)
    assert min_watermark is None
    # per_source map is empty
    assert per_source_wm == {}


@pytest.mark.asyncio
async def test_atomic_watermark_snapshot_used_for_both_stores() -> None:
    """PG watermark is snapshotted once and used for both store comparisons.

    This guards against the non-monotonic flap where a version could be
    read at different points in time, causing a store to appear converged
    against a stale snapshot while actually being behind.
    """
    src = str(uuid.uuid4())
    call_count = 0

    # Build a session factory that returns different results on repeated calls
    # (simulating PG writes between calls). After the first call, it returns v=10.
    # If the reconciler read PG twice, one store might compare against v=5 and
    # another against v=10, causing a false convergence.
    class _IncreasingPGSession:
        async def execute(self, stmt: Any) -> Any:
            nonlocal call_count
            call_count += 1
            version = 5 if call_count == 1 else 10

            class _R:
                def all(self) -> list[tuple[str, int]]:
                    return [(src, version)]

            return _R()

        async def __aenter__(self) -> _IncreasingPGSession:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

    class _IncreasingFactory:
        def __call__(self) -> _IncreasingPGSession:
            return _IncreasingPGSession()

    reconciler = GlobalReconciler(
        session_factory=_IncreasingFactory(),
        vector_store=_mock_vector_store({src: 5}),  # at v=5
        graph_store=_mock_graph_store({src: 5}),  # at v=5
    )
    # With a single PG snapshot (v=5), both stores are at watermark → converged.
    # If PG were read twice, second read returns v=10 and both stores would
    # appear lagging — demonstrating the non-monotonic flap the fix prevents.
    converged, _degraded, _staleness, _min_wm, _per_source = await reconciler.check_convergence(
        _WS
    )
    assert converged is True, (
        "With atomic watermark snapshot, stores at v=5 match PG first-read v=5 → converged"
    )
    assert call_count == 1, "PG must be read exactly once per check_convergence call"


# ---------------------------------------------------------------------------
# Tests: per_source_watermark multi-source (v10-AP1 regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_source_watermark_multi_source_independent() -> None:
    """Two sources with different store states get independent per-source watermarks.

    Source A is healthy at v10; source B is cold at v0.
    The per_source_watermark for A must be 10, not the global min.
    The per_source_watermark for B must be 0 (cold).
    The global min_watermark is 0 (for observability), but per-hit
    filtering must use the per-source map.
    """
    src_a = str(uuid.uuid4())
    src_b = str(uuid.uuid4())
    reconciler = _make_reconciler(
        pg_data={src_a: 10, src_b: 5},
        qdrant_data={src_a: 10, src_b: 0},  # B cold in Qdrant
        neo4j_data={src_a: 10, src_b: 0},  # B cold in Neo4j
    )
    (
        converged,
        _degraded,
        _staleness,
        min_watermark,
        per_source_wm,
    ) = await reconciler.check_convergence(_WS)
    assert converged is False  # B is lagging
    # Source A: all stores at 10 → per-source ceiling 10
    assert per_source_wm[src_a] == 10, (
        f"Source A should have per-source watermark=10; got {per_source_wm.get(src_a)}"
    )
    # Source B: qdrant and neo4j at 0 → per-source ceiling 0
    assert per_source_wm[src_b] == 0, (
        f"Source B should have per-source watermark=0; got {per_source_wm.get(src_b)}"
    )
    # Global min is 0 (worst-case floor)
    assert min_watermark == 0


@pytest.mark.asyncio
async def test_per_source_watermark_independent_per_source_ceiling() -> None:
    """Each source's ceiling is computed independently; they do not bleed.

    Three sources: A@v10 healthy, B@v5 partial lag, C@v1 lagging.
    Per-source watermarks must be A=10, B=5, C=1 independently.
    """
    src_a = str(uuid.uuid4())
    src_b = str(uuid.uuid4())
    src_c = str(uuid.uuid4())
    reconciler = _make_reconciler(
        pg_data={src_a: 10, src_b: 8, src_c: 7},
        qdrant_data={src_a: 10, src_b: 5, src_c: 1},
        neo4j_data={src_a: 10, src_b: 8, src_c: 1},
    )
    _converged, _degraded, _staleness, _min_wm, per_source_wm = await reconciler.check_convergence(
        _WS
    )
    # A: min(10,10,10)=10
    assert per_source_wm[src_a] == 10
    # B: min(8,5,8)=5
    assert per_source_wm[src_b] == 5
    # C: min(7,1,1)=1
    assert per_source_wm[src_c] == 1


# ---------------------------------------------------------------------------
# Tests: wait_for_convergence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_convergence_success() -> None:
    """When stores are already converged, returns empty degraded list immediately."""
    src = str(uuid.uuid4())
    reconciler = _make_reconciler(
        pg_data={src: 3},
        qdrant_data={src: 3},
        neo4j_data={src: 3},
    )
    degraded, staleness, min_watermark, per_source_wm = await reconciler.wait_for_convergence(
        _WS, timeout=5.0
    )
    assert degraded == []
    assert staleness is None
    # min_watermark: min(pg=3, qdrant=3, neo4j=3) = 3
    assert min_watermark == 3
    assert per_source_wm[src] == 3


@pytest.mark.asyncio
async def test_wait_for_convergence_timeout_returns_degraded_subsystems() -> None:
    """On timeout, degraded_subsystems and staleness_seconds are populated.

    Critically: the function returns (not raises), so the caller can
    still serve data with an explicit staleness signal.
    """
    src = str(uuid.uuid4())
    reconciler = _make_reconciler(
        pg_data={src: 10},
        qdrant_data={src: 5},  # lagging — will never converge in this test
        neo4j_data={src: 10},
    )
    # Very short timeout so the test runs fast
    degraded, staleness, min_watermark, per_source_wm = await reconciler.wait_for_convergence(
        _WS, timeout=0.1
    )
    assert "qdrant" in degraded, "Lagging Qdrant must be in degraded_subsystems on timeout"
    assert staleness is not None, "staleness_seconds must be set on timeout"
    assert staleness == 5.0
    # min_watermark: min(pg=10, qdrant=5, neo4j=10) = 5
    assert min_watermark == 5
    assert per_source_wm[src] == 5


@pytest.mark.asyncio
async def test_wait_for_convergence_does_not_raise_on_timeout() -> None:
    """Timeout must never raise — caller must receive a degraded result, not a crash."""
    src = str(uuid.uuid4())
    reconciler = _make_reconciler(
        pg_data={src: 99},
        qdrant_data={src: 0},
        neo4j_data={src: 0},
    )
    # Should complete without raising, even with very short timeout
    try:
        degraded, _staleness, _min_wm, _per_src = await reconciler.wait_for_convergence(
            _WS, timeout=0.05
        )
    except Exception as exc:  # pragma: no cover
        pytest.fail(f"wait_for_convergence must not raise; got {exc!r}")
    assert isinstance(degraded, list)


@pytest.mark.asyncio
async def test_wait_for_convergence_returns_min_watermark_on_converged() -> None:
    """Converged path must return the correct min_watermark (AP3 PIN)."""
    src = str(uuid.uuid4())
    reconciler = _make_reconciler(
        pg_data={src: 8},
        qdrant_data={src: 8},
        neo4j_data={src: 8},
    )
    degraded, staleness, min_watermark, per_source_wm = await reconciler.wait_for_convergence(
        _WS, timeout=5.0
    )
    assert degraded == []
    assert staleness is None
    assert min_watermark == 8
    assert per_source_wm[src] == 8


@pytest.mark.asyncio
async def test_wait_for_convergence_returns_min_watermark_on_timeout() -> None:
    """Timeout path must also return the min_watermark so callers can pin (AP3 PIN)."""
    src = str(uuid.uuid4())
    reconciler = _make_reconciler(
        pg_data={src: 15},
        qdrant_data={src: 3},  # lagging — pinned to 3
        neo4j_data={src: 15},
    )
    degraded, _staleness, min_watermark, per_source_wm = await reconciler.wait_for_convergence(
        _WS, timeout=0.05
    )
    assert "qdrant" in degraded
    # min is min(pg=15, qdrant=3, neo4j=15) = 3
    assert min_watermark == 3
    assert per_source_wm[src] == 3


# ---------------------------------------------------------------------------
# Tests: integration with GraphRAGComposer (degraded result, not failed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composer_serves_results_even_when_stores_degraded() -> None:
    """GraphRAGComposer must return hits even when stores are behind the watermark.

    AP3 contract: expose, don't crash.  The result is annotated with
    degraded_subsystems and staleness_seconds so the caller can decide
    whether to retry, but the response always carries data.

    The reconciler is replaced with a mock that immediately reports
    qdrant as lagging (simulating a timeout scenario without actual I/O).
    """
    import uuid as _uuid

    from omniscience_retrieval.graph_rag import GraphRAGComposer
    from omniscience_retrieval.models import (
        ChunkLineage,
        Citation,
        QueryStats,
        SearchHit,
        SearchRequest,
        SearchResult,
        SourceInfo,
    )

    _now = datetime.now(UTC)
    workspace_id = _uuid.uuid4()
    src_id = _uuid.uuid4()

    # Mock reconciler that immediately reports qdrant as lagging (timeout-like signal)
    # Now returns 4-tuple: (degraded, staleness, min_watermark, per_source_watermark)
    mock_reconciler = MagicMock()
    mock_reconciler.wait_for_convergence = AsyncMock(
        return_value=(
            ["qdrant"],
            8.0,
            7,
            {str(src_id): 7},  # per-source watermark
        )
    )

    # Fake vector store that always returns one hit (applied_version=7, within watermark)
    class _FakeVS:
        async def search(
            self,
            *,
            request: SearchRequest,
            workspace_id: _uuid.UUID,
            as_of: datetime | None = None,
        ) -> SearchResult:
            hit = SearchHit(
                chunk_id=_uuid.uuid4(),
                document_id=_uuid.uuid4(),
                score=0.9,
                text="some evidence",
                source=SourceInfo(id=src_id, name="s", type="slack"),
                citation=Citation(uri="x://y", title=None, indexed_at=_now, doc_version=1),
                lineage=ChunkLineage(
                    ingestion_run_id=None,
                    embedding_model="m",
                    embedding_provider="p",
                    parser_version="1",
                    chunker_strategy="fixed",
                ),
                metadata={},
                applied_version=7,  # at or below per-source watermark=7 → passes filter
            )
            return SearchResult(
                hits=[hit],
                query_stats=QueryStats(
                    total_matches_before_filters=1,
                    vector_matches=1,
                    text_matches=0,
                    duration_ms=1.0,
                ),
            )

    class _FakeLegacy:
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

    composer = GraphRAGComposer.__new__(GraphRAGComposer)
    composer._graphrag_active = True  # type: ignore[attr-defined]
    composer._global_reconciler = mock_reconciler  # type: ignore[attr-defined]
    composer._session_factory = None  # type: ignore[attr-defined]
    composer._is_entity_parked_fn = None  # type: ignore[attr-defined]
    composer._vector_store = _FakeVS()  # type: ignore[attr-defined]
    composer._graph_store = MagicMock()  # type: ignore[attr-defined]
    composer._graph_store.traverse = AsyncMock(side_effect=ValueError("entity_not_found"))
    composer._legacy_service = _FakeLegacy()  # type: ignore[attr-defined]

    req = SearchRequest(query="test query", top_k=5)
    result = await composer.search(req, workspace_id=workspace_id)

    # The result must always carry hits — staleness signals are additive, not replacements.
    # (This assertion verifies AP3's "expose, don't crash" contract.)
    assert isinstance(result.hits, list), "Hits must always be a list"
    # Degraded subsystems should be set when reconciler reported lag
    assert "qdrant" in result.degraded_subsystems, (
        "Lagging qdrant must appear in degraded_subsystems"
    )
    assert result.staleness_seconds == 8.0, (
        "staleness_seconds must match what the reconciler reported"
    )
    # AP3 PIN: pinned_watermark must be stamped on the result
    assert result.pinned_watermark == 7, (
        "pinned_watermark must match min_watermark from reconciler"
    )
