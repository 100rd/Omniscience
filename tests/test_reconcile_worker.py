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
    reemit_tick_cap: int = 1000,
) -> MagicMock:
    s = MagicMock()
    s.reconcile_tick_seconds = tick_seconds
    s.reconcile_orphan_delete_limit = orphan_limit
    s.reemit_tick_cap = reemit_tick_cap
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

    # AP2: _check_entity_drift and _check_edge_drift each open their own
    # session and call session.execute(select(Entity/Edge)...) returning a
    # result with .scalars().all() → [].  Provide empty-scalars mocks for
    # these calls so existing run_once() tests don't break.
    def _make_empty_scalars() -> MagicMock:
        r = MagicMock()
        r.scalars.return_value.all.return_value = []
        return r

    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _make_rows(workspace_ids),  # _load_workspace_ids
            _make_rows(active_source_ids),  # _load_active_source_ids
            run_result,  # _mark_ingestion_run_partial SELECT
            update_result,  # _mark_ingestion_run_partial UPDATE
            _make_empty_scalars(),  # _check_entity_drift: select(Entity)
            _make_empty_scalars(),  # _check_edge_drift: select(Edge)
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
    # AP2 additions — return empty dicts so _check_entity_drift does not
    # see unexpected mock objects from auto-mock.
    store.get_entity_versions = AsyncMock(return_value={})
    store.get_entity_content_hashes = AsyncMock(return_value={})
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
    # AP2 additions — provide sane defaults so _check_entity_drift and
    # _check_edge_drift do not see unexpected mock objects from auto-mock.
    store.get_entity_versions = AsyncMock(return_value={})
    store.get_entity_content_hashes = AsyncMock(return_value={})
    store.get_edge_versions = AsyncMock(return_value={})
    store.end_date_source_orphans = AsyncMock(return_value=0)
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
async def test_neo4j_orphan_active_heal() -> None:
    """AP2: Neo4j orphans trigger active end-dating (not just detection).

    The reconcile worker must call ``end_date_source_orphans`` on the graph
    store for each orphaned source.  Hard deletion (``delete_by_source``) must
    NOT be called — end-dating is the sanctioned path (ADR-0009 / ADR-0008 §3).
    """
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

    # Active heal must have been called (end-dating, not deletion).
    graph_store.end_date_source_orphans.assert_called_once()
    call_kwargs = graph_store.end_date_source_orphans.call_args.kwargs
    assert call_kwargs["workspace_id"] == _WS
    assert call_kwargs["source_id"] == str(_SRC_A)

    # Hard deletion must NOT be called.
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


@pytest.mark.asyncio
async def test_check_entity_drift_creates_outbox_events() -> None:
    from omniscience_core.db.models import Entity, OutboxEvent
    from omniscience_server.reconcile_worker import ReconcileWorker

    ws_id = uuid.uuid4()
    src_id = uuid.uuid4()
    ent_id_1 = uuid.uuid4()
    ent_id_2 = uuid.uuid4()

    # ent_1 is outdated in Qdrant (PG version > Qdrant version)
    # ent_2 is missing in Neo4j (Neo4j version = 0)
    ent_1 = MagicMock(spec=Entity)
    ent_1.id = ent_id_1
    ent_1.source_id = src_id
    ent_1.entity_type = "service"
    ent_1.name = "svc.auth"
    ent_1.display_name = "Auth Service"
    ent_1.entity_metadata = {}
    ent_1.version = 5
    ent_1.content_hash = None

    ent_2 = MagicMock(spec=Entity)
    ent_2.id = ent_id_2
    ent_2.source_id = src_id
    ent_2.entity_type = "service"
    ent_2.name = "svc.db"
    ent_2.display_name = "DB Service"
    ent_2.entity_metadata = {}
    ent_2.version = 2
    ent_2.content_hash = None

    # Mock DB
    result_mock = MagicMock()
    result_mock.scalars().all.return_value = [ent_1, ent_2]

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result_mock)

    tx = AsyncMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=tx)

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=cm)

    # Mock stores
    vs = _make_vector_store()
    vs.get_entity_versions = AsyncMock(return_value={ent_id_1: 4, ent_id_2: 2})
    vs.get_entity_content_hashes = AsyncMock(return_value={})

    gs = _make_graph_store()
    gs.get_entity_versions = AsyncMock(return_value={ent_id_1: 5, ent_id_2: 0})
    gs.get_entity_content_hashes = AsyncMock(return_value={})

    worker = ReconcileWorker(
        session_factory=factory,
        vector_store=vs,
        graph_store=gs,
        settings=_make_settings(),
    )

    await worker._check_entity_drift(workspace_id=ws_id)

    # Both entities should be backfilled (ent_1 due to qdrant, ent_2 due to neo4j)
    assert session.add.call_count == 2

    # Verify the payloads
    args1, _ = session.add.call_args_list[0]
    event1 = args1[0]
    assert isinstance(event1, OutboxEvent)
    assert event1.event_type == "entity.upsert"
    assert event1.payload["id"] == str(ent_id_1)
    assert event1.payload["is_backfill"] is True
    assert event1.payload["version"] == 5

    args2, _ = session.add.call_args_list[1]
    event2 = args2[0]
    assert isinstance(event2, OutboxEvent)
    assert event2.event_type == "entity.upsert"
    assert event2.payload["id"] == str(ent_id_2)
    assert event2.payload["is_backfill"] is True
    assert event2.payload["version"] == 2


# ---------------------------------------------------------------------------
# AP2 — content-hash drift detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_entity_drift_detects_hash_mismatch() -> None:
    """Same version but corrupted content_hash in Neo4j triggers backfill."""
    from omniscience_core.db.models import Entity, OutboxEvent
    from omniscience_server.reconcile_worker import ReconcileWorker

    ws_id = uuid.uuid4()
    src_id = uuid.uuid4()
    ent_id = uuid.uuid4()

    correct_hash = "aabbcc" * 10 + "aabbccdd"  # 64-char hex
    wrong_hash = "deadbeef" * 8  # same length, different value

    ent = MagicMock(spec=Entity)
    ent.id = ent_id
    ent.source_id = src_id
    ent.entity_type = "service"
    ent.name = "svc.alpha"
    ent.display_name = "Alpha"
    ent.entity_metadata = {}
    ent.version = 3
    ent.content_hash = correct_hash

    result_mock = MagicMock()
    result_mock.scalars().all.return_value = [ent]

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result_mock)
    tx = AsyncMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=tx)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)

    vs = _make_vector_store()
    # Version matches — no version drift
    vs.get_entity_versions = AsyncMock(return_value={ent_id: 3})
    # Qdrant has correct hash
    vs.get_entity_content_hashes = AsyncMock(return_value={ent_id: correct_hash})

    gs = _make_graph_store()
    # Version matches
    gs.get_entity_versions = AsyncMock(return_value={ent_id: 3})
    # Neo4j has WRONG hash — simulate corruption
    gs.get_entity_content_hashes = AsyncMock(return_value={ent_id: wrong_hash})

    worker = ReconcileWorker(
        session_factory=factory,
        vector_store=vs,
        graph_store=gs,
        settings=_make_settings(),
    )

    await worker._check_entity_drift(workspace_id=ws_id)

    # Should emit a backfill event for the hash-drifted entity
    assert session.add.call_count == 1
    event = session.add.call_args_list[0][0][0]
    assert isinstance(event, OutboxEvent)
    assert event.payload["id"] == str(ent_id)
    assert event.payload["is_backfill"] is True
    assert event.payload["content_hash"] == correct_hash


@pytest.mark.asyncio
async def test_check_entity_drift_skips_hash_when_pg_hash_is_none() -> None:
    """When Entity.content_hash is None (pre-AP2 row), hash comparison is skipped."""
    from omniscience_core.db.models import Entity
    from omniscience_server.reconcile_worker import ReconcileWorker

    ws_id = uuid.uuid4()
    src_id = uuid.uuid4()
    ent_id = uuid.uuid4()

    ent = MagicMock(spec=Entity)
    ent.id = ent_id
    ent.source_id = src_id
    ent.entity_type = "service"
    ent.name = "svc.legacy"
    ent.display_name = "Legacy"
    ent.entity_metadata = {}
    ent.version = 1
    ent.content_hash = None  # pre-AP2: no hash

    result_mock = MagicMock()
    result_mock.scalars().all.return_value = [ent]

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result_mock)
    tx = AsyncMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=tx)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)

    vs = _make_vector_store()
    vs.get_entity_versions = AsyncMock(return_value={ent_id: 1})
    vs.get_entity_content_hashes = AsyncMock(return_value={ent_id: "some_hash"})  # different!

    gs = _make_graph_store()
    gs.get_entity_versions = AsyncMock(return_value={ent_id: 1})
    gs.get_entity_content_hashes = AsyncMock(return_value={ent_id: "other_hash"})  # different!

    worker = ReconcileWorker(
        session_factory=factory,
        vector_store=vs,
        graph_store=gs,
        settings=_make_settings(),
    )

    await worker._check_entity_drift(workspace_id=ws_id)

    # No backfill because pg_hash is None — hash comparison is skipped
    assert session.add.call_count == 0


@pytest.mark.asyncio
async def test_check_pg_missing_qdrant_reemits_entities() -> None:
    """AP2 closed-loop: _reemit_entities_for_missing_sources emits entity.upsert OutboxEvents."""
    from omniscience_core.db.models import Entity, OutboxEvent
    from omniscience_server.reconcile_worker import ReconcileWorker

    ws_id = uuid.uuid4()
    src_missing = uuid.uuid4()
    ent_id = uuid.uuid4()

    test_hash = "cc" * 32

    # Entity belonging to the missing source
    ent = MagicMock(spec=Entity)
    ent.id = ent_id
    ent.source_id = src_missing
    ent.entity_type = "service"
    ent.name = "svc.missing"
    ent.display_name = "Missing"
    ent.entity_metadata = {"foo": "bar"}
    ent.version = 2
    ent.content_hash = test_hash

    # Session 1 (SELECT entities): returns [ent]
    entity_result = MagicMock()
    entity_result.scalars().all.return_value = [ent]

    session_read = AsyncMock()
    session_read.execute = AsyncMock(return_value=entity_result)
    cm_read = AsyncMock()
    cm_read.__aenter__ = AsyncMock(return_value=session_read)
    cm_read.__aexit__ = AsyncMock(return_value=False)

    # Session 2 (INSERT OutboxEvents)
    session_write = AsyncMock()
    session_write.add = MagicMock()
    tx_write = AsyncMock()
    tx_write.__aenter__ = AsyncMock(return_value=None)
    tx_write.__aexit__ = AsyncMock(return_value=False)
    session_write.begin = MagicMock(return_value=tx_write)
    cm_write = AsyncMock()
    cm_write.__aenter__ = AsyncMock(return_value=session_write)
    cm_write.__aexit__ = AsyncMock(return_value=False)

    # factory alternates: first call → read session, second call → write session
    factory = MagicMock(side_effect=[cm_read, cm_write])

    vs = _make_vector_store()
    gs = _make_graph_store()

    worker = ReconcileWorker(
        session_factory=factory,
        vector_store=vs,
        graph_store=gs,
        settings=_make_settings(),
    )

    await worker._reemit_entities_for_missing_sources(
        workspace_id=ws_id,
        missing_source_ids=[src_missing],
    )

    # Should have added an OutboxEvent for the missing entity
    assert session_write.add.call_count == 1
    event = session_write.add.call_args_list[0][0][0]
    assert isinstance(event, OutboxEvent)
    assert event.event_type == "entity.upsert"
    assert event.payload["id"] == str(ent_id)
    assert event.payload["is_backfill"] is True
    assert event.payload["content_hash"] == test_hash


# ---------------------------------------------------------------------------
# AP2 — entity_content_hash stability (unit test for the hash util)
# ---------------------------------------------------------------------------


def test_entity_content_hash_is_stable() -> None:
    """entity_content_hash returns the same value when called twice with same inputs."""
    from omniscience_core.audit.fingerprint import entity_content_hash

    h1 = entity_content_hash(
        entity_type="service",
        name="auth-service",
        display_name="Auth",
        metadata={"env": "prod", "version": 42},
    )
    h2 = entity_content_hash(
        entity_type="service",
        name="auth-service",
        display_name="Auth",
        metadata={"env": "prod", "version": 42},
    )
    assert h1 == h2
    assert len(h1) == 64  # BLAKE2b-256 = 32 bytes = 64 hex chars


def test_entity_content_hash_changes_on_field_change() -> None:
    """entity_content_hash changes when any content field changes."""
    from omniscience_core.audit.fingerprint import entity_content_hash

    base = entity_content_hash(
        entity_type="service",
        name="auth-service",
        display_name="Auth",
        metadata={"env": "prod"},
    )

    # Changing entity_type
    h_type = entity_content_hash(
        entity_type="function",  # different
        name="auth-service",
        display_name="Auth",
        metadata={"env": "prod"},
    )
    assert h_type != base

    # Changing name
    h_name = entity_content_hash(
        entity_type="service",
        name="auth-service-v2",  # different
        display_name="Auth",
        metadata={"env": "prod"},
    )
    assert h_name != base

    # Changing metadata
    h_meta = entity_content_hash(
        entity_type="service",
        name="auth-service",
        display_name="Auth",
        metadata={"env": "staging"},  # different
    )
    assert h_meta != base


def test_entity_content_hash_dict_order_independent() -> None:
    """entity_content_hash is independent of metadata dict insertion order."""
    from omniscience_core.audit.fingerprint import entity_content_hash

    h1 = entity_content_hash(
        entity_type="service",
        name="svc",
        display_name="Svc",
        metadata={"a": 1, "b": 2, "c": 3},
    )
    h2 = entity_content_hash(
        entity_type="service",
        name="svc",
        display_name="Svc",
        metadata={"c": 3, "a": 1, "b": 2},  # different order
    )
    assert h1 == h2


def test_entity_content_hash_round_trip() -> None:
    """Round-trip: hash computed at ingestion equals hash recomputed from event payload."""
    from omniscience_core.audit.fingerprint import entity_content_hash

    # Simulate ingestion path (postgres write)
    ingestion_hash = entity_content_hash(
        entity_type="terraform_resource",
        name="aws_s3_bucket.my_bucket",
        display_name="my_bucket",
        metadata={"region": "us-east-1", "versioned": True},
    )

    # Simulate event payload forwarded via outbox (same fields)
    event_hash = entity_content_hash(
        entity_type="terraform_resource",
        name="aws_s3_bucket.my_bucket",
        display_name="my_bucket",
        metadata={"region": "us-east-1", "versioned": True},
    )

    # Both must be identical — critical AP2 determinism guarantee
    assert ingestion_hash == event_hash


# ---------------------------------------------------------------------------
# AP2 — bounded re-emit + cursor + edge drift + Neo4j tombstone heal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reemit_tick_cap_exported() -> None:
    """AP2: REEMIT_TICK_CAP is exported from reconcile_worker as a positive int."""
    from omniscience_server.reconcile_worker import REEMIT_TICK_CAP

    assert isinstance(REEMIT_TICK_CAP, int)
    assert REEMIT_TICK_CAP > 0


@pytest.mark.asyncio
async def test_entity_drift_bounded_by_cap() -> None:
    """AP2: _check_entity_drift emits at most _reemit_cap entities per tick."""
    from omniscience_core.db.models import Entity

    ws_id = uuid.uuid4()
    cap = 2

    # 4 drifted entities (PG version=5, stores have version=0).
    entities = []
    for _ in range(4):
        ent = MagicMock(spec=Entity)
        ent.id = uuid.uuid4()
        ent.source_id = uuid.uuid4()
        ent.entity_type = "service"
        ent.name = f"svc.{ent.id}"
        ent.display_name = str(ent.id)
        ent.entity_metadata = {}
        ent.version = 5
        ent.content_hash = None
        entities.append(ent)

    read_result = MagicMock()
    read_result.scalars.return_value.all.return_value = entities
    read_session = AsyncMock()
    read_session.execute = AsyncMock(return_value=read_result)
    read_cm = AsyncMock()
    read_cm.__aenter__ = AsyncMock(return_value=read_session)
    read_cm.__aexit__ = AsyncMock(return_value=False)

    write_session = AsyncMock()
    write_session.add = MagicMock()
    write_tx = AsyncMock()
    write_tx.__aenter__ = AsyncMock(return_value=None)
    write_tx.__aexit__ = AsyncMock(return_value=False)
    write_session.begin = MagicMock(return_value=write_tx)
    write_cm = AsyncMock()
    write_cm.__aenter__ = AsyncMock(return_value=write_session)
    write_cm.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(side_effect=[read_cm, write_cm] * 10)

    vs = _make_vector_store()
    vs.get_entity_versions = AsyncMock(return_value={})  # all drifted
    vs.get_entity_content_hashes = AsyncMock(return_value={})

    gs = _make_graph_store()
    gs.get_entity_versions = AsyncMock(return_value={})  # all drifted
    gs.get_entity_content_hashes = AsyncMock(return_value={})

    settings = _make_settings()
    settings.reemit_tick_cap = cap

    worker = ReconcileWorker(
        session_factory=factory,
        vector_store=vs,
        graph_store=gs,
        settings=settings,
    )
    worker._reemit_cursor = 0

    await worker._check_entity_drift(workspace_id=ws_id)

    # Only cap events emitted.
    assert write_session.add.call_count == cap
    assert worker._reemit_cursor == cap

    # Second call: cap exhausted → zero additional events.
    worker._session_factory = MagicMock(side_effect=[read_cm, write_cm] * 10)
    write_session.add.reset_mock()
    await worker._check_entity_drift(workspace_id=ws_id)
    write_session.add.assert_not_called()


@pytest.mark.asyncio
async def test_edge_drift_creates_edge_upsert_outbox_event() -> None:
    """AP2: _check_edge_drift emits edge.upsert OutboxEvents for drifted edges."""
    from omniscience_core.db.models import Edge, OutboxEvent

    ws_id = uuid.uuid4()
    src_id = uuid.uuid4()
    tgt_id = uuid.uuid4()

    edge = MagicMock(spec=Edge)
    edge.id = uuid.uuid4()
    edge.source_entity_id = src_id
    edge.target_entity_id = tgt_id
    edge.edge_type = "calls"
    edge.edge_metadata = {"key": "val"}
    edge.version = 3

    edge_result = MagicMock()
    edge_result.scalars.return_value.all.return_value = [edge]
    edge_session = AsyncMock()
    edge_session.execute = AsyncMock(return_value=edge_result)
    edge_cm = AsyncMock()
    edge_cm.__aenter__ = AsyncMock(return_value=edge_session)
    edge_cm.__aexit__ = AsyncMock(return_value=False)

    write_session = AsyncMock()
    write_session.add = MagicMock()
    write_tx = AsyncMock()
    write_tx.__aenter__ = AsyncMock(return_value=None)
    write_tx.__aexit__ = AsyncMock(return_value=False)
    write_session.begin = MagicMock(return_value=write_tx)
    write_cm = AsyncMock()
    write_cm.__aenter__ = AsyncMock(return_value=write_session)
    write_cm.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(side_effect=[edge_cm, write_cm] * 10)

    gs = _make_graph_store()
    # Neo4j has no edge version → 0 < PG version 3 → drift.
    gs.get_edge_versions = AsyncMock(return_value={})

    vs = _make_vector_store()

    settings = _make_settings()

    worker = ReconcileWorker(
        session_factory=factory,
        vector_store=vs,
        graph_store=gs,
        settings=settings,
    )
    worker._reemit_cursor = 0

    with patch("omniscience_server.reconcile_worker.RECONCILE_DRIFT_TOTAL") as mc:
        mc.labels.return_value.inc = MagicMock()
        await worker._check_edge_drift(workspace_id=ws_id)

    assert write_session.add.call_count == 1
    event = write_session.add.call_args_list[0][0][0]
    assert isinstance(event, OutboxEvent)
    assert event.event_type == "edge.upsert"
    assert event.payload["source_entity_id"] == str(src_id)
    assert event.payload["target_entity_id"] == str(tgt_id)
    assert event.payload["edge_type"] == "calls"
    assert event.payload["version"] == 3
    assert event.payload["is_backfill"] is True


@pytest.mark.asyncio
async def test_entities_emitted_before_edges_causal_order() -> None:
    """AP2: entity.upsert events are inserted before edge.upsert events in the DB.

    The call order in _reconcile_workspace guarantees that _check_entity_drift
    (entities) is called before _check_edge_drift (edges), enforcing causal order
    so the outbox consumer sees entity definitions before edge references.
    """
    # We test that _reconcile_workspace calls entity drift before edge drift by
    # patching both methods and tracking call order.
    worker, _vs, _gs = _build_worker()
    call_order: list[str] = []

    async def _mock_entity_drift(*, workspace_id: uuid.UUID) -> None:
        call_order.append("entity")

    async def _mock_edge_drift(*, workspace_id: uuid.UUID) -> None:
        call_order.append("edge")

    worker._check_entity_drift = _mock_entity_drift  # type: ignore[method-assign]
    worker._check_edge_drift = _mock_edge_drift  # type: ignore[method-assign]

    # Also stub out the other checks to avoid side effects.
    async def _noop_pg(*a: Any, **kw: Any) -> list[uuid.UUID]:
        return []

    async def _noop_qdrant(*a: Any, **kw: Any) -> tuple[list[str], int]:
        return [], 0

    async def _noop_neo4j(*a: Any, **kw: Any) -> list[str]:
        return []

    worker._check_pg_missing_qdrant = _noop_pg  # type: ignore[method-assign]
    worker._check_qdrant_orphans = _noop_qdrant  # type: ignore[method-assign]
    worker._check_neo4j_orphans = _noop_neo4j  # type: ignore[method-assign]

    with (
        patch("omniscience_server.reconcile_worker.RECONCILE_DRIFT_TOTAL"),
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_RUNS_TOTAL"),
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_DURATION_SECONDS"),
    ):
        await worker.run_once()

    assert call_order == ["entity", "edge"], f"Expected entities before edges; got: {call_order}"


@pytest.mark.asyncio
async def test_reemit_cursor_resets_each_run() -> None:
    """AP2: cursor resets to 0 at the start of each run_once() call."""
    worker, _vs, _gs = _build_worker()
    worker._reemit_cursor = 999  # simulate a previous run

    with (
        patch("omniscience_server.reconcile_worker.RECONCILE_DRIFT_TOTAL"),
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_RUNS_TOTAL"),
        patch("omniscience_server.reconcile_worker.RECONCILE_WORKER_DURATION_SECONDS"),
    ):
        await worker.run_once()

    # After run_once, cursor should have been reset to 0 at start, then advanced by 0
    # (no drift in this run) → final value is 0.
    assert worker._reemit_cursor == 0


# ---------------------------------------------------------------------------
# Round-2 scope-gate: worker-path authz — cross-workspace edge target leak
#
# reconcile_worker.py's module docstring and _reconcile_workspace docstring
# both assert an "ACL invariant: every store call passes workspace_id;
# cross-workspace mixing is structurally impossible because the outer loop
# iterates workspaces and every per-workspace method pins the id."  There is
# no DB constraint enforcing that an Edge's source_entity and target_entity
# live in the same workspace (Edge.target_entity_id is a bare FK to
# entities.id — see packages/core/src/omniscience_core/db/models.py).  The
# PG "edge SoT" query in _check_edge_drift only joins Source via the edge's
# SOURCE entity; it never re-validates the TARGET entity's tenant.  If a
# target entity ever ends up outside workspace_id (corruption, a bad merge,
# a future migration bug — exactly the class of anomaly this worker exists
# to catch), the query still treats the edge as this workspace's ground
# truth and emits an edge.upsert OutboxEvent for it under this tenant,
# leaking a reference to another tenant's entity into this workspace's
# outbox stream.  This is a real gap in a worker path explicitly named by
# the round-2 review scope gate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edge_drift_query_pins_workspace_on_target_entity_too() -> None:
    """The PG edge-SoT query must validate tenancy on BOTH edge endpoints.

    White-box regression (matches this file's existing mock-at-session.execute
    style): capture the SELECT statement _check_edge_drift issues and assert
    it consults the entities/sources tables more than once in its FROM/JOIN
    clause — i.e. it re-validates the target entity's workspace, not only
    the source entity's.  Today it only joins once, so this fails.
    """
    import re

    ws_id = uuid.uuid4()
    captured: list[Any] = []

    async def _capture_execute(stmt: Any, *_a: Any, **_kw: Any) -> MagicMock:
        captured.append(stmt)
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        return result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_capture_execute)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)

    gs = _make_graph_store()
    vs = _make_vector_store()
    settings = _make_settings()
    worker = ReconcileWorker(
        session_factory=factory,
        vector_store=vs,
        graph_store=gs,
        settings=settings,
    )

    await worker._check_edge_drift(workspace_id=ws_id)

    assert captured, "expected _check_edge_drift to issue at least one SELECT"
    edge_stmt = captured[0]
    compiled_sql = str(edge_stmt.compile(compile_kwargs={"literal_binds": False}))
    from_clause = compiled_sql.split("WHERE")[0]
    join_hits = re.findall(r"\bjoin\s+entities\b", from_clause, re.IGNORECASE)
    assert len(join_hits) >= 2, (
        "the edge-drift PG query only joins Entity/Source via the edge's "
        "SOURCE entity (one 'JOIN entities'); it must also join/validate "
        "the TARGET entity's tenant before treating the edge as this "
        "workspace's ground truth and re-emitting it into the outbox.\n"
        f"Compiled SQL:\n{compiled_sql}"
    )
