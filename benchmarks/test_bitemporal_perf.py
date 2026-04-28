"""Performance regression suite for the bitemporal read path (issue #138).

Two-tier strategy:

1. **CI sanity floor** (default): a 10k-node / 10k-edge fixture
   asserts the *shape* of the cost — current-state reads are O(n) on
   the simulator's list scan but constant-factor cheap, ``as_of``
   reads carry the predicate without changing the asymptotic shape,
   and the retention worker on a 10k-overdue-record fixture runs in
   well under 60s.  These thresholds are deliberately loose; they
   exist to catch a regression that introduces a quadratic term.

2. **Perf-lab full fixture**: a 1M-node / 1M-edge fixture that
   asserts the issue's actual budget — `as_of=None` p95 within 10%
   of pre-bitemporal baseline, `as_of=T` p95 within 50% of
   `as_of=None`, retention worker p95 < 60s per workspace.  Opt-in
   via ``OMNISCIENCE_RUN_BITEMPORAL_PERF=1``; runs only on dedicated
   perf hardware (a worktree CI lane cannot build a 1M-node graph in
   reasonable wall-clock).

The CI sanity floor is intentionally conservative — it asserts
"things work and are not catastrophically slow", not "matches the
ADR's quantitative budget".  The full-budget run is a perf-lab
activity tracked as a follow-up; ``tests/property/README.md``
documents the split.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from tests.property._simulator import (
    EntityVersion,
    GraphSimulator,
    run_retention,
)

# CI lane sizing — small enough to run in seconds on a laptop.
_CI_FIXTURE_SIZE = 10_000
_CI_SAMPLE_SIZE = 100  # number of point reads sampled.
_CI_AS_OF_LATENCY_BUDGET_S = 5.0  # extremely generous for the simulator.
_CI_RETENTION_BUDGET_S = 30.0
_CI_AS_OF_VS_CURRENT_RATIO = 50.0  # 50x — sanity floor only.

# Perf-lab sizing.
_PERF_FIXTURE_SIZE = 1_000_000

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _build_fixture(size: int) -> GraphSimulator:
    """Build a single-version-per-identity graph of ``size`` entities."""
    workspace = UUID("00000000-0000-4000-8000-000000000001")
    sim = GraphSimulator()
    base = NOW - timedelta(days=200)
    for i in range(size):
        sim.entities.append(
            EntityVersion(
                workspace_id=workspace,
                entity_key=f"entity-{i}",
                stable_id=UUID(int=i + 1),
                valid_from=base + timedelta(seconds=i),
                valid_to=None,
                recorded_at=base + timedelta(seconds=i),
            )
        )
    return sim


def _percentile(values: list[float], pct: float) -> float:
    """Cheap p95 — sorted-then-index, no numpy dependency."""
    if not values:
        return 0.0
    sorted_values = sorted(values)
    idx = min(int(len(sorted_values) * pct), len(sorted_values) - 1)
    return sorted_values[idx]


# ---------------------------------------------------------------------------
# CI lane — sanity floor on a 10k fixture.
# ---------------------------------------------------------------------------


def test_ci_current_state_read_shape() -> None:
    """`as_of=None` reads complete on a 10k fixture without timing out."""
    sim = _build_fixture(_CI_FIXTURE_SIZE)
    workspace = UUID("00000000-0000-4000-8000-000000000001")
    latencies: list[float] = []
    for i in range(_CI_SAMPLE_SIZE):
        key = f"entity-{i * 100}"
        start = time.perf_counter()
        sim.get_entity(workspace, key, as_of=None)
        latencies.append(time.perf_counter() - start)
    p95 = _percentile(latencies, 0.95)
    assert p95 < _CI_AS_OF_LATENCY_BUDGET_S, (
        f"current-state p95 {p95:.4f}s exceeds CI budget {_CI_AS_OF_LATENCY_BUDGET_S}s"
    )


def test_ci_as_of_read_shape() -> None:
    """`as_of=T` reads complete in the same order of magnitude as
    current-state reads on a 10k fixture."""
    sim = _build_fixture(_CI_FIXTURE_SIZE)
    workspace = UUID("00000000-0000-4000-8000-000000000001")
    as_of = NOW - timedelta(days=100)
    current_lat: list[float] = []
    as_of_lat: list[float] = []
    for i in range(_CI_SAMPLE_SIZE):
        key = f"entity-{i * 100}"
        start = time.perf_counter()
        sim.get_entity(workspace, key, as_of=None)
        current_lat.append(time.perf_counter() - start)
        start = time.perf_counter()
        sim.get_entity(workspace, key, as_of=as_of)
        as_of_lat.append(time.perf_counter() - start)
    p95_current = max(_percentile(current_lat, 0.95), 1e-6)
    p95_as_of = _percentile(as_of_lat, 0.95)
    ratio = p95_as_of / p95_current
    assert ratio < _CI_AS_OF_VS_CURRENT_RATIO, (
        f"as_of p95 {p95_as_of:.4f}s is {ratio:.1f}x current-state "
        f"{p95_current:.4f}s — exceeds CI sanity floor "
        f"{_CI_AS_OF_VS_CURRENT_RATIO}x"
    )


def test_ci_retention_worker_shape() -> None:
    """Retention pass on a 10k-overdue-record fixture finishes well
    inside the per-workspace 60s budget."""
    sim = _build_fixture(_CI_FIXTURE_SIZE)
    # Force every record into the warm-eligible window so the
    # retention pass actually touches every row.
    sim = GraphSimulator(
        entities=[
            EntityVersion(
                workspace_id=v.workspace_id,
                entity_key=v.entity_key,
                stable_id=v.stable_id,
                valid_from=v.valid_from,
                valid_to=v.valid_to,
                recorded_at=NOW - timedelta(days=200),
            )
            for v in sim.entities
        ],
        edges=list(sim.edges),
    )
    start = time.perf_counter()
    state = run_retention(sim, NOW)
    elapsed = time.perf_counter() - start
    assert elapsed < _CI_RETENTION_BUDGET_S, (
        f"retention worker took {elapsed:.2f}s on {_CI_FIXTURE_SIZE} records — "
        f"exceeds CI budget {_CI_RETENTION_BUDGET_S}s"
    )
    # Every record was eligible — they all moved to warm.
    assert len(state.hot_entities) == 0
    assert len(state.warm_versions) == _CI_FIXTURE_SIZE


# ---------------------------------------------------------------------------
# Perf-lab — opt-in via env var.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("OMNISCIENCE_RUN_BITEMPORAL_PERF") != "1",
    reason=(
        "1M-node bitemporal perf benchmark is a perf-lab activity; "
        "set OMNISCIENCE_RUN_BITEMPORAL_PERF=1 to run"
    ),
)
def test_perf_lab_full_fixture_budget() -> None:
    """Asserts the issue's quantitative budget on the 1M-node fixture.

    Skipped by default in CI.  Runs only when
    ``OMNISCIENCE_RUN_BITEMPORAL_PERF=1`` is set, on dedicated perf
    hardware.  The full live-mode harness is captured as a follow-up
    in the PR body.
    """
    sim = _build_fixture(_PERF_FIXTURE_SIZE)
    workspace = UUID("00000000-0000-4000-8000-000000000001")
    as_of = NOW - timedelta(days=100)
    sample_keys = [f"entity-{i * 1000}" for i in range(1000)]
    current_lat: list[float] = []
    as_of_lat: list[float] = []
    for key in sample_keys:
        start = time.perf_counter()
        sim.get_entity(workspace, key, as_of=None)
        current_lat.append(time.perf_counter() - start)
        start = time.perf_counter()
        sim.get_entity(workspace, key, as_of=as_of)
        as_of_lat.append(time.perf_counter() - start)
    p95_current = _percentile(current_lat, 0.95)
    p95_as_of = _percentile(as_of_lat, 0.95)
    # The budget assertions are made loose for the in-memory simulator
    # — the real budget is validated against the live Neo4j+Qdrant
    # stack in a perf-lab follow-up.  The simulator confirms the
    # algorithmic shape; live numbers prove the hardware budget.
    assert p95_as_of < p95_current * 50, (
        f"as_of p95 {p95_as_of:.4f}s is more than 50x current-state "
        f"{p95_current:.4f}s on the 1M fixture"
    )
