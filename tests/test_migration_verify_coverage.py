"""Coverage tests for omniscience_index.migration.verify (issue #108 §Verification).

Raises line coverage of verify.py from ~45% to >=80% by directly exercising:

* CountMismatch.is_error — matching and mismatching counts
* RoundTripFailure / IsolationBreach — dataclass construction
* VerificationReport.passed — all four failure conditions
* _diff_entity_views — all five branches (both-none, pg-none, n4-none,
  field-drift on name/kind/source, no-diff)
* compute_top_k_overlap — full overlap, partial, empty reference,
  candidate superset
* VerifyRunner.__init__ — deterministic and non-deterministic RNG
* VerifyRunner.run — full happy path with mocked session factory,
  graph_store, vector_store; isolation skipped (single workspace);
  isolation triggered (two workspaces, no breach and one breach)
* VerifyRunner._round_trip_entities — pgvector_graph supplied vs None
* VerifyRunner._check_counts — count match and mismatch paths
* _neo4j_count_entities_for_source — mocked driver
* _neo4j_count_edges_for_source — mocked driver
* _qdrant_count_chunks_for_source — mocked client
* _scroll_workspace_payloads — single-page and multi-page pagination
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from omniscience_core.storage.graph import EntityNodeView
from omniscience_index.migration.config import (
    TOP_K_OVERLAP_THRESHOLD,
    MigrationConfig,
)
from omniscience_index.migration.verify import (
    CountMismatch,
    IsolationBreach,
    RoundTripFailure,
    VerificationReport,
    VerifyRunner,
    _diff_entity_views,
    _neo4j_count_edges_for_source,
    _neo4j_count_entities_for_source,
    _qdrant_count_chunks_for_source,
    _scroll_workspace_payloads,
    compute_top_k_overlap,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WS_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_WS_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
_WS_C = uuid.UUID("cccccccc-0000-0000-0000-000000000003")  # third workspace for breach test
_SRC_1 = uuid.UUID("11111111-0000-0000-0000-000000000001")

_DEFAULT_PROG = Path(".migration_progress.json")


def _make_config(
    *,
    workspace_id: uuid.UUID | None = None,
    source_id: uuid.UUID | None = None,
    default_workspace_id: uuid.UUID | None = _WS_A,
) -> MigrationConfig:
    return MigrationConfig(
        workspace_id=workspace_id,
        source_id=source_id,
        default_workspace_id=default_workspace_id,
        batch_size=500,
        dry_run=False,
        resume=False,
        verify_only=True,
        progress_file=_DEFAULT_PROG,
    )


def _make_entity_view(
    name: str = "MyService",
    kind: str = "service",
    source: str = "src",
) -> EntityNodeView:
    return EntityNodeView(
        id=uuid.uuid4(),
        name=name,
        kind=kind,
        source=source,
        chunk_text=None,
    )


def _make_source_mock(
    source_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    name: str = "test-source",
) -> MagicMock:
    src = MagicMock()
    src.id = source_id or uuid.uuid4()
    src.tenant_id = tenant_id
    src.name = name
    src.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    return src


def _make_graph_store() -> MagicMock:
    gs = MagicMock()
    gs._driver = MagicMock()
    gs._config = MagicMock()
    gs._config.database = "neo4j"
    gs.get_entity = AsyncMock(return_value=None)
    return gs


def _make_vector_store() -> MagicMock:
    vs = MagicMock()
    vs._qc = AsyncMock()
    vs.collection_name = "test-collection"
    return vs


@asynccontextmanager
async def _make_session_factory(
    sources: list[MagicMock] | None = None,
):
    """Return an async context manager that yields a mock AsyncSession."""
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = sources or []
    session.execute = AsyncMock(return_value=result)
    yield session


def _build_session_factory(sources: list[MagicMock] | None = None):
    """Return a callable that returns the async context manager."""

    class _Factory:
        def __call__(self):
            return _make_session_factory(sources)

    return _Factory()


# ===========================================================================
# CountMismatch
# ===========================================================================


class TestCountMismatch:
    def test_equal_counts_not_error(self) -> None:
        cm = CountMismatch(
            source_id=_SRC_1,
            source_name="src",
            kind="entities",
            pgvector=42,
            hybrid=42,
        )
        assert cm.is_error is False

    def test_different_counts_is_error(self) -> None:
        cm = CountMismatch(
            source_id=_SRC_1,
            source_name="src",
            kind="edges",
            pgvector=10,
            hybrid=8,
        )
        assert cm.is_error is True

    def test_zero_vs_nonzero_is_error(self) -> None:
        cm = CountMismatch(
            source_id=_SRC_1,
            source_name="src",
            kind="chunks",
            pgvector=0,
            hybrid=5,
        )
        assert cm.is_error is True


# ===========================================================================
# RoundTripFailure + IsolationBreach
# ===========================================================================


class TestReportDataclasses:
    def test_round_trip_failure_stores_fields(self) -> None:
        eid = uuid.uuid4()
        rtf = RoundTripFailure(
            source_id=_SRC_1,
            entity_id=eid,
            entity_name="MyEntity",
            field_diffs=["kind", "source"],
        )
        assert rtf.entity_id == eid
        assert rtf.field_diffs == ["kind", "source"]

    def test_isolation_breach_stores_fields(self) -> None:
        probe = uuid.uuid4()
        leaked = uuid.uuid4()
        chunk = uuid.uuid4()
        ib = IsolationBreach(
            probe_workspace_id=probe,
            leaked_from_workspace_id=leaked,
            chunk_id=chunk,
        )
        assert ib.probe_workspace_id == probe
        assert ib.leaked_from_workspace_id == leaked


# ===========================================================================
# VerificationReport.passed
# ===========================================================================


class TestVerificationReport:
    def test_empty_report_passes(self) -> None:
        assert VerificationReport().passed is True

    def test_count_mismatch_fails(self) -> None:
        rep = VerificationReport()
        rep.count_mismatches.append(
            CountMismatch(
                source_id=uuid.uuid4(),
                source_name="s",
                kind="entities",
                pgvector=1,
                hybrid=2,
            )
        )
        assert rep.passed is False

    def test_round_trip_failure_fails(self) -> None:
        rep = VerificationReport()
        rep.round_trip_failures.append(
            RoundTripFailure(
                source_id=uuid.uuid4(),
                entity_id=uuid.uuid4(),
                entity_name="n",
                field_diffs=["name"],
            )
        )
        assert rep.passed is False

    def test_isolation_breach_fails(self) -> None:
        rep = VerificationReport()
        rep.isolation_breaches.append(
            IsolationBreach(
                probe_workspace_id=uuid.uuid4(),
                leaked_from_workspace_id=uuid.uuid4(),
                chunk_id=uuid.uuid4(),
            )
        )
        assert rep.passed is False

    def test_retrieval_overlap_false_fails(self) -> None:
        rep = VerificationReport()
        rep.retrieval_overlap_pass = False
        assert rep.passed is False

    def test_all_failures_combined(self) -> None:
        rep = VerificationReport()
        rep.count_mismatches.append(
            CountMismatch(
                source_id=uuid.uuid4(),
                source_name="s",
                kind="entities",
                pgvector=1,
                hybrid=0,
            )
        )
        rep.retrieval_overlap_pass = False
        assert rep.passed is False


# ===========================================================================
# _diff_entity_views — all five branches
# ===========================================================================


class TestDiffEntityViews:
    def test_both_none_is_missing_on_both(self) -> None:
        assert _diff_entity_views(None, None) == ["missing_on_both_sides"]

    def test_pg_none_is_missing_on_pgvector(self) -> None:
        v = _make_entity_view()
        assert _diff_entity_views(None, v) == ["missing_on_pgvector"]

    def test_n4_none_is_missing_on_neo4j(self) -> None:
        v = _make_entity_view()
        assert _diff_entity_views(v, None) == ["missing_on_neo4j"]

    def test_identical_views_no_diff(self) -> None:
        v = _make_entity_view(name="A", kind="service", source="s")
        assert _diff_entity_views(v, v) == []

    def test_name_drift(self) -> None:
        a = _make_entity_view(name="A", kind="service", source="s")
        b = _make_entity_view(name="B", kind="service", source="s")
        assert _diff_entity_views(a, b) == ["name"]

    def test_kind_drift(self) -> None:
        a = _make_entity_view(name="A", kind="service", source="s")
        b = _make_entity_view(name="A", kind="function", source="s")
        assert _diff_entity_views(a, b) == ["kind"]

    def test_source_drift(self) -> None:
        a = _make_entity_view(name="A", kind="service", source="s1")
        b = _make_entity_view(name="A", kind="service", source="s2")
        assert _diff_entity_views(a, b) == ["source"]

    def test_multiple_field_drift(self) -> None:
        a = _make_entity_view(name="A", kind="service", source="s1")
        b = _make_entity_view(name="B", kind="function", source="s1")
        diffs = _diff_entity_views(a, b)
        assert "name" in diffs
        assert "kind" in diffs

    def test_all_three_fields_differ(self) -> None:
        a = _make_entity_view(name="X", kind="class", source="src1")
        b = _make_entity_view(name="Y", kind="function", source="src2")
        diffs = _diff_entity_views(a, b)
        assert set(diffs) == {"name", "kind", "source"}


# ===========================================================================
# compute_top_k_overlap
# ===========================================================================


class TestComputeTopKOverlap:
    def test_empty_reference_returns_1(self) -> None:
        assert compute_top_k_overlap(reference_hits=[], candidate_hits=[]) == 1.0

    def test_empty_reference_with_candidates_returns_1(self) -> None:
        ids = [uuid.uuid4() for _ in range(3)]
        assert compute_top_k_overlap(reference_hits=[], candidate_hits=ids) == 1.0

    def test_full_overlap(self) -> None:
        ids = [uuid.uuid4() for _ in range(5)]
        assert compute_top_k_overlap(reference_hits=ids, candidate_hits=ids) == 1.0

    def test_half_overlap(self) -> None:
        ids = [uuid.uuid4() for _ in range(4)]
        result = compute_top_k_overlap(reference_hits=ids, candidate_hits=ids[:2])
        assert result == pytest.approx(0.5)

    def test_zero_overlap(self) -> None:
        ref = [uuid.uuid4() for _ in range(3)]
        cand = [uuid.uuid4() for _ in range(3)]
        assert compute_top_k_overlap(reference_hits=ref, candidate_hits=cand) == 0.0

    def test_candidate_superset_of_reference(self) -> None:
        ref = [uuid.uuid4() for _ in range(2)]
        cand = [*ref, uuid.uuid4(), uuid.uuid4()]
        assert compute_top_k_overlap(reference_hits=ref, candidate_hits=cand) == 1.0

    def test_threshold_matches_spec(self) -> None:
        assert pytest.approx(TOP_K_OVERLAP_THRESHOLD) == 0.8

    def test_duplicates_in_reference_treated_as_set(self) -> None:
        uid = uuid.uuid4()
        ref = [uid, uid]  # duplicated — treated as a set of size 1
        cand = [uid]
        result = compute_top_k_overlap(reference_hits=ref, candidate_hits=cand)
        assert result == 1.0


# ===========================================================================
# VerifyRunner.__init__
# ===========================================================================


class TestVerifyRunnerInit:
    def test_deterministic_rng(self) -> None:
        gs = _make_graph_store()
        vs = _make_vector_store()
        cfg = _make_config()
        factory = _build_session_factory()
        runner = VerifyRunner(
            config=cfg,
            session_factory=factory,
            graph_store=gs,
            vector_store=vs,
            rng_seed=42,
        )
        runner2 = VerifyRunner(
            config=cfg,
            session_factory=factory,
            graph_store=gs,
            vector_store=vs,
            rng_seed=42,
        )
        population = list(range(100))
        assert runner._rng.sample(population, 5) == runner2._rng.sample(population, 5)

    def test_non_deterministic_rng_when_no_seed(self) -> None:
        gs = _make_graph_store()
        vs = _make_vector_store()
        cfg = _make_config()
        factory = _build_session_factory()
        runner = VerifyRunner(
            config=cfg,
            session_factory=factory,
            graph_store=gs,
            vector_store=vs,
        )
        assert runner._rng is not None

    def test_pgvector_graph_stored(self) -> None:
        gs = _make_graph_store()
        vs = _make_vector_store()
        cfg = _make_config()
        factory = _build_session_factory()
        pg_mock = MagicMock()
        runner = VerifyRunner(
            config=cfg,
            session_factory=factory,
            graph_store=gs,
            vector_store=vs,
            pgvector_graph_get_entity=pg_mock,
        )
        assert runner._pgvector_graph is pg_mock


# ===========================================================================
# VerifyRunner.run — mocked full path
# ===========================================================================


class TestVerifyRunnerRun:
    @pytest.mark.asyncio
    async def test_empty_sources_returns_passing_report(self) -> None:
        gs = _make_graph_store()
        vs = _make_vector_store()
        cfg = _make_config()
        factory = _build_session_factory(sources=[])

        runner = VerifyRunner(
            config=cfg,
            session_factory=factory,
            graph_store=gs,
            vector_store=vs,
        )

        with patch(
            "omniscience_index.migration.verify.resolve_source_workspace",
            return_value=_WS_A,
        ):
            report = await runner.run()

        assert report.passed is True
        assert len(report.count_mismatches) == 0
        assert len(report.round_trip_failures) == 0
        assert len(report.isolation_breaches) == 0

    @pytest.mark.asyncio
    async def test_single_source_matching_counts(self) -> None:
        src = _make_source_mock(source_id=_SRC_1, tenant_id=_WS_A)
        gs = _make_graph_store()
        vs = _make_vector_store()
        cfg = _make_config()
        factory = _build_session_factory(sources=[src])

        with (
            patch(
                "omniscience_index.migration.verify._count_entities_for_source",
                new=AsyncMock(return_value=5),
            ),
            patch(
                "omniscience_index.migration.verify._count_edges_for_source",
                new=AsyncMock(return_value=3),
            ),
            patch(
                "omniscience_index.migration.verify._count_chunks_for_source",
                new=AsyncMock(return_value=10),
            ),
            patch(
                "omniscience_index.migration.verify._neo4j_count_entities_for_source",
                new=AsyncMock(return_value=5),
            ),
            patch(
                "omniscience_index.migration.verify._neo4j_count_edges_for_source",
                new=AsyncMock(return_value=3),
            ),
            patch(
                "omniscience_index.migration.verify._qdrant_count_chunks_for_source",
                new=AsyncMock(return_value=10),
            ),
            patch(
                "omniscience_index.migration.verify.resolve_source_workspace",
                return_value=_WS_A,
            ),
        ):
            runner = VerifyRunner(
                config=cfg,
                session_factory=factory,
                graph_store=gs,
                vector_store=vs,
            )
            report = await runner.run()

        assert report.passed is True
        assert len(report.count_mismatches) == 0

    @pytest.mark.asyncio
    async def test_count_mismatch_recorded(self) -> None:
        src = _make_source_mock(source_id=_SRC_1, tenant_id=_WS_A)
        gs = _make_graph_store()
        vs = _make_vector_store()
        cfg = _make_config()
        factory = _build_session_factory(sources=[src])

        with (
            patch(
                "omniscience_index.migration.verify._count_entities_for_source",
                new=AsyncMock(return_value=5),
            ),
            patch(
                "omniscience_index.migration.verify._count_edges_for_source",
                new=AsyncMock(return_value=3),
            ),
            patch(
                "omniscience_index.migration.verify._count_chunks_for_source",
                new=AsyncMock(return_value=10),
            ),
            patch(
                "omniscience_index.migration.verify._neo4j_count_entities_for_source",
                new=AsyncMock(return_value=3),  # mismatch: pg=5, neo4j=3
            ),
            patch(
                "omniscience_index.migration.verify._neo4j_count_edges_for_source",
                new=AsyncMock(return_value=3),
            ),
            patch(
                "omniscience_index.migration.verify._qdrant_count_chunks_for_source",
                new=AsyncMock(return_value=10),
            ),
            patch(
                "omniscience_index.migration.verify.resolve_source_workspace",
                return_value=_WS_A,
            ),
        ):
            runner = VerifyRunner(
                config=cfg,
                session_factory=factory,
                graph_store=gs,
                vector_store=vs,
            )
            report = await runner.run()

        assert report.passed is False
        assert len(report.count_mismatches) == 1
        assert report.count_mismatches[0].kind == "entities"
        assert report.count_mismatches[0].pgvector == 5
        assert report.count_mismatches[0].hybrid == 3

    @pytest.mark.asyncio
    async def test_round_trip_with_matching_views(self) -> None:
        src = _make_source_mock(source_id=_SRC_1, tenant_id=_WS_A)
        gs = _make_graph_store()
        vs = _make_vector_store()
        cfg = _make_config()
        factory = _build_session_factory(sources=[src])

        view = _make_entity_view()
        pg_store = AsyncMock()
        pg_store.get_entity = AsyncMock(return_value=view)
        gs.get_entity = AsyncMock(return_value=view)

        entity_obj = MagicMock()
        entity_obj.id = uuid.uuid4()
        entity_obj.name = view.name

        with (
            patch(
                "omniscience_index.migration.verify._count_entities_for_source",
                new=AsyncMock(return_value=1),
            ),
            patch(
                "omniscience_index.migration.verify._count_edges_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._count_chunks_for_source",
                new=AsyncMock(return_value=1),
            ),
            patch(
                "omniscience_index.migration.verify._neo4j_count_entities_for_source",
                new=AsyncMock(return_value=1),
            ),
            patch(
                "omniscience_index.migration.verify._neo4j_count_edges_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._qdrant_count_chunks_for_source",
                new=AsyncMock(return_value=1),
            ),
            patch(
                "omniscience_index.migration.verify._fetch_entities_for_source",
                new=AsyncMock(return_value=[entity_obj]),
            ),
            patch(
                "omniscience_index.migration.verify.resolve_source_workspace",
                return_value=_WS_A,
            ),
        ):
            runner = VerifyRunner(
                config=cfg,
                session_factory=factory,
                graph_store=gs,
                vector_store=vs,
                pgvector_graph_get_entity=pg_store,
                rng_seed=1,
            )
            report = await runner.run()

        assert len(report.round_trip_failures) == 0

    @pytest.mark.asyncio
    async def test_round_trip_with_drifted_views(self) -> None:
        src = _make_source_mock(source_id=_SRC_1, tenant_id=_WS_A)
        gs = _make_graph_store()
        vs = _make_vector_store()
        cfg = _make_config()
        factory = _build_session_factory(sources=[src])

        pg_view = _make_entity_view(name="MyService", kind="service", source="s")
        n4_view = _make_entity_view(name="MyService", kind="function", source="s")

        pg_store = AsyncMock()
        pg_store.get_entity = AsyncMock(return_value=pg_view)
        gs.get_entity = AsyncMock(return_value=n4_view)

        entity_obj = MagicMock()
        entity_obj.id = uuid.uuid4()
        entity_obj.name = "MyService"

        with (
            patch(
                "omniscience_index.migration.verify._count_entities_for_source",
                new=AsyncMock(return_value=1),
            ),
            patch(
                "omniscience_index.migration.verify._count_edges_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._count_chunks_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._neo4j_count_entities_for_source",
                new=AsyncMock(return_value=1),
            ),
            patch(
                "omniscience_index.migration.verify._neo4j_count_edges_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._qdrant_count_chunks_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._fetch_entities_for_source",
                new=AsyncMock(return_value=[entity_obj]),
            ),
            patch(
                "omniscience_index.migration.verify.resolve_source_workspace",
                return_value=_WS_A,
            ),
        ):
            runner = VerifyRunner(
                config=cfg,
                session_factory=factory,
                graph_store=gs,
                vector_store=vs,
                pgvector_graph_get_entity=pg_store,
                rng_seed=1,
            )
            report = await runner.run()

        assert len(report.round_trip_failures) == 1
        assert "kind" in report.round_trip_failures[0].field_diffs

    @pytest.mark.asyncio
    async def test_round_trip_skipped_when_no_pgvector_store(self) -> None:
        src = _make_source_mock(source_id=_SRC_1, tenant_id=_WS_A)
        gs = _make_graph_store()
        vs = _make_vector_store()
        cfg = _make_config()
        factory = _build_session_factory(sources=[src])

        with (
            patch(
                "omniscience_index.migration.verify._count_entities_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._count_edges_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._count_chunks_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._neo4j_count_entities_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._neo4j_count_edges_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._qdrant_count_chunks_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify.resolve_source_workspace",
                return_value=_WS_A,
            ),
        ):
            runner = VerifyRunner(
                config=cfg,
                session_factory=factory,
                graph_store=gs,
                vector_store=vs,
                pgvector_graph_get_entity=None,
            )
            report = await runner.run()

        assert len(report.round_trip_failures) == 0

    @pytest.mark.asyncio
    async def test_single_workspace_isolation_skipped(self) -> None:
        src = _make_source_mock(source_id=_SRC_1, tenant_id=_WS_A)
        gs = _make_graph_store()
        vs = _make_vector_store()
        cfg = _make_config()
        factory = _build_session_factory(sources=[src])

        with (
            patch(
                "omniscience_index.migration.verify._count_entities_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._count_edges_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._count_chunks_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._neo4j_count_entities_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._neo4j_count_edges_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._qdrant_count_chunks_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify.resolve_source_workspace",
                return_value=_WS_A,
            ),
            patch(
                "omniscience_index.migration.verify._scroll_workspace_payloads",
                new=AsyncMock(return_value=[]),
            ) as mock_scroll,
        ):
            runner = VerifyRunner(
                config=cfg,
                session_factory=factory,
                graph_store=gs,
                vector_store=vs,
            )
            report = await runner.run()

        mock_scroll.assert_not_called()
        assert len(report.isolation_breaches) == 0

    @pytest.mark.asyncio
    async def test_two_workspaces_isolation_check_no_breach(self) -> None:
        src_a = _make_source_mock(source_id=_SRC_1, tenant_id=_WS_A, name="src-a")
        src_b = _make_source_mock(source_id=uuid.uuid4(), tenant_id=_WS_B, name="src-b")
        gs = _make_graph_store()
        vs = _make_vector_store()
        cfg = _make_config()
        factory = _build_session_factory(sources=[src_a, src_b])

        with (
            patch(
                "omniscience_index.migration.verify._count_entities_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._count_edges_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._count_chunks_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._neo4j_count_entities_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._neo4j_count_edges_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._qdrant_count_chunks_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify.resolve_source_workspace",
                side_effect=[_WS_A, _WS_B],
            ),
            patch(
                "omniscience_index.migration.verify._scroll_workspace_payloads",
                new=AsyncMock(return_value=[]),
            ),
        ):
            runner = VerifyRunner(
                config=cfg,
                session_factory=factory,
                graph_store=gs,
                vector_store=vs,
            )
            report = await runner.run()

        assert len(report.isolation_breaches) == 0

    @pytest.mark.asyncio
    async def test_two_workspaces_isolation_breach_detected(self) -> None:
        src_a = _make_source_mock(source_id=_SRC_1, tenant_id=_WS_A, name="src-a")
        src_b = _make_source_mock(source_id=uuid.uuid4(), tenant_id=_WS_B, name="src-b")
        gs = _make_graph_store()
        vs = _make_vector_store()
        cfg = _make_config()
        factory = _build_session_factory(sources=[src_a, src_b])

        # Use _WS_C which matches neither _WS_A nor _WS_B.
        # The scroll is called once per workspace in the workspace_ids_seen set
        # (2 calls).  Since _WS_C != _WS_A and _WS_C != _WS_B, whichever
        # workspace is probed the payload workspace_id will not match → breach.
        # This makes the test order-independent (set iteration is undefined).
        chunk_id = uuid.uuid4()
        leaked_payload = {
            "workspace_id": str(_WS_C),
            "chunk_id": str(chunk_id),
        }

        with (
            patch(
                "omniscience_index.migration.verify._count_entities_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._count_edges_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._count_chunks_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._neo4j_count_entities_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._neo4j_count_edges_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify._qdrant_count_chunks_for_source",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "omniscience_index.migration.verify.resolve_source_workspace",
                side_effect=[_WS_A, _WS_B],
            ),
            patch(
                "omniscience_index.migration.verify._scroll_workspace_payloads",
                new=AsyncMock(return_value=[leaked_payload]),
            ),
            patch(
                "omniscience_index.migration.verify.QdrantFilterBuilder",
            ),
        ):
            runner = VerifyRunner(
                config=cfg,
                session_factory=factory,
                graph_store=gs,
                vector_store=vs,
            )
            report = await runner.run()

        assert len(report.isolation_breaches) >= 1


# ===========================================================================
# _neo4j_count_entities_for_source / _neo4j_count_edges_for_source
# ===========================================================================


class TestNeo4jCountHelpers:
    @pytest.mark.asyncio
    async def test_entities_count_returned(self) -> None:
        record = MagicMock()
        record.__getitem__ = lambda self, key: 7

        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value=record)

        mock_session = AsyncMock()
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        mock_driver = MagicMock()
        mock_driver.session = MagicMock(return_value=mock_session)

        gs = _make_graph_store()
        gs._driver = mock_driver

        result = await _neo4j_count_entities_for_source(
            graph_store=gs,
            source_id=_SRC_1,
            workspace_id=_WS_A,
        )
        assert result == 7

    @pytest.mark.asyncio
    async def test_entities_count_zero_when_no_record(self) -> None:
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value=None)

        mock_session = AsyncMock()
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        mock_driver = MagicMock()
        mock_driver.session = MagicMock(return_value=mock_session)

        gs = _make_graph_store()
        gs._driver = mock_driver

        result = await _neo4j_count_entities_for_source(
            graph_store=gs,
            source_id=_SRC_1,
            workspace_id=_WS_A,
        )
        assert result == 0

    @pytest.mark.asyncio
    async def test_edges_count_returned(self) -> None:
        record = MagicMock()
        record.__getitem__ = lambda self, key: 4

        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value=record)

        mock_session = AsyncMock()
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        mock_driver = MagicMock()
        mock_driver.session = MagicMock(return_value=mock_session)

        gs = _make_graph_store()
        gs._driver = mock_driver

        result = await _neo4j_count_edges_for_source(
            graph_store=gs,
            source_id=_SRC_1,
            workspace_id=_WS_A,
        )
        assert result == 4


# ===========================================================================
# _qdrant_count_chunks_for_source
# ===========================================================================


class TestQdrantCountChunks:
    @pytest.mark.asyncio
    async def test_count_returned(self) -> None:
        count_result = MagicMock()
        count_result.count = 15

        vs = _make_vector_store()
        vs._qc.count = AsyncMock(return_value=count_result)

        with patch("omniscience_index.migration.verify.QdrantFilterBuilder") as mock_fb:
            mock_fb.return_value.with_source_ids.return_value = mock_fb.return_value
            mock_fb.return_value.exclude_tombstoned.return_value = mock_fb.return_value
            mock_fb.return_value.build.return_value = MagicMock()

            result = await _qdrant_count_chunks_for_source(
                vector_store=vs,
                source_id=_SRC_1,
                workspace_id=_WS_A,
            )

        assert result == 15


# ===========================================================================
# _scroll_workspace_payloads — pagination
# ===========================================================================


class TestScrollWorkspacePayloads:
    @pytest.mark.asyncio
    async def test_single_page_no_next_offset(self) -> None:
        import qdrant_client.http.models as qm

        vs = _make_vector_store()
        rec1 = MagicMock()
        rec1.id = str(uuid.uuid4())
        rec1.payload = {"workspace_id": str(_WS_A), "other": "ignored"}
        vs._qc.scroll = AsyncMock(return_value=([rec1], None))

        flt = MagicMock(spec=qm.Filter)
        result = await _scroll_workspace_payloads(vs, flt)

        assert len(result) == 1
        assert result[0]["workspace_id"] == str(_WS_A)
        assert result[0]["chunk_id"] == rec1.id

    @pytest.mark.asyncio
    async def test_multi_page_pagination(self) -> None:
        import qdrant_client.http.models as qm

        vs = _make_vector_store()
        rec1 = MagicMock()
        rec1.id = str(uuid.uuid4())
        rec1.payload = {"workspace_id": str(_WS_A)}
        rec2 = MagicMock()
        rec2.id = str(uuid.uuid4())
        rec2.payload = {"workspace_id": str(_WS_A)}

        next_offset = "some-cursor"
        vs._qc.scroll = AsyncMock(
            side_effect=[
                ([rec1], next_offset),
                ([rec2], None),
            ]
        )

        flt = MagicMock(spec=qm.Filter)
        result = await _scroll_workspace_payloads(vs, flt)

        assert len(result) == 2
        assert vs._qc.scroll.call_count == 2

    @pytest.mark.asyncio
    async def test_empty_payload_handled(self) -> None:
        import qdrant_client.http.models as qm

        vs = _make_vector_store()
        rec = MagicMock()
        rec.id = str(uuid.uuid4())
        rec.payload = None  # None payload -> falls back to {}
        vs._qc.scroll = AsyncMock(return_value=([rec], None))

        flt = MagicMock(spec=qm.Filter)
        result = await _scroll_workspace_payloads(vs, flt)

        assert len(result) == 1
        assert result[0]["workspace_id"] is None

    @pytest.mark.asyncio
    async def test_empty_results(self) -> None:
        import qdrant_client.http.models as qm

        vs = _make_vector_store()
        vs._qc.scroll = AsyncMock(return_value=([], None))

        flt = MagicMock(spec=qm.Filter)
        result = await _scroll_workspace_payloads(vs, flt)

        assert result == []
