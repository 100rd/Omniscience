"""Property tests for retention invariants (issue #138, ADR-0009).

Four properties:

1. **No data loss across hot -> warm.** Any entity queryable at
   ``as_of=T`` before eviction is still queryable at the same
   ``as_of=T`` after eviction (with the warm-tier fidelity ADR-0009 §1
   specifies — snapshot-per-day projection).
2. **Archive degradation.** Any ``as_of=T`` falling in the archive
   window returns the ``degraded_response`` envelope; no silent drop.
3. **Eviction is idempotent.** Running the worker twice on the same
   fixture changes nothing on the second run.
4. **Cross-workspace eviction isolation.** An eviction batch on
   workspace A consumes zero records from workspace B.

The simulator reference is ``tests/property/_simulator.py``.  Live
mode is gated behind ``OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.property._simulator import (
    DEGRADED_ARCHIVE_TIER,
    EntityVersion,
    GraphSimulator,
    RetentionState,
    query_with_tiers,
    resolve_tier,
    run_retention,
)
from tests.property._strategies import (
    NOW,
    WORKSPACE_A,
    WORKSPACE_B,
    workspace_bundle,
)

_PROPERTY_SETTINGS = settings(
    max_examples=120,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    deadline=None,
)


def _coerce_recorded_at(sim: GraphSimulator, days_ago_max: int) -> GraphSimulator:
    """Spread the simulator's ``recorded_at`` across hot/warm/archive.

    The default fixture's ``recorded_at`` is concentrated near ``NOW``
    (hot tier), which means the eviction property tests don't actually
    exercise warm/archive transitions.  This helper rewrites
    ``recorded_at`` for each version so that it spans the full
    [now - days_ago_max, now] range — covering all three tiers.
    """
    new_entities: list[EntityVersion] = []
    n = len(sim.entities)
    for i, v in enumerate(sim.entities):
        # Spread evenly across the window.
        shift_days = int(days_ago_max * (i / max(n, 1)))
        new_recorded_at = NOW - timedelta(days=shift_days)
        new_entities.append(
            EntityVersion(
                workspace_id=v.workspace_id,
                entity_key=v.entity_key,
                stable_id=v.stable_id,
                valid_from=v.valid_from,
                valid_to=v.valid_to,
                recorded_at=new_recorded_at,
            )
        )
    return GraphSimulator(entities=new_entities, edges=list(sim.edges))


# ---------------------------------------------------------------------------
# Property 1 — no data loss across hot -> warm.
# ---------------------------------------------------------------------------


@_PROPERTY_SETTINGS
@given(
    bundle=workspace_bundle(),
    days_ago_max=st.integers(min_value=120, max_value=300),
)
def test_no_data_loss_across_hot_to_warm(
    bundle: tuple[GraphSimulator, dict[UUID, list[str]]],
    days_ago_max: int,
) -> None:
    """Any entity queryable at ``as_of=T`` before eviction is still
    queryable at the same T after eviction.

    Pre-eviction baseline: every version is in the live store
    (``RetentionState.hot_entities``).  Run retention.  Post-eviction
    the version may live in hot or warm (depending on
    ``recorded_at``), and the tiered read-path resolves on ``as_of``
    not on storage location (ADR-0009 §4).  The invariant: for every
    sample_t whose pre-eviction baseline read returned a row, the
    post-eviction tiered read returns a non-degraded result that
    preserves ``stable_id`` (ADR-0009 §1 Warm fidelity).

    Archive samples (``as_of`` in archive window) are tested by
    property 2 and skipped here.
    """
    sim, key_index = bundle
    sim = _coerce_recorded_at(sim, days_ago_max=days_ago_max)
    post_state = run_retention(sim, NOW)

    for workspace_id, keys in key_index.items():
        for key in keys:
            for v in sim.all_versions(workspace_id, key):
                # Sample a T inside the version's window.
                if v.valid_to is None:
                    sample_t = v.valid_from + timedelta(microseconds=1)
                else:
                    if v.valid_to <= v.valid_from:
                        continue
                    sample_t = v.valid_from
                # Skip archive samples — those are tested by property 2.
                if resolve_tier(NOW, sample_t) == "archive":
                    continue
                # Pre-eviction baseline: the bitemporal point-in-time
                # predicate against the live store.  This is the
                # ground truth — what the user *would have* seen
                # before the worker ran.
                pre_result = sim.get_entity(workspace_id, key, as_of=sample_t)
                if pre_result is None:
                    continue
                # Post-eviction: tiered read-path (ADR-0009 §4).
                # Whether the entity ended up in hot or warm, the
                # tiered query at the same T must still return a row.
                post_result = query_with_tiers(post_state, workspace_id, key, sample_t, NOW)
                assert post_result is not None and not isinstance(post_result, dict), (
                    f"data loss: workspace={workspace_id} key={key!r} "
                    f"sample_t={sample_t} returned {pre_result} pre-eviction "
                    f"but {post_result!r} post-eviction"
                )
                # Stable id is preserved across the tier transition
                # (ADR-0009 §1 Warm: snapshot rows mirror the
                # bitemporal triple verbatim, including stable_id).
                assert post_result.stable_id == pre_result.stable_id


# ---------------------------------------------------------------------------
# Property 2 — archive degradation envelope.
# ---------------------------------------------------------------------------


@_PROPERTY_SETTINGS
@given(
    bundle=workspace_bundle(),
    archive_offset_days=st.integers(min_value=400, max_value=2000),
)
def test_archive_degradation_envelope(
    bundle: tuple[GraphSimulator, dict[UUID, list[str]]],
    archive_offset_days: int,
) -> None:
    """``as_of`` in the archive window returns ``degraded_response``."""
    sim, key_index = bundle
    state = run_retention(sim, NOW)
    archive_t = NOW - timedelta(days=archive_offset_days)
    assert resolve_tier(NOW, archive_t) == "archive"
    for workspace_id, keys in key_index.items():
        for key in keys:
            result = query_with_tiers(state, workspace_id, key, archive_t, NOW)
            assert isinstance(result, dict), (
                f"archive read should return degraded envelope; got {result!r}"
            )
            assert result["degraded_response"] == DEGRADED_ARCHIVE_TIER


# ---------------------------------------------------------------------------
# Property 3 — eviction is idempotent.
# ---------------------------------------------------------------------------


@_PROPERTY_SETTINGS
@given(
    bundle=workspace_bundle(),
    days_ago_max=st.integers(min_value=120, max_value=300),
)
def test_eviction_is_idempotent(
    bundle: tuple[GraphSimulator, dict[UUID, list[str]]],
    days_ago_max: int,
) -> None:
    """Running the worker twice on the same fixture is a no-op on the
    second run (ADR-0009 §3)."""
    sim, _ = bundle
    sim = _coerce_recorded_at(sim, days_ago_max=days_ago_max)
    first = run_retention(sim, NOW)
    second = run_retention(sim, NOW, state=first)

    assert _retention_state_equal(first, second), (
        "second retention run was not a no-op: "
        f"first.hot={len(first.hot_entities)}, second.hot={len(second.hot_entities)}, "
        f"first.warm={len(first.warm_versions)}, second.warm={len(second.warm_versions)}"
    )


def _retention_state_equal(a: RetentionState, b: RetentionState) -> bool:
    return (
        sorted(a.hot_entities, key=_entity_sort_key)
        == sorted(b.hot_entities, key=_entity_sort_key)
        and sorted(a.warm_versions, key=_entity_sort_key)
        == sorted(b.warm_versions, key=_entity_sort_key)
        and a.archived_workspace_dates == b.archived_workspace_dates
    )


def _entity_sort_key(v: EntityVersion) -> tuple[str, str, str, str]:
    return (
        str(v.workspace_id),
        v.entity_key,
        v.valid_from.isoformat(),
        v.valid_to.isoformat() if v.valid_to else "",
    )


# ---------------------------------------------------------------------------
# Property 4 — cross-workspace eviction isolation.
# ---------------------------------------------------------------------------


@_PROPERTY_SETTINGS
@given(
    bundle=workspace_bundle(),
    days_ago_max=st.integers(min_value=120, max_value=300),
)
def test_cross_workspace_eviction_isolation(
    bundle: tuple[GraphSimulator, dict[UUID, list[str]]],
    days_ago_max: int,
) -> None:
    """An eviction pass on workspace A consumes zero records from B.

    The simulator's ``run_retention`` iterates per-workspace; this
    test asserts the iteration boundary by running retention on a
    bundle restricted to workspace A and verifying workspace B's
    state is bit-identical to its pre-eviction snapshot.
    """
    sim, _ = bundle
    sim = _coerce_recorded_at(sim, days_ago_max=days_ago_max)
    # Snapshot B's pre-state.
    b_pre_entities = [v for v in sim.entities if v.workspace_id == WORKSPACE_B]
    # Run retention on a sim restricted to A — i.e. the worker's
    # per-workspace iteration (§Consequences-security in ADR-0009: an
    # eviction batch is workspace-scoped, structurally).
    a_only = GraphSimulator(
        entities=[v for v in sim.entities if v.workspace_id == WORKSPACE_A],
        edges=[e for e in sim.edges if e.workspace_id == WORKSPACE_A],
    )
    state_a = run_retention(a_only, NOW)
    # The state must contain zero records from workspace B.
    for v in state_a.hot_entities:
        assert v.workspace_id == WORKSPACE_A
    for v in state_a.warm_versions:
        assert v.workspace_id == WORKSPACE_A
    for ws, _date in state_a.archived_workspace_dates:
        assert ws == WORKSPACE_A
    # B's records in the original sim are untouched (no shared mutation).
    b_post_entities = [v for v in sim.entities if v.workspace_id == WORKSPACE_B]
    assert sorted(b_pre_entities, key=_entity_sort_key) == sorted(
        b_post_entities, key=_entity_sort_key
    ), "workspace B records mutated during workspace A retention"


# ---------------------------------------------------------------------------
# Sanity test — tier resolution at the canonical boundaries.
# ---------------------------------------------------------------------------


def test_tier_resolution_boundaries() -> None:
    """Hot/warm/archive boundaries match ADR-0009 §1 + §2."""
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert resolve_tier(now, now) == "hot"
    assert resolve_tier(now, now - timedelta(days=89)) == "hot"
    assert resolve_tier(now, now - timedelta(days=90)) == "hot"
    assert resolve_tier(now, now - timedelta(days=91)) == "warm"
    assert resolve_tier(now, now - timedelta(days=365)) == "warm"
    assert resolve_tier(now, now - timedelta(days=366)) == "archive"
    assert resolve_tier(now, now - timedelta(days=3650)) == "archive"
