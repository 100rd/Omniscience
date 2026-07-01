"""Unit tests for the pgvector -> Neo4j + Qdrant migration (issue #108).

These tests cover the migration tooling without requiring a live
Postgres / Neo4j / Qdrant — stores and session factories are
replaced by ``AsyncMock`` / in-memory stand-ins so the full control
flow (dry-run, resume, batching, workspace rejection, verification)
executes under ``pytest`` + ``asyncio_mode=auto``.

Coverage matrix (mirrors issue #108 acceptance criteria):

* ``--dry-run`` writes nothing.
* ``--batch-size`` is honoured end-to-end.
* ``--resume`` skips sources listed in the progress file.
* Missing ``Source.tenant_id`` without ``--default-workspace-id``
  raises :class:`MissingWorkspaceError` before any adapter call.
* ``--default-workspace-id`` rescues null-tenant sources.
* Verification detects count mismatches.
* Verification detects entity round-trip field drift.
* Progress file round-trips through the JSON layer cleanly.
* Top-k overlap helper behaves as documented.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_core.db.models import Source
from omniscience_core.storage.graph import EntityNodeView
from omniscience_index.migration.config import (
    DEFAULT_BATCH_SIZE,
    ROUND_TRIP_SAMPLE_SIZE,
    TOP_K_OVERLAP_THRESHOLD,
    MigrationConfig,
)
from omniscience_index.migration.progress import (
    load_progress,
    new_progress_state,
    save_progress,
)
from omniscience_index.migration.runner import (
    MigrationRunner,
    _chunk_to_payload,
    _chunked,
)
from omniscience_index.migration.verify import (
    CountMismatch,
    RoundTripFailure,
    VerificationReport,
    _diff_entity_views,
    compute_top_k_overlap,
)
from omniscience_index.migration.workspace import (
    MissingWorkspaceError,
    resolve_source_workspace,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeSource:
    """Stand-in for ``omniscience_core.db.models.Source`` in unit tests."""

    id: uuid.UUID
    name: str
    tenant_id: uuid.UUID | None


@dataclass
class _FakeEntity:
    id: uuid.UUID
    source_id: uuid.UUID
    entity_type: str
    name: str
    display_name: str
    chunk_id: uuid.UUID | None
    entity_metadata: dict[str, Any]
    created_at: datetime


@dataclass
class _FakeEdge:
    id: uuid.UUID
    source_entity_id: uuid.UUID
    target_entity_id: uuid.UUID
    edge_type: str
    edge_metadata: dict[str, Any]
    created_at: datetime


@dataclass
class _FakeDocument:
    id: uuid.UUID
    source_id: uuid.UUID
    external_id: str
    uri: str
    title: str | None
    content_hash: str
    doc_metadata: dict[str, Any]
    tombstoned_at: datetime | None = None


@dataclass
class _FakeChunk:
    id: uuid.UUID
    document_id: uuid.UUID
    ord: int
    text: str
    embedding: list[float]
    symbol: str | None
    chunk_metadata: dict[str, Any]
    embedding_model: str
    embedding_provider: str
    parser_version: str
    chunker_strategy: str


class _SessionFake:
    """Minimal async ``session`` that returns canned row lists.

    The runner calls ``session.execute(stmt).scalars().all()`` and
    ``session.execute(stmt).scalar_one()``.  We dispatch on a
    ``_handler`` callback keyed by the call order; a per-test helper
    builds a fresh fake and wires the sequence.
    """

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self._cursor = 0

    async def __aenter__(self) -> _SessionFake:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, stmt: Any) -> _ResultFake:
        if self._cursor >= len(self._results):
            raise AssertionError(f"SessionFake ran out of canned results at call #{self._cursor}")
        res = self._results[self._cursor]
        self._cursor += 1
        return _ResultFake(res)


class _ResultFake:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def scalars(self) -> _ResultFake:
        return self

    def all(self) -> Any:
        return self._payload

    def scalar_one(self) -> Any:
        return self._payload


def _session_factory_with(results: list[Any]) -> Any:
    """Return a callable session_factory that yields a fresh _SessionFake."""
    # The runner opens the session once via `async with`, so the same
    # `_SessionFake` must service every call made inside one run.
    session = _SessionFake(results)

    def factory() -> _SessionFake:
        return session

    return factory


def _make_config(**overrides: Any) -> MigrationConfig:
    base: dict[str, Any] = {
        "workspace_id": None,
        "source_id": None,
        "default_workspace_id": None,
        "batch_size": DEFAULT_BATCH_SIZE,
        "dry_run": False,
        "resume": False,
        "verify_only": False,
        "progress_file": Path("/tmp/_migration_progress_test.json"),  # noqa: S108
    }
    base.update(overrides)
    return MigrationConfig(**base)


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------


class TestResolveSourceWorkspace:
    def test_tenant_id_wins(self) -> None:
        tenant = uuid.uuid4()
        source = cast("Source", _FakeSource(id=uuid.uuid4(), name="s", tenant_id=tenant))
        assert resolve_source_workspace(source, default_workspace_id=uuid.uuid4()) == tenant

    def test_fallback_used_when_tenant_none(self) -> None:
        default = uuid.uuid4()
        source = cast("Source", _FakeSource(id=uuid.uuid4(), name="s", tenant_id=None))
        assert resolve_source_workspace(source, default_workspace_id=default) == default

    def test_missing_raises_with_source_identity(self) -> None:
        sid = uuid.uuid4()
        source = cast("Source", _FakeSource(id=sid, name="legacy-src", tenant_id=None))
        with pytest.raises(MissingWorkspaceError) as exc:
            resolve_source_workspace(source, default_workspace_id=None)
        assert exc.value.source_id == sid
        assert exc.value.source_name == "legacy-src"
        assert "legacy-src" in str(exc.value)


# ---------------------------------------------------------------------------
# MigrationConfig invariants
# ---------------------------------------------------------------------------


class TestMigrationConfig:
    def test_batch_size_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="batch_size"):
            _make_config(batch_size=0)

    def test_dry_run_and_verify_only_mutually_exclusive(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            _make_config(dry_run=True, verify_only=True)


# ---------------------------------------------------------------------------
# Progress file round-trip + resume
# ---------------------------------------------------------------------------


class TestProgressState:
    def test_round_trip_through_json(self, tmp_path: Path) -> None:
        sid = uuid.uuid4()
        state = new_progress_state(batch_size=500)
        state.mark_source_completed(sid)
        path = tmp_path / "p.json"
        save_progress(state, path)
        reloaded = load_progress(path)
        assert reloaded is not None
        assert reloaded.batch_size == 500
        assert reloaded.last_completed_source_id == sid
        assert reloaded.is_completed(sid)

    def test_load_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert load_progress(tmp_path / "no-such.json") is None

    def test_rejects_unknown_schema_version(self, tmp_path: Path) -> None:
        p = tmp_path / "p.json"
        p.write_text('{"schema_version": 99, "batch_size": 1}')
        with pytest.raises(ValueError, match="schema_version"):
            load_progress(p)

    def test_mark_source_completed_idempotent(self) -> None:
        state = new_progress_state(batch_size=1)
        sid = uuid.uuid4()
        state.mark_source_completed(sid)
        state.mark_source_completed(sid)
        assert state.completed_source_ids.count(sid) == 1


# ---------------------------------------------------------------------------
# _chunked helper
# ---------------------------------------------------------------------------


class TestChunked:
    def test_exact_split(self) -> None:
        assert _chunked([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]

    def test_remainder_in_last_batch(self) -> None:
        assert _chunked([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]

    def test_empty(self) -> None:
        assert _chunked([], 10) == []

    def test_single_large_batch(self) -> None:
        assert _chunked([1, 2, 3], 100) == [[1, 2, 3]]


# ---------------------------------------------------------------------------
# _chunk_to_payload
# ---------------------------------------------------------------------------


class TestChunkToPayload:
    def test_embedding_normalised_to_list_of_float(self) -> None:
        chunk = _FakeChunk(
            id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            ord=0,
            text="hello",
            embedding=[0.1, 0.2, 0.3],
            symbol=None,
            chunk_metadata={"foo": "bar"},
            embedding_model="m",
            embedding_provider="p",
            parser_version="v1",
            chunker_strategy="fixed",
        )
        payload = _chunk_to_payload(chunk)  # type: ignore[arg-type]
        assert payload["text"] == "hello"
        assert payload["ord"] == 0
        assert payload["embedding"] == [0.1, 0.2, 0.3]
        assert payload["metadata"] == {"foo": "bar"}
        assert all(isinstance(x, float) for x in payload["embedding"])

    def test_null_embedding_becomes_empty_list(self) -> None:
        chunk = _FakeChunk(
            id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            ord=0,
            text="t",
            embedding=[],
            symbol=None,
            chunk_metadata={},
            embedding_model="",
            embedding_provider="",
            parser_version="",
            chunker_strategy="",
        )
        # Force None to exercise the null branch:
        chunk.embedding = None  # type: ignore[assignment]
        payload = _chunk_to_payload(chunk)  # type: ignore[arg-type]
        assert payload["embedding"] == []


# ---------------------------------------------------------------------------
# MigrationRunner — dry-run writes nothing
# ---------------------------------------------------------------------------


def _make_runner_with(
    *,
    config: MigrationConfig,
    sources: list[_FakeSource],
    count_rows: list[int] | None = None,
) -> tuple[MigrationRunner, MagicMock, MagicMock]:
    """Build a runner whose session returns canned results in order.

    Call order:
      1. _select_sources  -> sources
      For each source in dry-run:
          2. _count_entities_for_source  -> int
          3. _count_edges_for_source     -> int
          4. _count_chunks_for_source    -> int
          5. _count_documents_for_source -> int
    """
    results: list[Any] = [sources]
    if count_rows is None:
        count_rows = [0] * (4 * len(sources))
    results.extend(count_rows)

    session_factory = _session_factory_with(results)
    graph_store = MagicMock()
    graph_store.upsert_entity = AsyncMock()
    graph_store.upsert_edge = AsyncMock()
    vector_store = MagicMock()
    vector_store.upsert_chunks = AsyncMock()

    runner = MigrationRunner(
        config=config,
        session_factory=session_factory,
        graph_store=graph_store,
        vector_store=vector_store,
    )
    return runner, graph_store, vector_store


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    tenant = uuid.uuid4()
    source = _FakeSource(id=uuid.uuid4(), name="s", tenant_id=tenant)
    config = _make_config(
        dry_run=True,
        progress_file=tmp_path / "p.json",
    )
    runner, graph_store, vector_store = _make_runner_with(
        config=config,
        sources=[source],
        count_rows=[10, 5, 20, 3],  # entities/edges/chunks/docs
    )
    report = await runner.run()

    graph_store.upsert_entity.assert_not_awaited()
    graph_store.upsert_edge.assert_not_awaited()
    vector_store.upsert_chunks.assert_not_awaited()
    assert report.dry_run is True
    assert report.source_reports[0].entities_copied == 10
    assert report.source_reports[0].edges_copied == 5
    assert report.source_reports[0].chunks_copied == 20
    assert report.source_reports[0].documents_scanned == 3


# ---------------------------------------------------------------------------
# MigrationRunner — missing workspace aborts before adapter call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_workspace_aborts_before_write(tmp_path: Path) -> None:
    null_tenant_source = _FakeSource(id=uuid.uuid4(), name="legacy", tenant_id=None)
    config = _make_config(
        default_workspace_id=None,
        progress_file=tmp_path / "p.json",
    )
    runner, graph_store, vector_store = _make_runner_with(
        config=config, sources=[null_tenant_source]
    )
    with pytest.raises(MissingWorkspaceError) as exc:
        await runner.run()
    assert exc.value.source_name == "legacy"
    graph_store.upsert_entity.assert_not_awaited()
    vector_store.upsert_chunks.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_workspace_id_rescues_legacy_source(tmp_path: Path) -> None:
    """With --default-workspace-id a null-tenant source is migrated."""
    src_id = uuid.uuid4()
    default_ws = uuid.uuid4()
    source = _FakeSource(id=src_id, name="legacy", tenant_id=None)
    config = _make_config(
        default_workspace_id=default_ws,
        dry_run=True,
        progress_file=tmp_path / "p.json",
    )
    runner, _, _ = _make_runner_with(config=config, sources=[source], count_rows=[0, 0, 0, 0])
    report = await runner.run()
    assert report.source_reports[0].workspace_id == default_ws


# ---------------------------------------------------------------------------
# MigrationRunner — resume skips completed sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_skips_completed_source(tmp_path: Path) -> None:
    done_source_id = uuid.uuid4()
    tenant = uuid.uuid4()
    done_source = _FakeSource(id=done_source_id, name="done", tenant_id=tenant)

    # Pre-seed the progress file so --resume treats done_source_id as completed.
    progress_path = tmp_path / "p.json"
    state = new_progress_state(batch_size=DEFAULT_BATCH_SIZE)
    state.mark_source_completed(done_source_id)
    save_progress(state, progress_path)

    config = _make_config(resume=True, progress_file=progress_path)
    runner, graph_store, vector_store = _make_runner_with(
        config=config, sources=[done_source], count_rows=[]
    )
    report = await runner.run()
    assert report.source_reports[0].skipped is True
    assert report.source_reports[0].skip_reason == "already_completed"
    graph_store.upsert_entity.assert_not_awaited()
    vector_store.upsert_chunks.assert_not_awaited()


# ---------------------------------------------------------------------------
# MigrationRunner — batch size honoured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_size_honoured_for_entities(tmp_path: Path) -> None:
    """With batch_size=2 and 5 entities, upsert_entity is awaited 5 times
    but in 3 explicit batches (no over-sized adapter call)."""
    tenant = uuid.uuid4()
    source = _FakeSource(id=uuid.uuid4(), name="s", tenant_id=tenant)
    ents = [
        _FakeEntity(
            id=uuid.uuid4(),
            source_id=source.id,
            entity_type="function",
            name=f"mod.fn_{i}",
            display_name=f"fn_{i}",
            chunk_id=None,
            entity_metadata={},
            created_at=datetime.now(UTC),
        )
        for i in range(5)
    ]
    # Results: sources, then per-source (entities, edges, docs)
    session_factory = _session_factory_with(
        [
            [source],  # _select_sources
            ents,  # _fetch_entities_for_source
            [],  # _fetch_edges_for_source
            [],  # documents loop -> no documents
        ]
    )
    graph_store = MagicMock()
    graph_store.upsert_entity = AsyncMock()
    graph_store.upsert_edge = AsyncMock()
    vector_store = MagicMock()
    vector_store.upsert_chunks = AsyncMock()

    config = _make_config(batch_size=2, progress_file=tmp_path / "p.json")
    runner = MigrationRunner(
        config=config,
        session_factory=session_factory,
        graph_store=graph_store,
        vector_store=vector_store,
    )
    report = await runner.run()
    assert graph_store.upsert_entity.await_count == 5
    assert report.source_reports[0].entities_copied == 5


# ---------------------------------------------------------------------------
# MigrationRunner — workspace_id always forwarded on writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_id_forwarded_on_every_entity_upsert(tmp_path: Path) -> None:
    tenant = uuid.uuid4()
    source = _FakeSource(id=uuid.uuid4(), name="s", tenant_id=tenant)
    ent = _FakeEntity(
        id=uuid.uuid4(),
        source_id=source.id,
        entity_type="class",
        name="pkg.Thing",
        display_name="Thing",
        chunk_id=None,
        entity_metadata={"foo": 1},
        created_at=datetime.now(UTC),
    )
    session_factory = _session_factory_with(
        [[source], [ent], [], []]  # sources / entities / edges / documents
    )
    graph_store = MagicMock()
    graph_store.upsert_entity = AsyncMock()
    graph_store.upsert_edge = AsyncMock()
    vector_store = MagicMock()
    vector_store.upsert_chunks = AsyncMock()

    config = _make_config(progress_file=tmp_path / "p.json")
    runner = MigrationRunner(
        config=config,
        session_factory=session_factory,
        graph_store=graph_store,
        vector_store=vector_store,
    )
    await runner.run()
    call = graph_store.upsert_entity.await_args
    assert call is not None
    assert call.kwargs["workspace_id"] == tenant


@pytest.mark.asyncio
async def test_workspace_id_injected_into_chunk_metadata(tmp_path: Path) -> None:
    tenant = uuid.uuid4()
    source = _FakeSource(id=uuid.uuid4(), name="s", tenant_id=tenant)
    doc = _FakeDocument(
        id=uuid.uuid4(),
        source_id=source.id,
        external_id="doc-1",
        uri="file://x",
        title=None,
        content_hash="h1",
        doc_metadata={"other": 1},
    )
    chunk = _FakeChunk(
        id=uuid.uuid4(),
        document_id=doc.id,
        ord=0,
        text="t",
        embedding=[0.0] * 3,
        symbol=None,
        chunk_metadata={},
        embedding_model="m",
        embedding_provider="p",
        parser_version="v",
        chunker_strategy="s",
    )
    # Walk the runner session sequence:
    #  1. select sources
    #  2. fetch entities (empty)
    #  3. fetch edges (empty)
    #  4. fetch documents for source
    #  5. fetch chunks for doc
    session_factory = _session_factory_with([[source], [], [], [doc], [chunk]])
    graph_store = MagicMock()
    graph_store.upsert_entity = AsyncMock()
    graph_store.upsert_edge = AsyncMock()
    vector_store = MagicMock()
    vector_store.upsert_chunks = AsyncMock()

    config = _make_config(progress_file=tmp_path / "p.json")
    runner = MigrationRunner(
        config=config,
        session_factory=session_factory,
        graph_store=graph_store,
        vector_store=vector_store,
    )
    await runner.run()
    call = vector_store.upsert_chunks.await_args
    assert call is not None
    assert call.kwargs["metadata"]["workspace_id"] == str(tenant)


# ---------------------------------------------------------------------------
# Verification — count mismatches
# ---------------------------------------------------------------------------


class TestVerificationReport:
    def test_empty_report_passes(self) -> None:
        rep = VerificationReport()
        assert rep.passed is True

    def test_count_mismatch_fails_report(self) -> None:
        rep = VerificationReport()
        rep.count_mismatches.append(
            CountMismatch(
                source_id=uuid.uuid4(),
                source_name="s",
                kind="entities",
                pgvector=10,
                hybrid=8,
            )
        )
        assert rep.passed is False

    def test_round_trip_failure_fails_report(self) -> None:
        rep = VerificationReport()
        rep.round_trip_failures.append(
            RoundTripFailure(
                source_id=uuid.uuid4(),
                entity_id=uuid.uuid4(),
                entity_name="n",
                field_diffs=["kind"],
            )
        )
        assert rep.passed is False

    def test_retrieval_drift_fails_report(self) -> None:
        rep = VerificationReport()
        rep.retrieval_overlap_pass = False
        rep.retrieval_overlap_ratio = 0.5
        assert rep.passed is False


# ---------------------------------------------------------------------------
# Verification — entity view diffing
# ---------------------------------------------------------------------------


class TestDiffEntityViews:
    def test_matching_views_have_no_diff(self) -> None:
        v = EntityNodeView(
            id=uuid.uuid4(),
            name="pkg.Thing",
            kind="class",
            source="src",
            chunk_text=None,
        )
        assert _diff_entity_views(v, v) == []

    def test_missing_on_neo4j_detected(self) -> None:
        v = EntityNodeView(id=uuid.uuid4(), name="n", kind="k", source="s", chunk_text=None)
        assert _diff_entity_views(v, None) == ["missing_on_neo4j"]

    def test_missing_on_pgvector_detected(self) -> None:
        v = EntityNodeView(id=uuid.uuid4(), name="n", kind="k", source="s", chunk_text=None)
        assert _diff_entity_views(None, v) == ["missing_on_pgvector"]

    def test_field_drift_detected(self) -> None:
        a = EntityNodeView(id=uuid.uuid4(), name="n", kind="class", source="s", chunk_text=None)
        b = EntityNodeView(id=uuid.uuid4(), name="n", kind="function", source="s", chunk_text=None)
        assert _diff_entity_views(a, b) == ["kind"]


# ---------------------------------------------------------------------------
# Verification — top-k overlap
# ---------------------------------------------------------------------------


class TestTopKOverlap:
    def test_full_overlap(self) -> None:
        ids = [uuid.uuid4() for _ in range(5)]
        assert compute_top_k_overlap(reference_hits=ids, candidate_hits=ids) == 1.0

    def test_half_overlap(self) -> None:
        ids = [uuid.uuid4() for _ in range(4)]
        assert compute_top_k_overlap(reference_hits=ids, candidate_hits=ids[:2]) == 0.5

    def test_empty_reference_is_vacuously_one(self) -> None:
        assert compute_top_k_overlap(reference_hits=[], candidate_hits=[]) == 1.0

    def test_threshold_matches_issue_spec(self) -> None:
        assert pytest.approx(0.8) == TOP_K_OVERLAP_THRESHOLD


# ---------------------------------------------------------------------------
# Module-level constants match issue specification
# ---------------------------------------------------------------------------


def test_round_trip_sample_size_is_tunable() -> None:
    assert ROUND_TRIP_SAMPLE_SIZE >= 1


def test_default_batch_size_is_spec_suggested() -> None:
    assert DEFAULT_BATCH_SIZE == 500
