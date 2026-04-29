"""Pure-Python simulator for the bitemporal + retention contract.

This module captures the contracts laid out in:

* ADR-0008 §1 (property semantics: open-closed `[valid_from, valid_to)`,
  ISO-8601 UTC, microsecond resolution).
* ADR-0008 §2 (identity vs. version shape: `(workspace_id, id)` is stable
  across versions, `:EntityState` carries per-version snapshots).
* ADR-0008 §3 (edge bitemporal shape + endpoint-validity coupling).
* ADR-0008 §5 (canonical `as_of` predicate).
* ADR-0008 §9 (identity stability invariant — exactly one row per
  `(workspace_id, id, T)`).
* ADR-0009 §1 (tier shapes: hot full fidelity, warm snapshot-per-day,
  archive Parquet with `degraded_response` envelope).
* ADR-0009 §2 (per-version eviction, edge end-dating does NOT trigger
  eviction).
* ADR-0009 §4 (read-path behaviour across tiers).

The simulator is the property-test surface: it deliberately encodes the
contract verbatim so the property tests can exercise it with random
fixtures.  The integration tests in #138 prove the simulator matches
real Neo4j+Qdrant behaviour (live mode, opt-in via env vars).

Nothing in this module imports the live adapters — the simulator is a
self-contained dependency-free reference implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

# ---------------------------------------------------------------------------
# Tier boundaries and degraded-response codes (mirrored from ADR-0009).
# ---------------------------------------------------------------------------

#: Hot tier upper boundary — entities with `recorded_at` newer than
#: ``now - HOT_WINDOW_DAYS`` live in the live Neo4j store at full
#: fidelity (ADR-0009 §1 Hot).
HOT_WINDOW_DAYS: Final[int] = 90

#: Warm tier upper boundary — entities with `recorded_at` between
#: ``now - WARM_WINDOW_DAYS`` and ``now - HOT_WINDOW_DAYS`` live in the
#: same database under a discriminator label, materialised one
#: snapshot-per-UTC-day (ADR-0009 §1 Warm).
WARM_WINDOW_DAYS: Final[int] = 365

#: Code emitted when ``as_of`` falls in the archive window.  Mirrors
#: ADR-0009 §4's example envelope.  The production code path will emit
#: this code from the retention/REST layer once the archive surface
#: lands; the simulator names it canonically here so the property tests
#: assert on a stable string.
DEGRADED_ARCHIVE_TIER: Final[str] = "as_of_in_archive_tier"

#: Code emitted when ``as_of`` precedes any recorded history.  Matches
#: the production constant ``apps.server.omniscience_server.as_of.DEGRADED_PRE_HISTORY``.
DEGRADED_PRE_HISTORY: Final[str] = "as_of_before_recorded_history"


# ---------------------------------------------------------------------------
# Domain shapes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntityVersion:
    """A single (:EntityState) row in the bitemporal chain.

    Fields mirror ADR-0008 §2 + §1.  ``valid_to=None`` is the
    still-valid sentinel; ``recorded_at`` is monotonic non-decreasing
    per ``(workspace_id, entity_key)`` (ADR-0008 §1).
    """

    workspace_id: UUID
    entity_key: str
    stable_id: UUID
    valid_from: datetime
    valid_to: datetime | None
    recorded_at: datetime


@dataclass(frozen=True)
class EdgeVersion:
    """A single relationship row in the bitemporal chain (ADR-0008 §3)."""

    workspace_id: UUID
    edge_key: str  # logical key, e.g. (source_id, target_id, edge_type)
    source_entity_key: str
    target_entity_key: str
    valid_from: datetime
    valid_to: datetime | None
    recorded_at: datetime


@dataclass
class GraphSimulator:
    """In-memory implementation of the ADR-0008 read + write contract.

    Every method enforces the workspace-scoped predicate from ADR-0005
    §ACL carry-forward — there is no method that can return data from a
    workspace other than the one supplied.  The cross-workspace
    isolation property test exercises this exhaustively.
    """

    entities: list[EntityVersion] = field(default_factory=list)
    edges: list[EdgeVersion] = field(default_factory=list)

    # ----- write API (mirrors neo4j_store.upsert_graph contract) -----

    def upsert_entity(self, version: EntityVersion) -> None:
        """Append a new version, end-dating the previous open one.

        Mirrors ADR-0008 §2's writer contract: a state-change creates a
        new ``:EntityState`` and end-dates the previous one's
        ``valid_to`` to the new ``valid_from``.  Monotonic
        ``recorded_at`` per identity is enforced (§1).
        """
        previous = self._open_version(version.workspace_id, version.entity_key)
        if previous is not None:
            if version.recorded_at < previous.recorded_at:
                raise ValueError(
                    f"recorded_at must be monotonic non-decreasing "
                    f"per (workspace_id, entity_key): got {version.recorded_at} "
                    f"after {previous.recorded_at}"
                )
            if version.stable_id != previous.stable_id:
                raise ValueError(
                    "stable_id must be invariant across versions of the same "
                    f"(workspace_id, entity_key); got {version.stable_id} "
                    f"after {previous.stable_id}"
                )
            if version.valid_from <= previous.valid_from:
                raise ValueError(
                    "valid_from must strictly advance across versions; "
                    f"got {version.valid_from} after {previous.valid_from}"
                )
            self.entities.remove(previous)
            self.entities.append(
                EntityVersion(
                    workspace_id=previous.workspace_id,
                    entity_key=previous.entity_key,
                    stable_id=previous.stable_id,
                    valid_from=previous.valid_from,
                    valid_to=version.valid_from,
                    recorded_at=previous.recorded_at,
                )
            )
        if version.valid_to is not None and version.valid_from >= version.valid_to:
            raise ValueError(
                "valid_from must be strictly less than valid_to; "
                f"got [{version.valid_from}, {version.valid_to})"
            )
        self.entities.append(version)

    def end_date_entity(
        self,
        workspace_id: UUID,
        entity_key: str,
        end_at: datetime,
    ) -> None:
        """Tombstone an entity by end-dating the open version (ADR-0008 §3, #137)."""
        current = self._open_version(workspace_id, entity_key)
        if current is None:
            return
        if end_at <= current.valid_from:
            raise ValueError(
                "end_at must be strictly greater than valid_from; "
                f"got end_at={end_at}, valid_from={current.valid_from}"
            )
        self.entities.remove(current)
        self.entities.append(
            EntityVersion(
                workspace_id=current.workspace_id,
                entity_key=current.entity_key,
                stable_id=current.stable_id,
                valid_from=current.valid_from,
                valid_to=end_at,
                recorded_at=current.recorded_at,
            )
        )

    def upsert_edge(self, edge: EdgeVersion) -> None:
        """Append an edge version, enforcing ADR-0008 §3 endpoint coupling.

        ``edge.valid_from >= max(source.valid_from, target.valid_from)``
        and ``edge.valid_to <= min(source.valid_to, target.valid_to)``
        with NULL = +inf.
        """
        if edge.valid_to is not None and edge.valid_from >= edge.valid_to:
            raise ValueError(
                "edge valid_from must be strictly less than valid_to; "
                f"got [{edge.valid_from}, {edge.valid_to})"
            )
        # Endpoint validity coupling.  We require that the edge window
        # is contained in the union of windows of each endpoint.
        if not self._endpoint_window_contains(
            workspace_id=edge.workspace_id,
            entity_key=edge.source_entity_key,
            window=(edge.valid_from, edge.valid_to),
        ):
            raise ValueError(
                "edge.valid_from..valid_to is not contained in any source-endpoint window"
            )
        if not self._endpoint_window_contains(
            workspace_id=edge.workspace_id,
            entity_key=edge.target_entity_key,
            window=(edge.valid_from, edge.valid_to),
        ):
            raise ValueError(
                "edge.valid_from..valid_to is not contained in any target-endpoint window"
            )
        self.edges.append(edge)

    # ----- read API (mirrors neo4j_store.get_entity / find_related) -----

    def get_entity(
        self,
        workspace_id: UUID,
        entity_key: str,
        as_of: datetime | None = None,
    ) -> EntityVersion | None:
        """Point-in-time entity read (ADR-0008 §5 canonical predicate)."""
        for v in self.entities:
            if v.workspace_id != workspace_id:
                continue
            if v.entity_key != entity_key:
                continue
            if as_of is None:
                if v.valid_to is None:
                    return v
                continue
            if v.valid_from <= as_of and (v.valid_to is None or as_of < v.valid_to):
                return v
        return None

    def get_edges(
        self,
        workspace_id: UUID,
        as_of: datetime | None = None,
    ) -> list[EdgeVersion]:
        """All edges visible at ``as_of`` (or current) for the workspace."""
        out: list[EdgeVersion] = []
        for e in self.edges:
            if e.workspace_id != workspace_id:
                continue
            if as_of is None:
                if e.valid_to is None:
                    out.append(e)
                continue
            if e.valid_from <= as_of and (e.valid_to is None or as_of < e.valid_to):
                out.append(e)
        return out

    def all_versions(
        self,
        workspace_id: UUID,
        entity_key: str,
    ) -> list[EntityVersion]:
        """Every version of a given identity in a workspace."""
        return [
            v
            for v in self.entities
            if v.workspace_id == workspace_id and v.entity_key == entity_key
        ]

    def stable_id(self, workspace_id: UUID, entity_key: str) -> UUID | None:
        """Return the workspace-scoped stable id for an identity, if any."""
        for v in self.entities:
            if v.workspace_id == workspace_id and v.entity_key == entity_key:
                return v.stable_id
        return None

    # ----- helpers -----

    def _open_version(
        self,
        workspace_id: UUID,
        entity_key: str,
    ) -> EntityVersion | None:
        for v in self.entities:
            if v.workspace_id != workspace_id:
                continue
            if v.entity_key != entity_key:
                continue
            if v.valid_to is None:
                return v
        return None

    def _endpoint_window_contains(
        self,
        workspace_id: UUID,
        entity_key: str,
        window: tuple[datetime, datetime | None],
    ) -> bool:
        """Whether some endpoint version covers the entire ``window``."""
        wf, wt = window
        for v in self.entities:
            if v.workspace_id != workspace_id:
                continue
            if v.entity_key != entity_key:
                continue
            if v.valid_from > wf:
                continue
            if v.valid_to is None:
                return True
            if wt is not None and wt <= v.valid_to:
                return True
        return False


# ---------------------------------------------------------------------------
# Tier resolution (ADR-0009 §1 + §4).
# ---------------------------------------------------------------------------


def resolve_tier(now: datetime, as_of: datetime) -> str:
    """Map ``as_of`` to ``'hot'``, ``'warm'``, or ``'archive'``.

    Mirrors ADR-0009 §1 boundary semantics: the boundary is inclusive
    on the older side ("90+ days old goes to warm").
    """
    age = now - as_of
    if age <= timedelta(days=HOT_WINDOW_DAYS):
        return "hot"
    if age <= timedelta(days=WARM_WINDOW_DAYS):
        return "warm"
    return "archive"


def degraded_response_for(now: datetime, as_of: datetime) -> dict[str, str] | None:
    """Return the ``meta`` envelope ADR-0009 §4 emits for archive reads."""
    if resolve_tier(now, as_of) == "archive":
        return {"degraded_response": DEGRADED_ARCHIVE_TIER}
    return None


# ---------------------------------------------------------------------------
# Retention worker simulator (ADR-0009 §2 + §3).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WarmSnapshotRow:
    """One row in the warm tier's snapshot-per-day projection.

    Mirrors ADR-0009 §1 Warm: ``(workspace_id, entity_id,
    snapshot_date, valid_from, valid_to, recorded_at_at_snapshot, ...)``.

    Warm rows are produced on-read from the underlying eligible
    version (rather than persisted per-day) — the simulator collapses
    the storage shape to keep the property tests' fixture size
    bounded.  The contract surface is identical: a warm-tier
    ``as_of`` read returns a ``WarmSnapshotRow`` with day-precision
    ``snapshot_date``, mirrored ``valid_from`` / ``valid_to`` from
    the source version, and the ``recorded_at_at_snapshot`` of the
    eligible version.
    """

    workspace_id: UUID
    entity_key: str
    stable_id: UUID
    snapshot_date: datetime  # UTC start-of-day
    valid_from: datetime
    valid_to: datetime | None
    recorded_at_at_snapshot: datetime


@dataclass
class RetentionState:
    """Simulator state after one or more retention runs.

    Warm storage holds **eligible source versions** — the snapshot
    rows are materialised on-read by ``query_with_tiers`` when a
    warm-tier ``as_of`` lands on a covered day.  This is functionally
    equivalent to the persisted-snapshot model (ADR-0009 §1 Warm) at
    the contract surface, but avoids the O(days * versions)
    materialisation cost that quickly explodes for property-test
    fixtures with multi-year valid windows.
    """

    hot_entities: list[EntityVersion] = field(default_factory=list)
    warm_versions: list[EntityVersion] = field(default_factory=list)
    # Archive snapshots are intentionally opaque: ADR-0009 §1 Archive
    # specifies they're not queryable via MCP/REST; the simulator only
    # tracks that they exist for the workspace.
    archived_workspace_dates: set[tuple[UUID, datetime]] = field(default_factory=set)

    @property
    def warm_snapshots(self) -> list[WarmSnapshotRow]:
        """Materialised warm rows for callers that need a list view.

        Used by tests that assert on snapshot count / equality; the
        materialisation is one row per (warm version, day in the
        intersection of its valid window with the warm `as_of`
        window).  Bounded by the warm window size.
        """
        rows: list[WarmSnapshotRow] = []
        for v in self.warm_versions:
            for snap_date in _warm_days_for_version(v):
                rows.append(
                    WarmSnapshotRow(
                        workspace_id=v.workspace_id,
                        entity_key=v.entity_key,
                        stable_id=v.stable_id,
                        snapshot_date=snap_date,
                        valid_from=v.valid_from,
                        valid_to=v.valid_to,
                        recorded_at_at_snapshot=v.recorded_at,
                    )
                )
        return rows


def run_retention(
    sim: GraphSimulator,
    now: datetime,
    *,
    state: RetentionState | None = None,
) -> RetentionState:
    """Run one retention pass.

    Per ADR-0009 §2:

    * Eviction is **per-version**: a node identity whose latest
      version is hot but whose older versions are past the cutoff
      moves the older versions to warm while the latest stays hot.
    * Edge end-dating does **not** trigger eviction — only
      ``recorded_at`` does.
    * Per-tenant iteration: cross-tenant batches are forbidden
      structurally — the worker iterates per-workspace.

    The pass is **idempotent** (§3): re-running this function with the
    same ``now`` and the same ``state`` yields the same result, because
    the eligibility predicate is stateless and the warm rows are keyed
    by ``(workspace_id, entity_id, snapshot_date)``.
    """
    if state is None:
        state = RetentionState(hot_entities=list(sim.entities))

    hot_cutoff = now - timedelta(days=HOT_WINDOW_DAYS)
    warm_cutoff = now - timedelta(days=WARM_WINDOW_DAYS)

    # Group by workspace -> per-tenant iteration (ADR-0009 §2).
    by_workspace: dict[UUID, list[EntityVersion]] = {}
    for v in state.hot_entities:
        by_workspace.setdefault(v.workspace_id, []).append(v)

    new_hot: list[EntityVersion] = []
    new_warm: list[EntityVersion] = list(state.warm_versions)
    archived_keys: set[tuple[UUID, datetime]] = set(state.archived_workspace_dates)

    for workspace_id, versions in by_workspace.items():
        for v in versions:
            if v.recorded_at >= hot_cutoff:
                # Hot — keep in live store.
                new_hot.append(v)
                continue
            if v.recorded_at >= warm_cutoff:
                # Warm — eligible source.  Idempotent: skip if the
                # same version is already tracked.
                if not any(_version_identity_equal(v, w) for w in new_warm):
                    new_warm.append(v)
                continue
            # Archive — track the (workspace, day) pair; the row is no
            # longer queryable via MCP/REST per ADR-0009 §1 Archive.
            archived_keys.add((workspace_id, _floor_utc_day(v.recorded_at)))

    return RetentionState(
        hot_entities=new_hot,
        warm_versions=new_warm,
        archived_workspace_dates=archived_keys,
    )


def _version_identity_equal(a: EntityVersion, b: EntityVersion) -> bool:
    """Whether two versions are the same row (idempotency key)."""
    return (
        a.workspace_id == b.workspace_id
        and a.entity_key == b.entity_key
        and a.valid_from == b.valid_from
        and a.valid_to == b.valid_to
    )


def _warm_days_for_version(v: EntityVersion) -> list[datetime]:
    """Enumerate the UTC days a warm-tier ``as_of`` could hit for ``v``.

    The simulator caps the enumeration at a bounded window (the warm
    `as_of` range relative to a fixed NOW) so the materialisation
    cost stays small on property-test fixtures.  This is a contract-
    preserving simplification: at the call site of
    ``query_with_tiers`` we re-derive the day from ``as_of`` and look
    up the version directly, so the materialised list is only ever
    walked by tests that explicitly want it.
    """
    # Bound the per-version enumeration to a reasonable window
    # (≈the warm tier).  The actual contract is "one row per day in
    # the valid window"; in tests we compute on demand via
    # ``query_with_tiers``.
    end_exclusive = v.valid_to if v.valid_to is not None else v.valid_from + timedelta(days=400)
    start_day = _floor_utc_day(v.valid_from)
    end_day = _floor_utc_day(end_exclusive - timedelta(microseconds=1))
    days: list[datetime] = []
    cur = start_day
    cap = 400  # safety cap.
    while cur <= end_day and cap > 0:
        days.append(cur)
        cur = cur + timedelta(days=1)
        cap -= 1
    return days


def query_with_tiers(
    state: RetentionState,
    workspace_id: UUID,
    entity_key: str,
    as_of: datetime,
    now: datetime,
) -> EntityVersion | WarmSnapshotRow | dict[str, str] | None:
    """Resolve a tiered point-in-time read (ADR-0009 §4).

    Returns:
        * an :class:`EntityVersion` for hot reads,
        * a :class:`WarmSnapshotRow` for warm reads,
        * a degraded-response ``meta`` envelope for archive reads,
        * ``None`` if no version covered ``as_of`` in any tier.
    """
    tier = resolve_tier(now, as_of)
    if tier == "archive":
        return {"degraded_response": DEGRADED_ARCHIVE_TIER}
    if tier == "hot":
        # Hot reads target the live store; if the version that
        # covers ``as_of`` happens to live in warm storage (its
        # ``recorded_at`` is past the hot cutoff but its
        # ``valid_from`` is still inside the hot ``as_of`` window),
        # the read still resolves — ADR-0009 §4 ties tier resolution
        # to ``as_of`` but the no-data-loss invariant requires that
        # all live versions remain reachable across the
        # eviction boundary.  In production this is the
        # `:Snapshot:Daily` discriminator label being skipped on the
        # hot path while the live `:Entity` row is read; in the
        # simulator we mirror the same effect by walking warm
        # versions as a hot-path fallback.
        hit = _hot_lookup(state.hot_entities, workspace_id, entity_key, as_of)
        if hit is not None:
            return hit
        return _hot_lookup(state.warm_versions, workspace_id, entity_key, as_of)
    # warm — snapshot-per-day, day precision.  The simulator stores
    # warm-eligible source versions and projects to a snapshot row
    # on-read (ADR-0009 §1 Warm).
    snap_date = _floor_utc_day(as_of)
    warm_hit = _hot_lookup(state.warm_versions, workspace_id, entity_key, as_of)
    if warm_hit is not None:
        return WarmSnapshotRow(
            workspace_id=warm_hit.workspace_id,
            entity_key=warm_hit.entity_key,
            stable_id=warm_hit.stable_id,
            snapshot_date=snap_date,
            valid_from=warm_hit.valid_from,
            valid_to=warm_hit.valid_to,
            recorded_at_at_snapshot=warm_hit.recorded_at,
        )
    # Transitional fallback: if the worker has not yet evicted this
    # record from the live store, the warm-tier read still finds it
    # (ADR-0009 §4 — tier resolution is on ``as_of``, but the live
    # store remains the source of truth until the worker has moved
    # the row).  This preserves the "no data loss across hot -> warm"
    # invariant in the steady-state transition window.
    return _hot_lookup(state.hot_entities, workspace_id, entity_key, as_of)


def _hot_lookup(
    versions: list[EntityVersion],
    workspace_id: UUID,
    entity_key: str,
    as_of: datetime,
) -> EntityVersion | None:
    """Apply the canonical §5 predicate against a list of live versions."""
    for v in versions:
        if v.workspace_id != workspace_id:
            continue
        if v.entity_key != entity_key:
            continue
        if v.valid_from <= as_of and (v.valid_to is None or as_of < v.valid_to):
            return v
    return None


def _floor_utc_day(ts: datetime) -> datetime:
    """Project a timestamp to its UTC day-floor (ADR-0009 §1 Warm)."""
    return datetime(ts.year, ts.month, ts.day, tzinfo=UTC)


__all__ = [
    "DEGRADED_ARCHIVE_TIER",
    "DEGRADED_PRE_HISTORY",
    "HOT_WINDOW_DAYS",
    "WARM_WINDOW_DAYS",
    "EdgeVersion",
    "EntityVersion",
    "GraphSimulator",
    "RetentionState",
    "WarmSnapshotRow",
    "degraded_response_for",
    "query_with_tiers",
    "resolve_tier",
    "run_retention",
]
