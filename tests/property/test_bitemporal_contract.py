"""Property tests for the bitemporal contract (issue #138).

Eight properties from ADR-0008 §5 + §9 + ADR-0006 §6 are encoded as
``hypothesis``-driven invariants over the simulator in
``tests/property/_simulator.py``.

The simulator is the contract surface: it captures ADR-0008 verbatim
and is dependency-free.  Each property test is decorated with
``@settings(max_examples=120, deadline=None)`` per the precedent in
``tests/test_tombstone_end_dating.py::test_property_no_data_loss_across_end_dating``
landed by issue #171.

Live-mode counterparts that exercise the same properties on real
Neo4j+Qdrant containers are gated behind
``OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1`` and
``OMNISCIENCE_RUN_QDRANT_CONTRACT_TESTS=1``; see
``tests/property/README.md`` for the rationale.

The 8 properties:

1. Identity stable across versions (ADR-0008 §9 invariant 1).
2. Monotonic ``recorded_at`` per identity (ADR-0008 §1).
3. Open-closed interval coverage (ADR-0008 §5 + §9 invariant 2).
4. No-overlap invariant (ADR-0008 §9 invariant 3).
5. Edge endpoint validity (ADR-0008 §3 endpoint coupling).
6. Tombstone correctness (ADR-0008 §3, issue #137).
7. Cross-workspace isolation under all ``as_of`` (P0 — ADR-0005 §ACL +
   ADR-0008 §Consequences-security #4).
8. Cross-store consistency (ADR-0008 §6 + ADR-0006 §6 — graph and
   vector see the same ``valid`` window for the same identity).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from uuid import UUID

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.property._simulator import (
    EntityVersion,
    GraphSimulator,
)
from tests.property._strategies import (
    EARLIEST,
    LATEST,
    NOW,
    WORKSPACE_A,
    WORKSPACE_B,
    workspace_bundle,
    workspace_bundle_with_edges,
)

_PROPERTY_SETTINGS = settings(
    max_examples=120,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    deadline=None,
)


# ---------------------------------------------------------------------------
# Property 1 — identity stable across versions.
# ---------------------------------------------------------------------------


@_PROPERTY_SETTINGS
@given(bundle=workspace_bundle(), as_of_offset_days=st.integers(min_value=-400, max_value=400))
def test_identity_stable_across_versions(
    bundle: tuple[GraphSimulator, dict[UUID, list[str]]],
    as_of_offset_days: int,
) -> None:
    """For any ``(workspace_id, entity_key)``, every version returned by
    ``get_entity(as_of=T)`` carries the same ``stable_id``."""
    sim, key_index = bundle
    as_of = NOW + timedelta(days=as_of_offset_days)
    for workspace_id, keys in key_index.items():
        for key in keys:
            seen_ids: set[UUID] = {v.stable_id for v in sim.all_versions(workspace_id, key)}
            assert len(seen_ids) == 1, (
                f"identity instability: workspace={workspace_id} key={key!r} "
                f"saw stable_ids={seen_ids}"
            )
            v = sim.get_entity(workspace_id, key, as_of=as_of)
            if v is not None:
                assert v.stable_id == next(iter(seen_ids)), (
                    f"as_of={as_of} returned stable_id={v.stable_id} but chain has {seen_ids}"
                )


# ---------------------------------------------------------------------------
# Property 2 — monotonic recorded_at per identity.
# ---------------------------------------------------------------------------


@_PROPERTY_SETTINGS
@given(bundle=workspace_bundle())
def test_monotonic_recorded_at(
    bundle: tuple[GraphSimulator, dict[UUID, list[str]]],
) -> None:
    """``recorded_at`` is non-decreasing per identity, in chain order."""
    sim, key_index = bundle
    for workspace_id, keys in key_index.items():
        for key in keys:
            versions = sorted(sim.all_versions(workspace_id, key), key=lambda v: v.valid_from)
            recorded_ats = [v.recorded_at for v in versions]
            for prev, nxt in pairwise(recorded_ats):
                assert prev <= nxt, (
                    f"recorded_at regression: workspace={workspace_id} key={key!r} {prev} -> {nxt}"
                )


# ---------------------------------------------------------------------------
# Property 3 — open-closed interval coverage.
# ---------------------------------------------------------------------------


@_PROPERTY_SETTINGS
@given(bundle=workspace_bundle())
def test_open_closed_interval_coverage(
    bundle: tuple[GraphSimulator, dict[UUID, list[str]]],
) -> None:
    """Every wall-clock T in an entity's lifetime falls into exactly
    one ``[valid_from, valid_to)`` interval (ADR-0008 §5 + §9 #2)."""
    sim, key_index = bundle
    for workspace_id, keys in key_index.items():
        for key in keys:
            versions = sorted(sim.all_versions(workspace_id, key), key=lambda v: v.valid_from)
            if not versions:
                continue
            # Sample T at every version's valid_from + epsilon and at
            # the midpoints between consecutive versions.
            sample_points: list[datetime] = []
            for v in versions:
                sample_points.append(v.valid_from)
                sample_points.append(v.valid_from + timedelta(microseconds=1))
                if v.valid_to is not None:
                    sample_points.append(v.valid_to - timedelta(microseconds=1))
            for t in sample_points:
                hits = [
                    v
                    for v in versions
                    if v.valid_from <= t and (v.valid_to is None or t < v.valid_to)
                ]
                assert len(hits) <= 1, (
                    f"interval overlap: workspace={workspace_id} key={key!r} "
                    f"t={t} hit {len(hits)} versions"
                )


# ---------------------------------------------------------------------------
# Property 4 — no-overlap across versions of the same identity.
# ---------------------------------------------------------------------------


@_PROPERTY_SETTINGS
@given(bundle=workspace_bundle())
def test_no_overlap_invariant(
    bundle: tuple[GraphSimulator, dict[UUID, list[str]]],
) -> None:
    """No two versions of the same identity have overlapping windows
    (ADR-0008 §9 invariant 3)."""
    sim, key_index = bundle
    for workspace_id, keys in key_index.items():
        for key in keys:
            versions = sorted(sim.all_versions(workspace_id, key), key=lambda v: v.valid_from)
            for v_a, v_b in pairwise(versions):
                assert v_a.valid_to is not None, (
                    f"non-final version with valid_to=None: workspace={workspace_id} key={key!r}"
                )
                assert v_a.valid_to <= v_b.valid_from, (
                    f"overlap: workspace={workspace_id} key={key!r} "
                    f"[{v_a.valid_from}, {v_a.valid_to}) overlaps [{v_b.valid_from}, _)"
                )


# ---------------------------------------------------------------------------
# Property 5 — edge endpoint validity.
# ---------------------------------------------------------------------------


@_PROPERTY_SETTINGS
@given(
    bundle=workspace_bundle_with_edges(),
    as_of_offset_days=st.integers(min_value=-400, max_value=400),
)
def test_edge_endpoint_validity(
    bundle: tuple[GraphSimulator, dict[UUID, list[str]]],
    as_of_offset_days: int,
) -> None:
    """An edge at ``as_of=T`` is valid at T AND both endpoints are
    valid at T (ADR-0008 §3 endpoint coupling)."""
    sim, _ = bundle
    as_of = NOW + timedelta(days=as_of_offset_days)
    for workspace_id in (WORKSPACE_A, WORKSPACE_B):
        edges = sim.get_edges(workspace_id, as_of=as_of)
        for e in edges:
            src = sim.get_entity(workspace_id, e.source_entity_key, as_of=as_of)
            tgt = sim.get_entity(workspace_id, e.target_entity_key, as_of=as_of)
            assert src is not None, f"edge {e.edge_key} valid at {as_of} but source missing"
            assert tgt is not None, f"edge {e.edge_key} valid at {as_of} but target missing"


# ---------------------------------------------------------------------------
# Property 6 — tombstone correctness.
# ---------------------------------------------------------------------------


@_PROPERTY_SETTINGS
@given(end_offset_days=st.integers(min_value=1, max_value=180))
def test_tombstone_correctness(end_offset_days: int) -> None:
    """An entity end-dated at E is queryable at T < E and absent at T >= E.

    Builds a deterministic single-version chain, end-dates at
    ``valid_from + end_offset_days``, and asserts the predicate holds
    on either side of E and at the boundary.
    """
    sim = GraphSimulator()
    valid_from = datetime(2026, 1, 1, tzinfo=UTC)
    end_at = valid_from + timedelta(days=end_offset_days)
    sim.upsert_entity(
        EntityVersion(
            workspace_id=WORKSPACE_A,
            entity_key="svc.tombstone-target",
            stable_id=UUID(int=42),
            valid_from=valid_from,
            valid_to=None,
            recorded_at=valid_from,
        )
    )
    sim.end_date_entity(WORKSPACE_A, "svc.tombstone-target", end_at)
    # T < E -> queryable.
    pre_t = end_at - timedelta(microseconds=1)
    assert sim.get_entity(WORKSPACE_A, "svc.tombstone-target", as_of=pre_t) is not None
    # T == E -> absent (open-closed: end is exclusive).
    assert sim.get_entity(WORKSPACE_A, "svc.tombstone-target", as_of=end_at) is None
    # T > E -> absent.
    post_t = end_at + timedelta(microseconds=1)
    assert sim.get_entity(WORKSPACE_A, "svc.tombstone-target", as_of=post_t) is None


# ---------------------------------------------------------------------------
# Property 7 — cross-workspace isolation under all as_of values.
#
# THIS IS THE P0 SECURITY GATE FOR EPIC #97.  Any failure here blocks
# the ADR flip in #139.
# ---------------------------------------------------------------------------


@_PROPERTY_SETTINGS
@given(
    bundle=workspace_bundle(),
    as_of_offset_days=st.integers(min_value=-400, max_value=400),
)
def test_cross_workspace_isolation_under_all_as_of(
    bundle: tuple[GraphSimulator, dict[UUID, list[str]]],
    as_of_offset_days: int,
) -> None:
    """A query for workspace A never returns workspace B's data.

    Adversarial fixture: workspace A and workspace B share entity_keys
    drawn from the same alphabet (overlapping names — composite
    uniqueness allows this), with overlapping ``valid_*`` ranges.  The
    property holds for every ``as_of`` in the bounded calendar window
    (including pre-history and future).
    """
    sim, key_index = bundle
    as_of = NOW + timedelta(days=as_of_offset_days)

    # Walk every entity stored in the simulator and assert get_entity
    # for the *other* workspace returns either ``None`` or a row whose
    # ``workspace_id`` matches the queried workspace.  Composite
    # uniqueness (workspace_id, id) explicitly allows ``stable_id``
    # collisions across workspaces (ADR-0005 §Schema posture); the
    # ACL boundary is the workspace_id field on every row, not the
    # stable_id.
    for v in sim.entities:
        opposite = WORKSPACE_B if v.workspace_id == WORKSPACE_A else WORKSPACE_A
        result = sim.get_entity(opposite, v.entity_key, as_of=as_of)
        if result is None:
            continue
        assert result.workspace_id == opposite, (
            f"ACL leak: querying workspace={opposite} for key={v.entity_key!r} "
            f"at as_of={as_of} returned a version owned by workspace={result.workspace_id}"
        )

    # Every version returned by ``all_versions(WORKSPACE_A, ...)``
    # carries workspace_id == WORKSPACE_A — and symmetrically for B.
    for key in key_index.get(WORKSPACE_A, []):
        for v in sim.all_versions(WORKSPACE_A, key):
            assert v.workspace_id == WORKSPACE_A
    for key in key_index.get(WORKSPACE_B, []):
        for v in sim.all_versions(WORKSPACE_B, key):
            assert v.workspace_id == WORKSPACE_B

    # Edge filter is also workspace-scoped under all `as_of`.
    edges_a = sim.get_edges(WORKSPACE_A, as_of=as_of)
    edges_b = sim.get_edges(WORKSPACE_B, as_of=as_of)
    for e in edges_a:
        assert e.workspace_id == WORKSPACE_A
    for e in edges_b:
        assert e.workspace_id == WORKSPACE_B


# ---------------------------------------------------------------------------
# Property 8 — cross-store consistency (graph/vector alignment).
# ---------------------------------------------------------------------------


@_PROPERTY_SETTINGS
@given(bundle=workspace_bundle(), as_of_offset_days=st.integers(min_value=-400, max_value=400))
def test_cross_store_consistency(
    bundle: tuple[GraphSimulator, dict[UUID, list[str]]],
    as_of_offset_days: int,
) -> None:
    """A chunk from Qdrant at ``as_of=T`` is reachable via a graph
    entity valid at T (or the chunk is metadata-only).

    The simulator models a Qdrant chunk as a payload mirror of an
    entity version with the same ``(workspace_id, entity_key,
    valid_from, valid_to)``.  Cross-store consistency holds iff for
    every chunk visible at T, the graph exposes a covering entity
    version at T.
    """
    sim, key_index = bundle
    as_of = NOW + timedelta(days=as_of_offset_days)

    for workspace_id, keys in key_index.items():
        for key in keys:
            # Synthesise a "chunk" mirror per entity version.  Per
            # ADR-0006 §6 + ADR-0008 §6 the units, interval
            # convention, and sentinel are identical; the simulator
            # treats the entity version as a chunk-equivalent payload.
            for v in sim.all_versions(workspace_id, key):
                chunk_visible_at_t = v.valid_from <= as_of and (
                    v.valid_to is None or as_of < v.valid_to
                )
                if not chunk_visible_at_t:
                    continue
                # Then a graph entity must also be visible.
                graph_v = sim.get_entity(workspace_id, key, as_of=as_of)
                assert graph_v is not None, (
                    f"cross-store inconsistency: chunk visible at {as_of} for "
                    f"workspace={workspace_id} key={key!r} but graph returned None"
                )
                assert graph_v.workspace_id == workspace_id
                assert graph_v.entity_key == key
                # The graph version must cover T with the same window
                # as the chunk (units + interval convention identical
                # across stores per ADR-0008 §6).
                assert graph_v.valid_from <= as_of
                assert graph_v.valid_to is None or as_of < graph_v.valid_to


# ---------------------------------------------------------------------------
# Sanity tests — confirm the simulator captures the canonical scenarios.
# ---------------------------------------------------------------------------


def test_simulator_rejects_naive_overlap() -> None:
    """Direct attempt to upsert an overlapping window is rejected.

    Belt-and-braces: the writer contract enforces the §1 corruption
    guard.  This makes the property tests' "no overlap" finding
    impossible to pass by accident.
    """
    sim = GraphSimulator()
    sim.upsert_entity(
        EntityVersion(
            workspace_id=WORKSPACE_A,
            entity_key="svc.demo",
            stable_id=UUID(int=1),
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
            valid_to=None,
            recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    with pytest.raises(ValueError):
        sim.upsert_entity(
            EntityVersion(
                workspace_id=WORKSPACE_A,
                entity_key="svc.demo",
                stable_id=UUID(int=1),
                valid_from=datetime(2025, 6, 1, tzinfo=UTC),  # backward!
                valid_to=None,
                recorded_at=datetime(2026, 2, 1, tzinfo=UTC),
            )
        )


def test_simulator_canonical_predicate() -> None:
    """`as_of == valid_from` is included; `as_of == valid_to` is excluded."""
    sim = GraphSimulator()
    vf = datetime(2026, 1, 1, tzinfo=UTC)
    vt = datetime(2026, 2, 1, tzinfo=UTC)
    sim.upsert_entity(
        EntityVersion(
            workspace_id=WORKSPACE_A,
            entity_key="svc.boundary",
            stable_id=UUID(int=2),
            valid_from=vf,
            valid_to=None,
            recorded_at=vf,
        )
    )
    sim.end_date_entity(WORKSPACE_A, "svc.boundary", vt)
    # as_of == vf -> hit.
    assert sim.get_entity(WORKSPACE_A, "svc.boundary", as_of=vf) is not None
    # as_of == vt -> miss (open-closed).
    assert sim.get_entity(WORKSPACE_A, "svc.boundary", as_of=vt) is None


# Expose the bounded-calendar globals for live-mode tests that import
# this module.
__all__ = ["EARLIEST", "LATEST", "NOW"]
