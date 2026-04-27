"""Unit tests for retention metric emission (issue #136, ADR-0009 §8).

Covers the contract that the worker emits the four metric families
specified in ADR-0009 §8 with the right label cardinalities and values.
This is complementary to ``test_retention_worker.py`` (which covers
correctness of the eviction pipeline) — these tests assert that the
dashboard / alert layer (issue #136) sees what it expects.

Specifically:

1. ``omniscience_graph_records_total`` is set on every workspace tick
   for both stores, with non-zero values when the underlying counters
   are non-zero.
2. ``omniscience_retention_eviction_total`` increments by the moved
   row count on a non-dry-run pass.
3. ``omniscience_retention_worker_duration_seconds`` records at least
   one observation per phase per store on a non-dry-run pass.
4. ``omniscience_retention_worker_lag_seconds`` reflects the maximum
   per-workspace lag.
5. ``eviction_inconsistent`` state surfaces as cross-store divergence
   in the eviction counter (cross-store mismatch invariant per
   ADR-0009 §9).

The tests use the same async-mock harness as ``test_retention_worker``
so no live store is required.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_core.config import Settings
from omniscience_core.telemetry.metrics import (
    RETENTION_EVICTION_TOTAL,
    RETENTION_GRAPH_RECORDS_TOTAL,
    RETENTION_WORKER_DURATION_SECONDS,
    RETENTION_WORKER_LAG_SECONDS,
)
from omniscience_server.retention_constants import (
    PHASE_MARK,
    PHASE_MOVE,
    PHASE_READ,
    STORE_LABEL_NEO4J,
    STORE_LABEL_QDRANT,
    TIER_LABEL_ARCHIVE,
    TIER_LABEL_HOT,
    TIER_LABEL_WARM,
    TRANSITION_HOT_TO_WARM,
)
from omniscience_server.retention_worker import RetentionWorker

# ---------------------------------------------------------------------------
# Shared fixtures (mirrors test_retention_worker.py for consistency)
# ---------------------------------------------------------------------------


_NOW: datetime = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
_WS_A: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_WS_B: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-00000000000b")


def _make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "retention_hot_days": 90,
        "retention_warm_days": 365,
        "retention_archive_years": 7,
        "retention_tick_seconds": 60,
        "retention_batch_size": 500,
        "retention_dry_run": False,
        "retention_enabled": True,
        "retention_archive_bucket": None,
        "retention_archive_kms_key_arn": None,
        "retention_archive_s3_endpoint_url": None,
        "retention_archive_s3_region": "us-east-1",
        "retention_sample_size": 5,
    }
    base.update(overrides)
    return Settings(**base)


def _session_factory_for(*workspace_ids: uuid.UUID) -> Any:
    session = AsyncMock()

    async def _execute(_stmt: Any) -> Any:
        result = MagicMock()
        result.all.return_value = [(wid,) for wid in workspace_ids]
        return result

    session.execute = _execute
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _make_graph_store(
    *,
    eligible_es: int = 0,
    eligible_edges: int = 0,
    archive_eligible: int = 0,
    sample: list[dict[str, Any]] | None = None,
    oldest: datetime | None = None,
    move_es: int = 0,
    move_edges: int = 0,
    records: dict[str, int] | None = None,
) -> AsyncMock:
    store = AsyncMock()
    store.count_hot_to_warm_eligible = AsyncMock(return_value=(eligible_es, eligible_edges))
    store.count_warm_to_archive_eligible = AsyncMock(return_value=archive_eligible)
    store.list_warm_to_archive_dates = AsyncMock(return_value=[])
    store.sample_hot_to_warm_eligible = AsyncMock(return_value=sample or [])
    store.oldest_hot_to_warm_recorded_at = AsyncMock(return_value=oldest)
    store.mark_hot_to_warm = AsyncMock(return_value=(eligible_es, eligible_edges))
    store.move_hot_to_warm = AsyncMock(return_value=(move_es, move_edges))
    store.fetch_warm_snapshot_rows = AsyncMock(return_value=([], []))
    store.delete_warm_snapshot = AsyncMock(return_value=(0, 0))
    store.count_records_by_tier = AsyncMock(return_value=records or {"hot": 0, "warm": 0})
    return store


def _make_vector_store(
    *,
    eligible_chunks: int = 0,
    marked: int = 0,
    deleted: int = 0,
    by_tier: dict[str, int] | None = None,
) -> AsyncMock:
    store = AsyncMock()
    store.count_retention_eligible = AsyncMock(return_value=eligible_chunks)
    store.mark_retention_warm = AsyncMock(return_value=marked)
    store.delete_retention_archive = AsyncMock(return_value=deleted)
    store.count_chunks_by_tier = AsyncMock(return_value=by_tier or {"hot": 0, "warm": 0})
    return store


def _gauge_value(gauge: Any, **labels: str) -> float:
    """Read a gauge's current value for a specific label set.

    Uses ``collect()`` so we depend on the prometheus_client *public*
    sample interface rather than internal underscore-prefixed
    attributes that change between releases.
    """
    label_kv = {k: str(v) for k, v in labels.items()}
    for metric in gauge.collect():
        for sample in metric.samples:
            if sample.name == metric.name and sample.labels == label_kv:
                return float(sample.value)
    raise AssertionError(f"no sample for {gauge._name} with labels {label_kv}")


def _counter_value(counter: Any, **labels: str) -> float:
    """Read a counter's `_total` sample for a specific label set."""
    label_kv = {k: str(v) for k, v in labels.items()}
    target_name = counter._name + "_total"
    for metric in counter.collect():
        for sample in metric.samples:
            if sample.name == target_name and sample.labels == label_kv:
                return float(sample.value)
    raise AssertionError(f"no _total sample for {counter._name} with labels {label_kv}")


def _histogram_count(histogram: Any, **labels: str) -> int:
    """Return the cumulative sample count for a labelled histogram cell.

    Reads the ``_count`` sample emitted by ``collect()`` — the public
    interface for sample counts. Avoids the brittle private bucket-list
    attribute that varies between prometheus_client releases.
    """
    label_kv = {k: str(v) for k, v in labels.items()}
    target_name = histogram._name + "_count"
    for metric in histogram.collect():
        for sample in metric.samples:
            if sample.name == target_name and sample.labels == label_kv:
                return int(sample.value)
    return 0


# ---------------------------------------------------------------------------
# Test 1: graph_records_total is set per (tier, store) on every tick
# ---------------------------------------------------------------------------


async def test_graph_records_total_set_per_tier_and_store() -> None:
    """Worker emits the gauge for every (tier, store) cell it owns.

    ADR-0009 §8: ``omniscience_graph_records_total{tier, store}``
    Gauge. The worker writes hot+warm for both Neo4j and Qdrant on
    every tick, plus archive=0 for Qdrant (re-embed from archive is
    not supported in v1; explicit 0 keeps the gauge from going stale
    across deploys).
    """
    settings = _make_settings()
    graph = _make_graph_store(records={"hot": 1234, "warm": 567})
    vector = _make_vector_store(by_tier={"hot": 89, "warm": 12})
    worker = RetentionWorker(
        session_factory=_session_factory_for(_WS_A),
        graph_store=graph,
        vector_store=vector,
        settings=settings,
    )
    await worker.run_once(now=_NOW)

    assert (
        _gauge_value(RETENTION_GRAPH_RECORDS_TOTAL, tier=TIER_LABEL_HOT, store=STORE_LABEL_NEO4J)
        == 1234.0
    )
    assert (
        _gauge_value(RETENTION_GRAPH_RECORDS_TOTAL, tier=TIER_LABEL_WARM, store=STORE_LABEL_NEO4J)
        == 567.0
    )
    assert (
        _gauge_value(RETENTION_GRAPH_RECORDS_TOTAL, tier=TIER_LABEL_HOT, store=STORE_LABEL_QDRANT)
        == 89.0
    )
    assert (
        _gauge_value(RETENTION_GRAPH_RECORDS_TOTAL, tier=TIER_LABEL_WARM, store=STORE_LABEL_QDRANT)
        == 12.0
    )
    # Archive on Qdrant is structurally 0 — re-embed not supported in v1.
    assert (
        _gauge_value(
            RETENTION_GRAPH_RECORDS_TOTAL, tier=TIER_LABEL_ARCHIVE, store=STORE_LABEL_QDRANT
        )
        == 0.0
    )


# ---------------------------------------------------------------------------
# Test 2: eviction_total counter increments by moved rows on a live tick
# ---------------------------------------------------------------------------


async def test_eviction_total_increments_on_live_tick() -> None:
    """A non-dry-run tick increments the eviction counter by moved rows.

    ADR-0009 §8: ``omniscience_retention_eviction_total{transition,
    store}`` increments per record on the move phase.
    """
    settings = _make_settings()
    graph = _make_graph_store(
        eligible_es=10,
        eligible_edges=2,
        oldest=_NOW - timedelta(days=100),
        move_es=10,
        move_edges=2,
    )
    vector = _make_vector_store(eligible_chunks=5, marked=5)

    before_neo4j = _counter_value(
        RETENTION_EVICTION_TOTAL,
        transition=TRANSITION_HOT_TO_WARM,
        store=STORE_LABEL_NEO4J,
    )
    before_qdrant = _counter_value(
        RETENTION_EVICTION_TOTAL,
        transition=TRANSITION_HOT_TO_WARM,
        store=STORE_LABEL_QDRANT,
    )

    worker = RetentionWorker(
        session_factory=_session_factory_for(_WS_A),
        graph_store=graph,
        vector_store=vector,
        settings=settings,
    )
    await worker.run_once(now=_NOW)

    after_neo4j = _counter_value(
        RETENTION_EVICTION_TOTAL,
        transition=TRANSITION_HOT_TO_WARM,
        store=STORE_LABEL_NEO4J,
    )
    after_qdrant = _counter_value(
        RETENTION_EVICTION_TOTAL,
        transition=TRANSITION_HOT_TO_WARM,
        store=STORE_LABEL_QDRANT,
    )
    # 10 entity-states + 2 edges = 12 evicted on Neo4j.
    assert after_neo4j - before_neo4j == pytest.approx(12.0)
    # Qdrant: marked=5 — chunks moved into warm tier.
    assert after_qdrant - before_qdrant == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Test 3: dry-run does NOT touch the eviction counter
# ---------------------------------------------------------------------------


async def test_dry_run_does_not_increment_eviction_total() -> None:
    """ADR-0009 §3: dry-run emits metrics + reports without writing.

    The eviction counter is the move-phase signal — dry-run skips the
    move phase entirely, so the counter must NOT advance. This is the
    inverse contract of test 2.
    """
    settings = _make_settings(retention_dry_run=True)
    graph = _make_graph_store(
        eligible_es=10,
        eligible_edges=2,
        oldest=_NOW - timedelta(days=100),
        move_es=10,
        move_edges=2,
    )
    vector = _make_vector_store(eligible_chunks=5, marked=5)
    before_neo4j = _counter_value(
        RETENTION_EVICTION_TOTAL,
        transition=TRANSITION_HOT_TO_WARM,
        store=STORE_LABEL_NEO4J,
    )
    worker = RetentionWorker(
        session_factory=_session_factory_for(_WS_A),
        graph_store=graph,
        vector_store=vector,
        settings=settings,
    )
    await worker.run_once(now=_NOW)
    after_neo4j = _counter_value(
        RETENTION_EVICTION_TOTAL,
        transition=TRANSITION_HOT_TO_WARM,
        store=STORE_LABEL_NEO4J,
    )
    assert after_neo4j == before_neo4j
    # And the move/mark methods were not invoked.
    graph.mark_hot_to_warm.assert_not_awaited()
    graph.move_hot_to_warm.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 4: duration histogram observes one sample per phase per tick
# ---------------------------------------------------------------------------


async def test_worker_duration_histogram_observes_each_phase() -> None:
    """ADR-0009 §8: histogram records read/mark/move on every tick.

    The histogram has 12 buckets covering 10ms..5min. We assert that
    a non-dry-run tick produces at least one observation in each
    (phase, store) cell — a regression here would silently zero out
    the dashboard p50/p95/p99 panel.
    """
    settings = _make_settings()
    graph = _make_graph_store(
        eligible_es=1, eligible_edges=0, move_es=1, oldest=_NOW - timedelta(days=100)
    )
    vector = _make_vector_store()

    before_read = _histogram_count(
        RETENTION_WORKER_DURATION_SECONDS, phase=PHASE_READ, store=STORE_LABEL_NEO4J
    )
    before_mark = _histogram_count(
        RETENTION_WORKER_DURATION_SECONDS, phase=PHASE_MARK, store=STORE_LABEL_NEO4J
    )
    before_move = _histogram_count(
        RETENTION_WORKER_DURATION_SECONDS, phase=PHASE_MOVE, store=STORE_LABEL_NEO4J
    )

    worker = RetentionWorker(
        session_factory=_session_factory_for(_WS_A),
        graph_store=graph,
        vector_store=vector,
        settings=settings,
    )
    await worker.run_once(now=_NOW)

    assert (
        _histogram_count(
            RETENTION_WORKER_DURATION_SECONDS, phase=PHASE_READ, store=STORE_LABEL_NEO4J
        )
        > before_read
    )
    assert (
        _histogram_count(
            RETENTION_WORKER_DURATION_SECONDS, phase=PHASE_MARK, store=STORE_LABEL_NEO4J
        )
        > before_mark
    )
    assert (
        _histogram_count(
            RETENTION_WORKER_DURATION_SECONDS, phase=PHASE_MOVE, store=STORE_LABEL_NEO4J
        )
        > before_move
    )


# ---------------------------------------------------------------------------
# Test 5: lag_seconds gauge reflects oldest overdue
# ---------------------------------------------------------------------------


async def test_lag_seconds_gauge_reflects_oldest_overdue_recorded_at() -> None:
    """ADR-0009 §8: lag_seconds = wall-clock since oldest overdue.

    A 100-day-old overdue record at retention_hot_days=90 produces
    ≈ 10 days of lag. The gauge is set deployment-wide as the max of
    per-workspace lags — with one workspace, the value is exactly the
    workspace's lag.
    """
    settings = _make_settings()
    overdue_age_days = 100
    graph = _make_graph_store(
        eligible_es=1,
        oldest=_NOW - timedelta(days=overdue_age_days),
        move_es=1,
    )
    vector = _make_vector_store()
    worker = RetentionWorker(
        session_factory=_session_factory_for(_WS_A),
        graph_store=graph,
        vector_store=vector,
        settings=settings,
    )
    await worker.run_once(now=_NOW)

    gauge_seconds = _gauge_value(RETENTION_WORKER_LAG_SECONDS)
    expected_seconds = overdue_age_days * 86400.0
    # Allow a small slack for monotonic clock drift between the worker's
    # observation and our expected_seconds calculation.
    assert gauge_seconds == pytest.approx(expected_seconds, abs=2.0)


# ---------------------------------------------------------------------------
# Test 6: lag_seconds is zero when no records are overdue
# ---------------------------------------------------------------------------


async def test_lag_seconds_zero_when_no_overdue_records() -> None:
    """No overdue records → lag stays at 0 (clean deployment baseline)."""
    settings = _make_settings()
    graph = _make_graph_store(eligible_es=0, oldest=None)
    vector = _make_vector_store()
    worker = RetentionWorker(
        session_factory=_session_factory_for(_WS_A),
        graph_store=graph,
        vector_store=vector,
        settings=settings,
    )
    await worker.run_once(now=_NOW)
    assert _gauge_value(RETENTION_WORKER_LAG_SECONDS) == 0.0


# ---------------------------------------------------------------------------
# Test 7: cross-store divergence (eviction_inconsistent) surfaces
# ---------------------------------------------------------------------------


async def test_eviction_inconsistent_surfaces_in_metrics() -> None:
    """ADR-0009 §9: cross-store divergence is observable in eviction counter.

    Simulates the divergence shape: Neo4j moves N rows but Qdrant
    moves a much smaller M (e.g. transient outage, partial mark
    failure). The Prometheus alert
    ``RetentionEvictionInconsistent`` triggers on the rate
    difference; this test asserts the underlying counter cells diverge
    correctly so the alert has a signal to fire on.
    """
    settings = _make_settings()
    graph = _make_graph_store(
        eligible_es=100,
        oldest=_NOW - timedelta(days=100),
        move_es=100,
        move_edges=0,
    )
    # Qdrant only marks 10 chunks — divergence of ~90 between the two
    # stores' eviction totals.
    vector = _make_vector_store(eligible_chunks=10, marked=10)

    before_neo4j = _counter_value(
        RETENTION_EVICTION_TOTAL,
        transition=TRANSITION_HOT_TO_WARM,
        store=STORE_LABEL_NEO4J,
    )
    before_qdrant = _counter_value(
        RETENTION_EVICTION_TOTAL,
        transition=TRANSITION_HOT_TO_WARM,
        store=STORE_LABEL_QDRANT,
    )
    worker = RetentionWorker(
        session_factory=_session_factory_for(_WS_A),
        graph_store=graph,
        vector_store=vector,
        settings=settings,
    )
    await worker.run_once(now=_NOW)
    after_neo4j = _counter_value(
        RETENTION_EVICTION_TOTAL,
        transition=TRANSITION_HOT_TO_WARM,
        store=STORE_LABEL_NEO4J,
    )
    after_qdrant = _counter_value(
        RETENTION_EVICTION_TOTAL,
        transition=TRANSITION_HOT_TO_WARM,
        store=STORE_LABEL_QDRANT,
    )

    delta_neo4j = after_neo4j - before_neo4j
    delta_qdrant = after_qdrant - before_qdrant
    # 100 vs 10 — the alert's `abs(... ) > 0.1` threshold is comfortably
    # tripped by this magnitude over a 1h rate window in production.
    assert delta_neo4j == pytest.approx(100.0)
    assert delta_qdrant == pytest.approx(10.0)
    assert abs(delta_neo4j - delta_qdrant) >= 80.0


# ---------------------------------------------------------------------------
# Test 8: per-workspace iteration writes the gauge for both workspaces
# ---------------------------------------------------------------------------


async def test_per_workspace_iteration_updates_gauge_for_each() -> None:
    """ADR-0009 §3: worker iterates workspaces sequentially.

    The gauge label set is global (no ``workspace_id`` label); each
    workspace's tick OVERWRITES the previous one's gauge value. Operators
    aggregate via PromQL ``sum() by (tier, store)``. This test asserts
    the worker calls the underlying counter for both workspaces — i.e.
    the per-workspace iteration is complete on every tick.
    """
    settings = _make_settings()
    graph = _make_graph_store(records={"hot": 100, "warm": 50})
    vector = _make_vector_store(by_tier={"hot": 20, "warm": 10})
    worker = RetentionWorker(
        session_factory=_session_factory_for(_WS_A, _WS_B),
        graph_store=graph,
        vector_store=vector,
        settings=settings,
    )
    await worker.run_once(now=_NOW)

    # Two workspaces → two calls each.
    assert graph.count_records_by_tier.await_count == 2
    assert vector.count_chunks_by_tier.await_count == 2
    # Both workspace_ids passed through (no leakage of one into the other).
    seen_ws = {call.kwargs["workspace_id"] for call in graph.count_records_by_tier.await_args_list}
    assert seen_ws == {_WS_A, _WS_B}
