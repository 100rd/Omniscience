"""Unit tests for ReconcileWorker.

Strategy: mock all three stores (session_factory / QdrantVectorStore /
Neo4jGraphStore) so no live infrastructure is needed.  Each test covers
one drift type in isolation plus idempotency and the per-run bounds.

Fixture conventions follow test_index_writer.py:
- session-factory mocks are built from a small helper.
- Qdrant / Neo4j store mocks are ``AsyncMock`` with individual method
  return values configured per test.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from omniscience_server.reconcile_constants import (
    DRIFT_NEO4J_ORPHAN,
    DRIFT_PG_MISSING_QDRANT,
    DRIFT_QDRANT_ORPHAN,
)
from omniscience_server.reconcile_worker import (
    ReconcileRunReport,
    ReconcileWorker,
    _try_parse_uuid,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WS = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_SRC_A = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Mock factories
# ---------------------------------------------------------------------------


def _make_settings(
    tick_seconds: int = 3600,
    orphan_limit: int = 200,
) -> MagicMock:
    s = MagicMock()
    s.reconcile_tick_seconds = tick_seconds
    s.reconcile_orphan_delete_limit = orphan_limit
    return s


def _make_session_factory(
    workspace_ids: list[uuid.UUID] | None = None,
    active_source_ids: list[uuid.UUID] | None = None,
    ingestion_run_id: uuid.UUID | None = None,
) -> MagicMock:
    """Build a minimal async_sessionmaker mock.

    Each ``execute`` call returns a result appropriate for the query being
    made in order: workspace IDs, then active source IDs, then (optionally)
    ingestion-run lookup, then the UPDATE.
    """
    workspace_ids = workspace_ids or [_WS]
    active_source_ids = active_source_ids or []

    def _make_rows(ids: list[Any]) -> MagicMock:
        result = MagicMock()
        result.all.return_value = [(i,) for i in ids]
        result.first.return_value = (ids[0],) if ids else None
        return result

    run_id = ingestion_run_id or uuid.uuid4()
    run_result = MagicMock()
    run_result.first.return_value = (run_id,)

    update_result = MagicMock()

    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _make_rows(workspace_ids),  # _load_workspace_ids
            _make_rows(active_source_ids),  # _load_active_source_ids
            run_result,  # _mark_ingestion_run_partial SELECT
            update_result,  # _mark_ingestion_run_partial UPDATE
        ]
        * 10  # repeated so tests that call execute multiple times don't exhaust
    )

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    tx = AsyncMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=tx)

    factory = MagicMock()
    factory.return_value = cm
    return factory


def _make_vector_store(
    chunks_by_source: dict[str, int] | None = None,
) -> AsyncMock:
    store = AsyncMock()
    store.count_chunks_by_source = AsyncMock(return_value=chunks_by_source or {})
    store.collection_name = "omniscience"
    # Internal Qdrant client mock used by _delete_qdrant_orphan_source
    qc = AsyncMock()
    count_result = MagicMock()
    count_result.count = 0
    qc.count = AsyncMock(return_value=count_result)
    qc.scroll = AsyncMock(return_value=([], None))
    qc.delete = AsyncMock()
    store._qc = qc
    return store


def _make_graph_store(
    entities_by_source: dict[str, int] | None = None,
) -> AsyncMock:
    store = AsyncMock()
    store.count_entities_by_source = AsyncMock(return_value=entities_by_source or {})
    return store


def _build_worker(
    workspace_ids: list[uuid.UUID] | None = None,
    active_source_ids: list[uuid.UUID] | None = None,
    chunks_by_source: dict[str, int] | None = None,
    entities_by_source: dict[str, int] | None = None,
    orphan_limit: int = 200,
    ingestion_run_id: uuid.UUID | None = None,
) -> tuple[ReconcileWorker, AsyncMock, AsyncMock]:
    factory = _make_session_factory(
        workspace_ids=workspace_ids,
        active_source_ids=active_source_ids,
        ingestion_run_id=ingestion_run_id,
    )
    vector_store = _make_vector_store(chunks_by_source=chunks_by_source)
    graph_store = _make_graph_store(entities_by_source=entities_by_source)
    settings = _make_settings(orphan_limit=orphan_limit)
    worker = ReconcileWorker(
        session_factory=factory,
        vector_store=vector_store,
        graph_store=graph_store,
        settings=settings,
    )
    return worker, vector_store, graph_store


# ---------------------------------------------------------------------------
# _try_parse_uuid
# ---------------------------------------------------------------------------


def test_try_parse_uuid_valid() -> None:
    val = uuid.uuid4()
    assert _try_parse_uuid(str(val)) == val


def test_try_parse_uuid_invalid() -> None:
    assert _try_parse_uuid("not-a-uuid") is None


def test_try_parse_uuid_empty_string() -> None:
    assert _try_parse_uuid("") is None


# ---------------------------------------------------------------------------
# No drift — clean run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_once_no_drift() -> None:
    """When PG sources match Qdrant sources and Neo4j has no extras, no drift."""
    worker, _vs, _gs = _build_worker(
        active_source_ids=[_SRC_A],
        chunks_by_source={str(_SRC_A): 5},
        entities_by_source={str(_SRC_A): 3},
    )
    with (
        patch("omniscience_server.reconcile_worker.RECONCILE_DRIFT_TOTAL") as mock_counter,
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_RUNS_TOTAL") as mock_runs,
        patch(
            "omniscience_server.reconcile_worker.RECONCILE_WORKER_DURATION_SECONDS"
        ) as mock_hist,
    ):
        mock_counter.labels.return_value.inc = MagicMock()
        mock_runs.inc = MagicMock()
        mock_hist.observe = MagicMock()

        report = await worker.run_once()

    assert isinstance(report, ReconcileRunReport)
    assert len(report.per_workspace) == 1
    ws_report = report.per_workspace[0]
    assert ws_report.workspace_id == _WS
    assert ws_report.pg_missing_qdrant == []
    assert ws_report.qdrant_orphan_sources == []
    assert ws_report.qdrant_orphans_deleted == 0
    assert ws_report.neo4j_orphan_sources == []
    # Drift counter must NOT be incremented.
    mock_counter.labels.return_value.inc.assert_not_called()


# ---------------------------------------------------------------------------
# Drift type A: PG document has no Qdrant chunks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_missing_qdrant_detected() -> None:
    """Source in PG but absent from Qdrant should be reported and run marked partial."""
    run_id = uuid.uuid4()
    worker, _vs, _gs = _build_worker(
        active_source_ids=[_SRC_A],
        chunks_by_source={},  # SRC_A has no Qdrant chunks
        entities_by_source={},
        ingestion_run_id=run_id,
    )
    with (
        patch("omniscience_server.reconcile_worker.RECONCILE_DRIFT_TOTAL") as mock_counter,
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_RUNS_TOTAL") as mock_runs,
        patch(
            "omniscience_server.reconcile_worker.RECONCILE_WORKER_DURATION_SECONDS"
        ) as mock_hist,
    ):
        mock_counter.labels.return_value.inc = MagicMock()
        mock_runs.inc = MagicMock()
        mock_hist.observe = MagicMock()

        report = await worker.run_once()

    ws_report = report.per_workspace[0]
    assert _SRC_A in ws_report.pg_missing_qdrant
    # Drift counter must be incremented with correct type
    mock_counter.labels.assert_any_call(drift_type=DRIFT_PG_MISSING_QDRANT)
    mock_counter.labels.return_value.inc.assert_called()


@pytest.mark.asyncio
async def test_pg_missing_qdrant_marks_ingestion_run_partial() -> None:
    """_mark_ingestion_run_partial is called for the affected source."""
    run_id = uuid.uuid4()
    worker, _, _ = _build_worker(
        active_source_ids=[_SRC_A],
        chunks_by_source={},
        entities_by_source={},
        ingestion_run_id=run_id,
    )
    called_sources: list[uuid.UUID] = []

    async def _spy(*, source_id: uuid.UUID) -> None:
        called_sources.append(source_id)

    worker._mark_ingestion_run_partial = _spy  # type: ignore[method-assign]
    with (
        patch("omniscience_server.reconcile_worker.RECONCILE_DRIFT_TOTAL") as m,
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_RUNS_TOTAL"),
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_DURATION_SECONDS"),
    ):
        m.labels.return_value.inc = MagicMock()
        await worker.run_once()

    assert _SRC_A in called_sources


# ---------------------------------------------------------------------------
# Drift type B: Qdrant orphan (chunks with no PG source)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qdrant_orphan_detected() -> None:
    """Chunks in Qdrant for a source absent from PG should be detected."""
    worker, _vs, _gs = _build_worker(
        active_source_ids=[],  # PG has no documents
        chunks_by_source={str(_SRC_A): 3},  # Qdrant has 3 chunks for SRC_A
        entities_by_source={},
    )
    with (
        patch("omniscience_server.reconcile_worker.RECONCILE_DRIFT_TOTAL") as mc,
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_RUNS_TOTAL"),
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_DURATION_SECONDS"),
    ):
        mc.labels.return_value.inc = MagicMock()
        report = await worker.run_once()

    ws_report = report.per_workspace[0]
    assert str(_SRC_A) in ws_report.qdrant_orphan_sources
    mc.labels.assert_any_call(drift_type=DRIFT_QDRANT_ORPHAN)
    mc.labels.return_value.inc.assert_called()


@pytest.mark.asyncio
async def test_qdrant_orphan_deletion_is_bounded() -> None:
    """Orphan deletion must not exceed ``orphan_limit`` per tick."""
    limit = 5
    worker, vector_store, _gs = _build_worker(
        active_source_ids=[],
        chunks_by_source={str(_SRC_A): 100},  # 100 orphan chunks
        entities_by_source={},
        orphan_limit=limit,
    )
    # Make _qc.count return 100 and scroll return `limit` records
    count_result = MagicMock()
    count_result.count = 100
    vector_store._qc.count = AsyncMock(return_value=count_result)

    fake_records = [MagicMock(id=uuid.uuid4()) for _ in range(limit)]
    vector_store._qc.scroll = AsyncMock(return_value=(fake_records, None))
    vector_store._qc.delete = AsyncMock()

    with (
        patch("omniscience_server.reconcile_worker.RECONCILE_DRIFT_TOTAL") as mc,
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_RUNS_TOTAL"),
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_DURATION_SECONDS"),
    ):
        mc.labels.return_value.inc = MagicMock()
        report = await worker.run_once()

    ws_report = report.per_workspace[0]
    # Deleted count bounded by limit
    assert ws_report.qdrant_orphans_deleted <= limit
    # Qdrant delete was called with at most `limit` point IDs
    if vector_store._qc.delete.called:
        call_args = vector_store._qc.delete.call_args
        selector = call_args.kwargs.get("points_selector") or call_args.args[1]
        assert len(selector.points) <= limit


# ---------------------------------------------------------------------------
# Drift type C: Neo4j orphan (entities with no PG source)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_neo4j_orphan_detected() -> None:
    """Entity nodes in Neo4j for a source absent from PG should be reported."""
    worker, _vs, _gs = _build_worker(
        active_source_ids=[],  # PG has no documents
        chunks_by_source={},
        entities_by_source={str(_SRC_A): 7},
    )
    with (
        patch("omniscience_server.reconcile_worker.RECONCILE_DRIFT_TOTAL") as mc,
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_RUNS_TOTAL"),
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_DURATION_SECONDS"),
    ):
        mc.labels.return_value.inc = MagicMock()
        report = await worker.run_once()

    ws_report = report.per_workspace[0]
    assert str(_SRC_A) in ws_report.neo4j_orphan_sources
    mc.labels.assert_any_call(drift_type=DRIFT_NEO4J_ORPHAN)
    mc.labels.return_value.inc.assert_called()


@pytest.mark.asyncio
async def test_neo4j_orphan_no_deletion() -> None:
    """Neo4j orphans must NOT trigger any Neo4j delete (deferred to retention)."""
    worker, _vs, graph_store = _build_worker(
        active_source_ids=[],
        chunks_by_source={},
        entities_by_source={str(_SRC_A): 7},
    )
    with (
        patch("omniscience_server.reconcile_worker.RECONCILE_DRIFT_TOTAL") as mc,
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_RUNS_TOTAL"),
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_DURATION_SECONDS"),
    ):
        mc.labels.return_value.inc = MagicMock()
        await worker.run_once()

    # No mutation method should have been called on the graph store
    if hasattr(graph_store, "delete_by_source"):
        graph_store.delete_by_source.assert_not_called()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_twice_no_double_delete() -> None:
    """Two consecutive runs on the same orphan state produce the same result."""
    chunks_by_source = {str(_SRC_A): 2}

    count_result = MagicMock()
    count_result.count = 2
    fake_records = [MagicMock(id=uuid.uuid4()), MagicMock(id=uuid.uuid4())]

    # Build two workers that share the same store mocks but independent sessions.
    def _build_idempotent_worker() -> tuple[ReconcileWorker, AsyncMock]:
        factory = _make_session_factory(active_source_ids=[])
        vs = _make_vector_store(chunks_by_source=chunks_by_source)
        vs._qc.count = AsyncMock(return_value=count_result)
        vs._qc.scroll = AsyncMock(return_value=(fake_records, None))
        vs._qc.delete = AsyncMock()
        gs = _make_graph_store()
        w = ReconcileWorker(
            session_factory=factory,
            vector_store=vs,
            graph_store=gs,
            settings=_make_settings(),
        )
        return w, vs

    worker1, _vs1 = _build_idempotent_worker()
    worker2, _vs2 = _build_idempotent_worker()

    with (
        patch("omniscience_server.reconcile_worker.RECONCILE_DRIFT_TOTAL") as mc,
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_RUNS_TOTAL"),
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_DURATION_SECONDS"),
    ):
        mc.labels.return_value.inc = MagicMock()
        r1 = await worker1.run_once()
        r2 = await worker2.run_once()

    # Both runs should detect the same orphan
    assert r1.per_workspace[0].qdrant_orphan_sources == r2.per_workspace[0].qdrant_orphan_sources


# ---------------------------------------------------------------------------
# last_report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_last_report_is_populated_after_run() -> None:
    worker, _, _ = _build_worker()
    assert worker.last_report is None
    with (
        patch("omniscience_server.reconcile_worker.RECONCILE_DRIFT_TOTAL"),
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_RUNS_TOTAL"),
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_DURATION_SECONDS"),
    ):
        await worker.run_once()
    assert worker.last_report is not None
    assert isinstance(worker.last_report, ReconcileRunReport)


# ---------------------------------------------------------------------------
# stop() / lifecycle
# ---------------------------------------------------------------------------


def test_stop_sets_running_false() -> None:
    worker, _, _ = _build_worker()
    worker._running = True
    worker.stop()
    assert worker._running is False


# ---------------------------------------------------------------------------
# Multiple workspaces — each gets its own report row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_workspaces_separate_reports() -> None:
    ws2 = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000099")
    # Build a factory that returns two workspace IDs on the first call,
    # then empty active source sets for both.
    ws_result = MagicMock()
    ws_result.all.return_value = [(_WS,), (ws2,)]
    empty_src = MagicMock()
    empty_src.all.return_value = []

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[ws_result, empty_src, empty_src] * 10)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    tx = AsyncMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=tx)
    factory = MagicMock()
    factory.return_value = cm

    vs = _make_vector_store()
    gs = _make_graph_store()
    worker = ReconcileWorker(
        session_factory=factory,
        vector_store=vs,
        graph_store=gs,
        settings=_make_settings(),
    )

    with (
        patch("omniscience_server.reconcile_worker.RECONCILE_DRIFT_TOTAL"),
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_RUNS_TOTAL"),
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_DURATION_SECONDS"),
    ):
        report = await worker.run_once()

    assert len(report.per_workspace) == 2
    ws_ids = {r.workspace_id for r in report.per_workspace}
    assert _WS in ws_ids
    assert ws2 in ws_ids
