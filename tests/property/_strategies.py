"""Hypothesis strategies for the bitemporal property tests.

The strategies are deliberately adversarial:

* Multi-workspace fixtures with overlapping ``entity_key`` values across
  workspaces (composite uniqueness allows it).
* Overlapping ``valid_*`` ranges across workspaces.
* Per-workspace version chains with strictly-monotonic ``valid_from``.
* End-dated identities at random points in the chain.

The shared shape is :func:`workspace_bundle` — a single fixture that
emits both workspaces' chains plus the global ``now`` for tier
resolution.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from hypothesis import strategies as st

from tests.property._simulator import EdgeVersion, EntityVersion, GraphSimulator

# ---------------------------------------------------------------------------
# Bounded fixed point in the calendar — keeps shrinks deterministic.
# ---------------------------------------------------------------------------

NOW: datetime = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
EARLIEST: datetime = NOW - timedelta(days=2 * 365)  # 2 years pre-now
LATEST: datetime = NOW + timedelta(days=30)  # tolerate near-future as_of


# Two fixed workspace UUIDs — fixed because composite-uniqueness must
# survive overlapping `entity_key` values across the same two ids.
WORKSPACE_A: UUID = UUID("11111111-1111-4111-8111-111111111111")
WORKSPACE_B: UUID = UUID("22222222-2222-4222-8222-222222222222")


def _utc_datetime() -> st.SearchStrategy[datetime]:
    """A UTC datetime in a bounded calendar window."""
    return st.datetimes(
        min_value=EARLIEST.replace(tzinfo=None),
        max_value=LATEST.replace(tzinfo=None),
    ).map(lambda d: d.replace(tzinfo=UTC))


@st.composite
def _entity_chain(
    draw: st.DrawFn,
    *,
    workspace_id: UUID,
    entity_key: str,
) -> tuple[list[EntityVersion], datetime | None]:
    """A monotonic chain of entity versions for one identity.

    Returns the chain plus an optional end-date timestamp (for the
    tombstone-correctness property).  ``stable_id`` is invariant across
    the chain (ADR-0008 §9 invariant 1).
    """
    n = draw(st.integers(min_value=1, max_value=4))
    stable_id = UUID(int=draw(st.integers(min_value=1, max_value=2**63 - 1)))
    base = draw(_utc_datetime())
    chain: list[EntityVersion] = []
    cur_recorded = base
    cur_valid_from = base
    for i in range(n):
        # Strict monotonic advance.
        gap_days = draw(st.integers(min_value=1, max_value=30))
        if i > 0:
            cur_valid_from = cur_valid_from + timedelta(days=gap_days)
            cur_recorded = max(cur_recorded, cur_valid_from)
        chain.append(
            EntityVersion(
                workspace_id=workspace_id,
                entity_key=entity_key,
                stable_id=stable_id,
                valid_from=cur_valid_from,
                valid_to=None,  # closed by next loop iteration.
                recorded_at=cur_recorded,
            )
        )
    # End-date a chain ~30% of the time for the tombstone property.
    end_date: datetime | None = None
    if draw(st.booleans()) and draw(st.booleans()):
        last = chain[-1]
        end_offset = draw(st.integers(min_value=1, max_value=60))
        end_date = last.valid_from + timedelta(days=end_offset)
    return chain, end_date


@st.composite
def _workspace_chains(
    draw: st.DrawFn,
    *,
    workspace_id: UUID,
) -> list[tuple[list[EntityVersion], datetime | None]]:
    """A workspace's worth of entity chains.

    Generates 1-3 distinct ``entity_key`` values per workspace.  The
    keys are drawn from a small alphabet so the cross-workspace
    isolation property test exercises overlapping names: the
    *intersection* of A's keys and B's keys is non-empty by
    construction with high probability.
    """
    keys = draw(
        st.lists(
            st.sampled_from(["alpha", "beta", "gamma", "delta"]),
            min_size=1,
            max_size=3,
            unique=True,
        )
    )
    return [draw(_entity_chain(workspace_id=workspace_id, entity_key=k)) for k in keys]


@st.composite
def workspace_bundle(
    draw: st.DrawFn,
) -> tuple[GraphSimulator, dict[UUID, list[str]]]:
    """A two-workspace simulator populated via the writer contract.

    Returns:
        A pair ``(sim, key_index)`` where ``key_index`` maps each
        workspace UUID to its list of ``entity_key`` values.  The
        property tests use the index to know what to query without
        leaking workspace-A keys into workspace-B queries (or vice
        versa) by accident.
    """
    sim = GraphSimulator()
    key_index: dict[UUID, list[str]] = {}
    for workspace_id in (WORKSPACE_A, WORKSPACE_B):
        chains = draw(_workspace_chains(workspace_id=workspace_id))
        for chain, end_date in chains:
            for v in chain:
                sim.upsert_entity(v)
            if end_date is not None:
                # Only end-date if the result is structurally valid
                # (i.e. end_date > current open window's valid_from).
                last = chain[-1]
                if end_date > last.valid_from:
                    sim.end_date_entity(workspace_id, last.entity_key, end_date)
            key_index.setdefault(workspace_id, []).append(chain[0].entity_key)
    return sim, key_index


@st.composite
def workspace_bundle_with_edges(
    draw: st.DrawFn,
) -> tuple[GraphSimulator, dict[UUID, list[str]]]:
    """A workspace_bundle that also has edges between entities.

    Used for the edge-endpoint-validity property.  Edges are added
    only when at least two entities exist in the same workspace.
    """
    sim, key_index = draw(workspace_bundle())
    for workspace_id, keys in key_index.items():
        if len(keys) < 2:
            continue
        # Pick one source/target pair per workspace.
        src_key = keys[0]
        tgt_key = keys[1]
        src = sim.get_entity(workspace_id, src_key)
        tgt = sim.get_entity(workspace_id, tgt_key)
        if src is None or tgt is None:
            continue
        # Choose an edge window inside the intersection of source and
        # target validity windows.
        wf = max(src.valid_from, tgt.valid_from)
        # Cap valid_to at the earliest endpoint close (or NULL if both
        # are still open).
        candidates_to: list[datetime] = []
        if src.valid_to is not None:
            candidates_to.append(src.valid_to)
        if tgt.valid_to is not None:
            candidates_to.append(tgt.valid_to)
        wt = min(candidates_to) if candidates_to else None
        if wt is not None and wf >= wt:
            continue  # impossible window — skip silently.
        edge = EdgeVersion(
            workspace_id=workspace_id,
            edge_key=f"{src_key}->{tgt_key}",
            source_entity_key=src_key,
            target_entity_key=tgt_key,
            valid_from=wf,
            valid_to=wt,
            recorded_at=max(src.recorded_at, tgt.recorded_at),
        )
        sim.upsert_edge(edge)
    return sim, key_index


__all__ = [
    "EARLIEST",
    "LATEST",
    "NOW",
    "WORKSPACE_A",
    "WORKSPACE_B",
    "workspace_bundle",
    "workspace_bundle_with_edges",
]
