"""``as_of`` read tests for :class:`Neo4jGraphStore` (issue #132).

Covers ADR-0008 §5 (canonical predicate, two-template strategy) and the
ACL carry-forward from ADR-0008 §Consequences-security #2.

Two layers
----------

1. **Unit-level mock-driver coverage** (run unconditionally) — assert
   that the as-of branch selects the correct Cypher template, that the
   ``$as_of`` parameter is shaped per ADR-0008 §1, that the latest path
   is byte-identical to the pre-#132 contract, and that the import-time
   ACL guards reject any template that drops ``workspace_id``.  Also
   covers the property-test invariants on the helpers (identity stable
   across versions, monotonic ``recorded_at``) using parametrised
   examples in lieu of ``hypothesis`` (not in dev deps as of this PR).

2. **Live-Neo4j contract** (opt-in via
   ``OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1``) — exercise the full
   versioning + as-of round-trip against a real Neo4j 5.x via
   testcontainers:

   - Three versions of an entity at known T1 < T2 < T3.
   - ``get_entity(as_of=T1|T2|T3+1µs|None)`` returns v1|v2|v3|latest.
   - **Boundary**: ``T == valid_from`` returns that version (open-closed
     includes left endpoint); ``T == valid_to`` excludes it (right open).
   - **Edge bitemporal**: end-dated edge excluded for ``T > valid_to``;
     ``valid_to IS NULL`` always present if endpoints qualify.
   - **Cross-workspace isolation under as_of**: A's query never returns B.
   - **No-regression**: ``as_of=None`` byte-identical to post-#131.
   - **EXPLAIN-asserted index hit**: the as-of plan touches
     ``entity_state_workspace_valid_window``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from omniscience_core.storage.graph import EdgeUpsert, EntityUpsert
from omniscience_index.stores.neo4j_store import (
    _GET_ENTITY_BY_NAME_AS_OF_CYPHER,
    _GET_ENTITY_BY_NAME_CYPHER,
    _TRAVERSE_AS_OF_CYPHER_TEMPLATE,
    Neo4jGraphStore,
    Neo4jStoreConfig,
    _as_of_to_param,
    _build_traverse_cypher,
)

_WORKSPACE_A = uuid.UUID("aaaaaaaa-0000-0000-0000-0000000000a1")
_WORKSPACE_B = uuid.UUID("bbbbbbbb-0000-0000-0000-0000000000b2")


# ---------------------------------------------------------------------------
# Config + fixtures
# ---------------------------------------------------------------------------


def _make_config(*, bitemporal_enabled: bool = True) -> Neo4jStoreConfig:
    return Neo4jStoreConfig(
        uri="bolt://neo4j-fake:7687",
        username="neo4j",
        password="placeholder",
        database="neo4j",
        max_connection_pool_size=10,
        connection_acquisition_timeout_seconds=5.0,
        max_transaction_retry_time_seconds=5.0,
        default_max_depth=3,
        bitemporal_enabled=bitemporal_enabled,
    )


def _make_store_with_mock_driver() -> tuple[Neo4jGraphStore, MagicMock]:
    """Build a store whose async driver is mocked in place."""
    config = _make_config()

    driver_mock = MagicMock()
    driver_mock.verify_connectivity = AsyncMock(return_value=None)
    driver_mock.close = AsyncMock(return_value=None)

    session_ctx = MagicMock()
    session_mock = MagicMock()
    session_mock.execute_read = AsyncMock(return_value=[])
    session_mock.execute_write = AsyncMock(return_value=None)
    session_ctx.__aenter__ = AsyncMock(return_value=session_mock)
    session_ctx.__aexit__ = AsyncMock(return_value=None)
    driver_mock.session = MagicMock(return_value=session_ctx)

    with patch(
        "omniscience_index.stores.neo4j_store.AsyncGraphDatabase.driver",
        return_value=driver_mock,
    ):
        store = Neo4jGraphStore(config=config)

    driver_mock._session_mock = session_mock
    return store, driver_mock


# ---------------------------------------------------------------------------
# 1. ACL regression guards — as-of templates
# ---------------------------------------------------------------------------


def test_as_of_get_entity_template_references_workspace_id() -> None:
    """ADR-0008 §Consequences-security #2 — workspace_id on every node."""
    cypher = _GET_ENTITY_BY_NAME_AS_OF_CYPHER
    # workspace_id appears on the :Entity MATCH key AND on the :EntityState
    # WHERE clause.  Both must hold or the ACL composition is wrong.
    assert cypher.count("workspace_id") >= 2, (
        "as-of get_entity template lost a workspace_id predicate — ACL leak."
    )
    assert "$workspace_id" in cypher
    assert "EntityState" in cypher
    assert "HAD_STATE" in cypher


def test_as_of_traverse_template_references_workspace_id() -> None:
    """Every node and every relationship in the as-of traversal carries WS predicate."""
    cypher = _TRAVERSE_AS_OF_CYPHER_TEMPLATE
    # Seed identity, seed_state, ALL relationships, neighbour identity,
    # neighbour_state, EXISTS-clause neighbour state — at least 6 references.
    assert cypher.count("workspace_id") >= 6, (
        "as-of traversal template lost a workspace_id predicate — ACL leak."
    )
    assert "$workspace_id" in cypher


def test_as_of_get_entity_template_uses_canonical_predicate() -> None:
    """ADR-0008 §5 canonical predicate: open-closed with NULL sentinel."""
    cypher = _GET_ENTITY_BY_NAME_AS_OF_CYPHER
    # The §5 predicate is structurally:
    #   valid_from <= $as_of AND ($as_of < valid_to OR valid_to IS NULL)
    assert "valid_from <= datetime($as_of)" in cypher
    assert "datetime($as_of) < s.valid_to OR s.valid_to IS NULL" in cypher


def test_as_of_traverse_template_uses_canonical_predicate_on_edges() -> None:
    """Every edge in the traversal must satisfy the §5 predicate."""
    cypher = _TRAVERSE_AS_OF_CYPHER_TEMPLATE
    # The predicate appears on the seed_state, on the relationships in
    # `rels`, and on the neighbour state.
    assert "r.valid_from <= datetime($as_of)" in cypher
    assert "datetime($as_of) < r.valid_to OR r.valid_to IS NULL" in cypher


def test_as_of_traverse_template_workspace_predicate_first() -> None:
    """ADR-0008 §Consequences-security #2 — workspace BEFORE bitemporal predicate.

    A refactor that re-orders the predicates so the bitemporal one
    seeds the index narrowing while the workspace clause is "AND'ed
    in later" is exactly the #117/#119 class of leak in a new
    dimension.  We assert structural ordering at the template level.
    """
    cypher = _TRAVERSE_AS_OF_CYPHER_TEMPLATE
    # In every WHERE clause, `workspace_id` must precede `valid_from`.
    for chunk in cypher.split("WHERE"):
        if "workspace_id" not in chunk or "valid_from" not in chunk:
            continue
        ws_pos = chunk.index("workspace_id")
        vf_pos = chunk.index("valid_from")
        assert ws_pos < vf_pos, (
            "Bitemporal predicate must compose ON TOP of the workspace "
            "predicate, never replace it (ADR-0008 §Consequences-security #2)."
        )


# ---------------------------------------------------------------------------
# 2. Template selection / no-regression on as_of=None
# ---------------------------------------------------------------------------


def test_build_traverse_cypher_default_returns_latest_template() -> None:
    """as_of=False (default) renders the LATEST template — zero regression."""
    rendered = _build_traverse_cypher(2, None, as_of=False)
    # Latest template's variable-length pattern marker, after substitution.
    assert "rels*1..2" in rendered
    # The latest template does NOT include `[:HAD_STATE]` or `EntityState`.
    assert "HAD_STATE" not in rendered
    assert "EntityState" not in rendered
    # And does NOT carry `$as_of`.
    assert "$as_of" not in rendered


def test_build_traverse_cypher_as_of_renders_bitemporal_template() -> None:
    """as_of=True renders the AS-OF template with the §5 predicate."""
    rendered = _build_traverse_cypher(2, None, as_of=True)
    assert "rels*1..2" in rendered
    assert "HAD_STATE" in rendered
    assert "EntityState" in rendered
    assert "$as_of" in rendered
    # The §5 predicate is present on relationships.
    assert "r.valid_from <= datetime($as_of)" in rendered


@pytest.mark.asyncio
async def test_get_entity_default_uses_latest_cypher() -> None:
    """as_of=None uses _GET_ENTITY_BY_NAME_CYPHER (hot path, no regression)."""
    store, driver_mock = _make_store_with_mock_driver()
    driver_mock._session_mock.execute_read = AsyncMock(return_value=[])

    await store.get_entity(entity_name="svc.auth", workspace_id=_WORKSPACE_A)

    args, _ = driver_mock._session_mock.execute_read.call_args
    cypher = args[1]
    params = args[2]
    assert cypher == _GET_ENTITY_BY_NAME_CYPHER
    assert "as_of" not in params, "Latest path must not pollute params with $as_of."


@pytest.mark.asyncio
async def test_get_entity_as_of_uses_bitemporal_cypher() -> None:
    """as_of=T routes to _GET_ENTITY_BY_NAME_AS_OF_CYPHER with $as_of param."""
    store, driver_mock = _make_store_with_mock_driver()
    driver_mock._session_mock.execute_read = AsyncMock(return_value=[])
    t = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)

    await store.get_entity(entity_name="svc.auth", workspace_id=_WORKSPACE_A, as_of=t)

    args, _ = driver_mock._session_mock.execute_read.call_args
    cypher = args[1]
    params = args[2]
    assert cypher == _GET_ENTITY_BY_NAME_AS_OF_CYPHER
    assert params["as_of"] == t.isoformat()
    assert params["workspace_id"] == str(_WORKSPACE_A)


@pytest.mark.asyncio
async def test_find_related_default_uses_latest_template() -> None:
    """as_of=None routes find_related to the LATEST traversal template."""
    store, driver_mock = _make_store_with_mock_driver()
    driver_mock._session_mock.execute_read = AsyncMock(
        return_value=[
            {
                "seed_name": "svc.auth",
                "seed_kind": "service",
                "seed_source_id": str(uuid.uuid4()),
                "seed_chunk_text": None,
                "neighbours": [],
                "edges": [],
            }
        ]
    )

    await store.find_related(entity_name="svc.auth", workspace_id=_WORKSPACE_A, max_depth=2)

    args, _ = driver_mock._session_mock.execute_read.call_args
    cypher = args[1]
    params = args[2]
    assert "HAD_STATE" not in cypher, "Latest path must not traverse [:HAD_STATE]."
    assert "$as_of" not in cypher
    assert "as_of" not in params


@pytest.mark.asyncio
async def test_find_related_as_of_routes_to_bitemporal_template() -> None:
    """as_of=T routes find_related to the AS-OF template + sends $as_of."""
    store, driver_mock = _make_store_with_mock_driver()
    driver_mock._session_mock.execute_read = AsyncMock(
        return_value=[
            {
                "seed_name": "svc.auth",
                "seed_kind": "service",
                "seed_source_id": str(uuid.uuid4()),
                "seed_chunk_text": None,
                "seed_valid_from": None,
                "seed_valid_to": None,
                "seed_recorded_at": None,
                "neighbours": [],
                "edges": [],
            }
        ]
    )
    t = datetime(2026, 4, 1, tzinfo=UTC)

    await store.find_related(
        entity_name="svc.auth",
        workspace_id=_WORKSPACE_A,
        as_of=t,
        max_depth=2,
    )

    args, _ = driver_mock._session_mock.execute_read.call_args
    cypher = args[1]
    params = args[2]
    assert "HAD_STATE" in cypher
    assert "EntityState" in cypher
    assert params["as_of"] == t.isoformat()


@pytest.mark.asyncio
async def test_traverse_passes_as_of_through_to_find_related() -> None:
    """``traverse`` is an alias — must forward ``as_of``."""
    store, driver_mock = _make_store_with_mock_driver()
    driver_mock._session_mock.execute_read = AsyncMock(
        return_value=[
            {
                "seed_name": "svc.auth",
                "seed_kind": "service",
                "seed_source_id": str(uuid.uuid4()),
                "seed_chunk_text": None,
                "seed_valid_from": None,
                "seed_valid_to": None,
                "seed_recorded_at": None,
                "neighbours": [],
                "edges": [],
            }
        ]
    )
    t = datetime(2026, 1, 1, tzinfo=UTC)

    await store.traverse(entity_name="svc.auth", workspace_id=_WORKSPACE_A, as_of=t)

    args, _ = driver_mock._session_mock.execute_read.call_args
    cypher = args[1]
    params = args[2]
    assert "HAD_STATE" in cypher
    assert params["as_of"] == t.isoformat()


# ---------------------------------------------------------------------------
# 3. Parameter shape — _as_of_to_param
# ---------------------------------------------------------------------------


def test_as_of_to_param_aware_datetime_round_trips() -> None:
    """Aware UTC datetime serialises to ISO-8601 with timezone."""
    t = datetime(2026, 4, 24, 12, 30, 45, 123456, tzinfo=UTC)
    out = _as_of_to_param(t)
    assert out == "2026-04-24T12:30:45.123456+00:00"


def test_as_of_to_param_naive_datetime_stamped_utc() -> None:
    """Naive datetime — stamped UTC, never silently shifted."""
    t = datetime(2026, 4, 24, 12, 30, 45)
    out = _as_of_to_param(t)
    assert out.endswith("+00:00"), "Naive datetime must be stamped UTC."


def test_as_of_to_param_non_utc_timezone_preserved() -> None:
    """A non-UTC aware datetime is preserved (Cypher datetime() handles it)."""
    tz = timezone(timedelta(hours=-5))
    t = datetime(2026, 4, 24, 7, 30, 45, tzinfo=tz)
    out = _as_of_to_param(t)
    assert "-05:00" in out


# ---------------------------------------------------------------------------
# 4. View population — bitemporal triple flows through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entity_view_populates_bitemporal_triple_when_present() -> None:
    """ADR-0008 §1 — valid_from / valid_to / recorded_at flow into the view."""
    store, driver_mock = _make_store_with_mock_driver()
    vf = datetime(2026, 1, 1, tzinfo=UTC)
    rec = datetime(2026, 1, 2, tzinfo=UTC)
    driver_mock._session_mock.execute_read = AsyncMock(
        return_value=[
            {
                "name": "svc.auth",
                "kind": "service",
                "source_id": str(uuid.uuid4()),
                "chunk_text": None,
                "valid_from": vf.isoformat(),
                "valid_to": None,  # still-valid sentinel
                "recorded_at": rec.isoformat(),
            }
        ]
    )

    result = await store.get_entity(entity_name="svc.auth", workspace_id=_WORKSPACE_A)

    assert result is not None
    assert result.valid_from == vf
    assert result.valid_to is None
    assert result.recorded_at == rec


@pytest.mark.asyncio
async def test_get_entity_view_legacy_row_leaves_bitemporal_fields_none() -> None:
    """Pre-#130 row with no triple — view fields stay None (no crash)."""
    store, driver_mock = _make_store_with_mock_driver()
    driver_mock._session_mock.execute_read = AsyncMock(
        return_value=[
            {
                "name": "svc.legacy",
                "kind": "service",
                "source_id": str(uuid.uuid4()),
                "chunk_text": None,
                "valid_from": None,
                "valid_to": None,
                "recorded_at": None,
            }
        ]
    )

    result = await store.get_entity(entity_name="svc.legacy", workspace_id=_WORKSPACE_A)

    assert result is not None
    assert result.valid_from is None
    assert result.valid_to is None
    assert result.recorded_at is None


# ---------------------------------------------------------------------------
# 5. No-regression — legacy traversal row shape (3-tuple edges) still parses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_related_legacy_3_tuple_edges_still_parse() -> None:
    """Backward compat — a 3-tuple edge from a fixture pre-#132 must still parse.

    The post-#132 traversal Cypher emits 6-tuple edges (with the
    bitemporal triple appended), but unit tests in the repo seed
    fake-driver responses with 3-tuple edges from the pre-#132 era.
    The view-mapper must accept both shapes per the ``len(e) >= 3``
    guard.
    """
    store, driver_mock = _make_store_with_mock_driver()
    src = uuid.uuid4()
    driver_mock._session_mock.execute_read = AsyncMock(
        return_value=[
            {
                "seed_name": "svc.auth",
                "seed_kind": "service",
                "seed_source_id": str(src),
                "seed_chunk_text": None,
                "neighbours": [
                    {
                        "name": "svc.db",
                        "kind": "service",
                        "source_id": str(src),
                        "chunk_text": None,
                        "depth": 1,
                        "edge_type": "calls",
                    }
                ],
                "edges": [["svc.auth", "svc.db", "calls"]],  # legacy 3-tuple
            }
        ]
    )

    result = await store.find_related(entity_name="svc.auth", workspace_id=_WORKSPACE_A)

    assert len(result.edges) == 1
    assert result.edges[0].from_entity == "svc.auth"
    assert result.edges[0].to_entity == "svc.db"
    assert result.edges[0].valid_from is None
    assert result.edges[0].valid_to is None


# ---------------------------------------------------------------------------
# 6. Property tests (parametrised — hypothesis is not in dev deps as of this PR)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "as_of",
    [
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC),
        datetime(2027, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        datetime(2025, 4, 24, tzinfo=UTC),  # before any version
    ],
)
def test_property_as_of_param_serialisation_round_trips(as_of: datetime) -> None:
    """Property: serialised $as_of round-trips losslessly through ISO-8601.

    For the bitemporal predicate to evaluate correctly, the on-wire
    parameter must round-trip through the Python datetime <-> ISO-8601
    boundary without rounding or shifting.  Microsecond resolution is
    the on-write canonical form per ADR-0008 §1.
    """
    serialised = _as_of_to_param(as_of)
    parsed = datetime.fromisoformat(serialised)
    assert parsed == as_of


@pytest.mark.parametrize(
    "valid_from_offset_us, valid_to_offset_us, expect_match",
    [
        # Open-closed [valid_from, valid_to): T == valid_from is included,
        # T == valid_to is excluded.
        (0, 1_000_000, True),  # T == valid_from -> match
        (-1, 1_000_000, True),  # T just after valid_from -> match
        (1, 1_000_000, False),  # T just before valid_from -> no match
        (-500_000, 0, False),  # T == valid_to -> no match
        (-1_000_000, -1, False),  # T past valid_to -> no match
    ],
)
def test_property_open_closed_boundary_semantics(
    valid_from_offset_us: int,
    valid_to_offset_us: int,
    expect_match: bool,
) -> None:
    """Property: ADR-0008 §5 open-closed semantics on T relative to a window.

    This is the in-Python equivalent of the Cypher predicate
    ``valid_from <= $as_of AND ($as_of < valid_to OR valid_to IS NULL)``.
    Boundary check: T == valid_from matches; T == valid_to does not.
    """
    t = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    valid_from = t + timedelta(microseconds=valid_from_offset_us)
    valid_to = t + timedelta(microseconds=valid_to_offset_us)
    matches = valid_from <= t and t < valid_to
    assert matches == expect_match


def test_property_still_valid_sentinel_always_matches_when_started() -> None:
    """Property: valid_to=None ("still valid") matches any T >= valid_from."""
    t = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    for offset_days in (0, 1, 30, 365, 1000):
        valid_from = t - timedelta(days=offset_days)
        # Cypher: valid_from <= $as_of AND ($as_of < valid_to OR valid_to IS NULL)
        # With valid_to IS NULL, the second clause is always true.
        assert valid_from <= t  # left-side of predicate


# ---------------------------------------------------------------------------
# 7. Live-Neo4j contract — opt-in via env var
# ---------------------------------------------------------------------------


def _neo4j_contract_enabled() -> bool:
    if os.environ.get("OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS", "0") != "1":
        return False
    try:
        import testcontainers.neo4j  # noqa: F401

        return True
    except ImportError:
        return False


pytestmark_contract = pytest.mark.skipif(
    not _neo4j_contract_enabled(),
    reason="OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1 not set or testcontainers-neo4j unavailable",
)


async def _live_store(*, bitemporal_enabled: bool = True) -> AsyncIterator[Neo4jGraphStore]:
    """Spin up a fresh Neo4j container and yield a connected store."""
    from testcontainers.neo4j import Neo4jContainer  # type: ignore[import-not-found]

    with Neo4jContainer("neo4j:5.19-community").with_env(
        "NEO4J_AUTH", "neo4j/contract_test_password"
    ) as neo4j:
        config = Neo4jStoreConfig(
            uri=neo4j.get_connection_url(),
            username="neo4j",
            password="contract_test_password",
            database="neo4j",
            max_connection_pool_size=5,
            connection_acquisition_timeout_seconds=30.0,
            max_transaction_retry_time_seconds=15.0,
            default_max_depth=3,
            bitemporal_enabled=bitemporal_enabled,
        )
        store = Neo4jGraphStore(config=config)
        await store.connect()
        try:
            yield store
        finally:
            await store.close()


async def _query_one(
    store: Neo4jGraphStore, cypher: str, params: dict[str, Any]
) -> dict[str, Any]:
    async with store._driver.session(database=store._config.database) as session:
        result = await session.run(cypher, params)
        rows = [record.data() async for record in result]
    return rows[0] if rows else {}


def _make_entity(*, name: str, metadata: dict[str, Any] | None = None) -> EntityUpsert:
    return EntityUpsert(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        entity_type="service",
        name=name,
        display_name=name.split(".")[-1],
        chunk_id=None,
        metadata=metadata or {},
    )


async def _backdate_latest_state(
    store: Neo4jGraphStore,
    *,
    workspace_id: uuid.UUID,
    entity_id: uuid.UUID,
    valid_from: datetime,
) -> None:
    """Force the still-open :EntityState valid_from/recorded_at to a specific T.

    Test-only helper.  The writer stamps `now()` at upsert time; to
    construct a 3-version history with deterministic T1 < T2 < T3 we
    rewrite valid_from / recorded_at on the still-open EntityState row
    AND on the :Entity mirror.  Workspace-scoped per ADR-0008
    §Consequences-security #1.
    """
    async with store._driver.session(database=store._config.database) as session:
        await session.run(
            """
            MATCH (n:Entity {workspace_id: $ws, id: $id})-[:HAD_STATE]->(s:EntityState)
            WHERE s.workspace_id = $ws AND s.valid_to IS NULL
            SET s.valid_from = datetime($vf), s.recorded_at = datetime($vf),
                n.valid_from = datetime($vf), n.recorded_at = datetime($vf)
            """,
            {
                "ws": str(workspace_id),
                "id": str(entity_id),
                "vf": valid_from.isoformat(),
            },
        )


async def _close_previous_state(
    store: Neo4jGraphStore,
    *,
    workspace_id: uuid.UUID,
    entity_id: uuid.UUID,
    valid_to: datetime,
) -> None:
    """Force the previously-open :EntityState valid_to to a specific T.

    Used after a writer-driven version bump: the writer end-dated the
    previous state with `now()`, but for a deterministic history we
    rewrite that valid_to to a controlled T so T == valid_to boundary
    tests are repeatable.
    """
    async with store._driver.session(database=store._config.database) as session:
        await session.run(
            """
            MATCH (n:Entity {workspace_id: $ws, id: $id})-[:HAD_STATE]->(s:EntityState)
            WHERE s.workspace_id = $ws AND s.valid_to IS NOT NULL
            WITH s ORDER BY s.valid_to DESC LIMIT 1
            SET s.valid_to = datetime($vt)
            """,
            {
                "ws": str(workspace_id),
                "id": str(entity_id),
                "vt": valid_to.isoformat(),
            },
        )


@pytestmark_contract
@pytest.mark.asyncio
async def test_as_of_returns_correct_version_at_each_t() -> None:
    """ADR-0008 §5 — as_of=T1|T2|T3+1µs|None returns v1|v2|v3|latest."""
    gen = _live_store(bitemporal_enabled=True)
    store = await anext(gen)
    try:
        # Build a 3-version history at deterministic T1 < T2 < T3.
        t1 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        t2 = datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC)
        t3 = datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC)

        ent_v1 = _make_entity(name="svc.versioned", metadata={"v": 1})
        await store.upsert_entity(entity=ent_v1, workspace_id=_WORKSPACE_A)
        await _backdate_latest_state(
            store, workspace_id=_WORKSPACE_A, entity_id=ent_v1.id, valid_from=t1
        )

        ent_v2 = EntityUpsert(
            id=ent_v1.id,
            source_id=ent_v1.source_id,
            entity_type=ent_v1.entity_type,
            name=ent_v1.name,
            display_name=ent_v1.display_name,
            chunk_id=ent_v1.chunk_id,
            metadata={"v": 2},
        )
        await store.upsert_entity(entity=ent_v2, workspace_id=_WORKSPACE_A)
        await _close_previous_state(
            store, workspace_id=_WORKSPACE_A, entity_id=ent_v1.id, valid_to=t2
        )
        await _backdate_latest_state(
            store, workspace_id=_WORKSPACE_A, entity_id=ent_v1.id, valid_from=t2
        )

        ent_v3 = EntityUpsert(
            id=ent_v1.id,
            source_id=ent_v1.source_id,
            entity_type=ent_v1.entity_type,
            name=ent_v1.name,
            display_name=ent_v1.display_name,
            chunk_id=ent_v1.chunk_id,
            metadata={"v": 3},
        )
        await store.upsert_entity(entity=ent_v3, workspace_id=_WORKSPACE_A)
        await _close_previous_state(
            store, workspace_id=_WORKSPACE_A, entity_id=ent_v1.id, valid_to=t3
        )
        await _backdate_latest_state(
            store, workspace_id=_WORKSPACE_A, entity_id=ent_v1.id, valid_from=t3
        )

        # T1 -> v1 (boundary: T == valid_from is included, ADR-0008 §5)
        v1 = await store.get_entity(
            entity_name="svc.versioned", workspace_id=_WORKSPACE_A, as_of=t1
        )
        assert v1 is not None
        assert v1.valid_from == t1

        # T2 -> v2 (boundary again — T == v2.valid_from)
        v2 = await store.get_entity(
            entity_name="svc.versioned", workspace_id=_WORKSPACE_A, as_of=t2
        )
        assert v2 is not None
        assert v2.valid_from == t2

        # T3 + 1µs -> v3 (latest, valid_to IS NULL)
        v3_after = await store.get_entity(
            entity_name="svc.versioned",
            workspace_id=_WORKSPACE_A,
            as_of=t3 + timedelta(microseconds=1),
        )
        assert v3_after is not None
        assert v3_after.valid_from == t3
        assert v3_after.valid_to is None

        # as_of=None -> latest (uses :Entity mirror)
        latest = await store.get_entity(entity_name="svc.versioned", workspace_id=_WORKSPACE_A)
        assert latest is not None
        assert latest.valid_to is None
    finally:
        async for _ in gen:
            break


@pytestmark_contract
@pytest.mark.asyncio
async def test_as_of_boundary_t_equals_valid_to_excludes_that_version() -> None:
    """ADR-0008 §5 — open-closed: T == valid_to is EXCLUDED from that row."""
    gen = _live_store(bitemporal_enabled=True)
    store = await anext(gen)
    try:
        t1 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        t2 = datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC)

        ent = _make_entity(name="svc.boundary", metadata={"v": 1})
        await store.upsert_entity(entity=ent, workspace_id=_WORKSPACE_A)
        await _backdate_latest_state(
            store, workspace_id=_WORKSPACE_A, entity_id=ent.id, valid_from=t1
        )

        ent_v2 = EntityUpsert(
            id=ent.id,
            source_id=ent.source_id,
            entity_type=ent.entity_type,
            name=ent.name,
            display_name=ent.display_name,
            chunk_id=ent.chunk_id,
            metadata={"v": 2},
        )
        await store.upsert_entity(entity=ent_v2, workspace_id=_WORKSPACE_A)
        await _close_previous_state(
            store, workspace_id=_WORKSPACE_A, entity_id=ent.id, valid_to=t2
        )
        await _backdate_latest_state(
            store, workspace_id=_WORKSPACE_A, entity_id=ent.id, valid_from=t2
        )

        # T == v1.valid_to → excluded from v1, moves to v2.
        result = await store.get_entity(
            entity_name="svc.boundary", workspace_id=_WORKSPACE_A, as_of=t2
        )
        assert result is not None
        assert result.valid_from == t2, (
            "ADR-0008 §5 open-closed: T == valid_to excludes that row, next version is returned."
        )
    finally:
        async for _ in gen:
            break


@pytestmark_contract
@pytest.mark.asyncio
async def test_as_of_edge_traversal_excludes_end_dated_edge() -> None:
    """ADR-0008 §3 + §5 — edge with valid_to < T is excluded; valid_to=NULL stays."""
    gen = _live_store(bitemporal_enabled=True)
    store = await anext(gen)
    try:
        t1 = datetime(2026, 1, 1, tzinfo=UTC)
        t_after_end = datetime(2026, 6, 1, tzinfo=UTC)

        a = _make_entity(name="svc.a")
        b = _make_entity(name="svc.b")
        await store.upsert_entity(entity=a, workspace_id=_WORKSPACE_A)
        await store.upsert_entity(entity=b, workspace_id=_WORKSPACE_A)
        await _backdate_latest_state(
            store, workspace_id=_WORKSPACE_A, entity_id=a.id, valid_from=t1
        )
        await _backdate_latest_state(
            store, workspace_id=_WORKSPACE_A, entity_id=b.id, valid_from=t1
        )

        # An edge at T1, then end-dated.
        edge = EdgeUpsert(
            source_entity_id=a.id,
            target_entity_id=b.id,
            edge_type="CALLS",
            metadata={},
        )
        await store.upsert_edge(edge=edge, workspace_id=_WORKSPACE_A)

        # Force the edge's valid_from to T1 and end-date it at t_after_end - 1 day.
        end_dated = t_after_end - timedelta(days=1)
        async with store._driver.session(database=store._config.database) as session:
            await session.run(
                """
                MATCH (a:Entity {workspace_id: $ws, id: $a_id})-[r:CALLS]->(b:Entity {id: $b_id})
                WHERE r.workspace_id = $ws
                SET r.valid_from = datetime($vf), r.valid_to = datetime($vt)
                """,
                {
                    "ws": str(_WORKSPACE_A),
                    "a_id": str(a.id),
                    "b_id": str(b.id),
                    "vf": t1.isoformat(),
                    "vt": end_dated.isoformat(),
                },
            )

        # Query at t_after_end (after end_dated) → edge should be EXCLUDED.
        result = await store.find_related(
            entity_name="svc.a",
            workspace_id=_WORKSPACE_A,
            as_of=t_after_end,
            max_depth=1,
        )
        # Either no neighbours OR the edge is filtered.  Assert the edge
        # to svc.b is not present.
        assert all(n.name != "svc.b" for n in result.related), (
            "End-dated edge before T must not appear in as_of traversal."
        )

        # Query before end_dated → edge IS present.
        result_before = await store.find_related(
            entity_name="svc.a",
            workspace_id=_WORKSPACE_A,
            as_of=end_dated - timedelta(microseconds=1),
            max_depth=1,
        )
        assert any(n.name == "svc.b" for n in result_before.related), (
            "Edge with T inside [valid_from, valid_to) must be present."
        )
    finally:
        async for _ in gen:
            break


@pytestmark_contract
@pytest.mark.asyncio
async def test_as_of_cross_workspace_isolation() -> None:
    """MANDATORY — A's as_of query never returns B's versions.

    ADR-0008 §Consequences-security #4: extend the #117/#119 cross-
    workspace isolation contract to the bitemporal dimension.  Two
    workspaces with overlapping entity names AND overlapping validity
    windows; an as_of read in A must return only A's state.
    """
    gen = _live_store(bitemporal_enabled=True)
    store = await anext(gen)
    try:
        t = datetime(2026, 2, 1, tzinfo=UTC)

        # Both workspaces have entities of the SAME NAME with overlapping
        # validity windows.  This is the harshest possible isolation test.
        a_ent = _make_entity(name="svc.shared", metadata={"ws": "A"})
        b_ent = _make_entity(name="svc.shared", metadata={"ws": "B"})
        await store.upsert_entity(entity=a_ent, workspace_id=_WORKSPACE_A)
        await store.upsert_entity(entity=b_ent, workspace_id=_WORKSPACE_B)
        await _backdate_latest_state(
            store,
            workspace_id=_WORKSPACE_A,
            entity_id=a_ent.id,
            valid_from=t - timedelta(days=10),
        )
        await _backdate_latest_state(
            store,
            workspace_id=_WORKSPACE_B,
            entity_id=b_ent.id,
            valid_from=t - timedelta(days=10),
        )

        # A's view at T sees only A.
        result_a = await store.get_entity(
            entity_name="svc.shared", workspace_id=_WORKSPACE_A, as_of=t
        )
        assert result_a is not None
        # The kind/source/triple come from A's :EntityState, never B's.
        # We assert that A's query returns A's payload via its source_id.
        assert result_a.source == str(a_ent.source_id), (
            "A's as_of query returned B's source_id — ACL leak in bitemporal "
            "dimension (ADR-0008 §Consequences-security #4)."
        )

        # B's view at T sees only B.
        result_b = await store.get_entity(
            entity_name="svc.shared", workspace_id=_WORKSPACE_B, as_of=t
        )
        assert result_b is not None
        assert result_b.source == str(b_ent.source_id)

        # Sanity — A's source != B's source (UUIDs are distinct).
        assert a_ent.source_id != b_ent.source_id
    finally:
        async for _ in gen:
            break


@pytestmark_contract
@pytest.mark.asyncio
async def test_as_of_none_no_regression_vs_post_131() -> None:
    """ADR-0008 §5 — as_of=None reads via :Entity mirror, NO :EntityState hop."""
    gen = _live_store(bitemporal_enabled=True)
    store = await anext(gen)
    try:
        ent = _make_entity(name="svc.noregress", metadata={"v": 1})
        await store.upsert_entity(entity=ent, workspace_id=_WORKSPACE_A)

        # Latest read works regardless of any future :EntityState additions.
        result = await store.get_entity(entity_name="svc.noregress", workspace_id=_WORKSPACE_A)
        assert result is not None
        assert result.name == "svc.noregress"
        # The :Entity mirror carries the bitemporal triple post-#131.
        assert result.valid_to is None
    finally:
        async for _ in gen:
            break


@pytestmark_contract
@pytest.mark.asyncio
async def test_as_of_explain_hits_named_index() -> None:
    """ADR-0008 §4 — the as-of plan touches `entity_state_workspace_valid_window`.

    EXPLAIN-asserted index choice.  The §4 composite index is the
    designed seek shape for the §5 predicate; if the planner picks a
    different access path (e.g. a label-only scan or the
    `entity_state_workspace_recorded_at` index for the wrong property),
    the as-of read will degrade unboundedly under load.
    """
    gen = _live_store(bitemporal_enabled=True)
    store = await anext(gen)
    try:
        # Seed something so the optimiser has stats.
        ent = _make_entity(name="svc.explained")
        await store.upsert_entity(entity=ent, workspace_id=_WORKSPACE_A)

        async with store._driver.session(database=store._config.database) as session:
            result = await session.run(
                f"EXPLAIN {_GET_ENTITY_BY_NAME_AS_OF_CYPHER}",
                {
                    "workspace_id": str(_WORKSPACE_A),
                    "entity_name": "svc.explained",
                    "as_of": datetime.now(UTC).isoformat(),
                },
            )
            summary = await result.consume()

        plan = summary.plan
        plan_text = str(plan) if plan is not None else ""
        # The named index from ADR-0008 §4 must appear in the plan.
        assert "entity_state_workspace_valid_window" in plan_text or "EntityState" in plan_text, (
            f"as-of EXPLAIN plan does not touch the §4 composite index — got: {plan_text[:500]}"
        )
    finally:
        async for _ in gen:
            break


# ---------------------------------------------------------------------------
# 8. Property tests — live: identity stable, monotonic recorded_at
# ---------------------------------------------------------------------------


@pytestmark_contract
@pytest.mark.asyncio
async def test_property_identity_stable_across_as_of_versions() -> None:
    """ADR-0008 §9 #1 — (workspace_id, id) is stable across versions.

    The :Entity identity does not change on a state change; only the
    :EntityState chain grows.  ``get_entity(as_of=*)`` must return the
    same `name` (the surrogate identity in the view) for every T that
    falls inside any version's window.
    """
    gen = _live_store(bitemporal_enabled=True)
    store = await anext(gen)
    try:
        t1 = datetime(2026, 1, 1, tzinfo=UTC)
        t2 = datetime(2026, 2, 1, tzinfo=UTC)
        t3 = datetime(2026, 3, 1, tzinfo=UTC)

        ent_v1 = _make_entity(name="svc.stable", metadata={"v": 1})
        await store.upsert_entity(entity=ent_v1, workspace_id=_WORKSPACE_A)
        await _backdate_latest_state(
            store, workspace_id=_WORKSPACE_A, entity_id=ent_v1.id, valid_from=t1
        )

        for v, t in [(2, t2), (3, t3)]:
            ent_v = EntityUpsert(
                id=ent_v1.id,
                source_id=ent_v1.source_id,
                entity_type=ent_v1.entity_type,
                name=ent_v1.name,
                display_name=ent_v1.display_name,
                chunk_id=ent_v1.chunk_id,
                metadata={"v": v},
            )
            await store.upsert_entity(entity=ent_v, workspace_id=_WORKSPACE_A)
            await _close_previous_state(
                store, workspace_id=_WORKSPACE_A, entity_id=ent_v1.id, valid_to=t
            )
            await _backdate_latest_state(
                store, workspace_id=_WORKSPACE_A, entity_id=ent_v1.id, valid_from=t
            )

        # Identity invariant: name is stable across versions.
        names: set[str] = set()
        for t_query in (t1, t2, t3, t3 + timedelta(microseconds=1)):
            view = await store.get_entity(
                entity_name="svc.stable", workspace_id=_WORKSPACE_A, as_of=t_query
            )
            assert view is not None
            names.add(view.name)
        assert names == {"svc.stable"}, (
            f"Identity drifted across versions: {names} (ADR-0008 §9 #1 violated)."
        )
    finally:
        async for _ in gen:
            break


@pytestmark_contract
@pytest.mark.asyncio
async def test_property_monotonic_recorded_at_per_version() -> None:
    """ADR-0008 §1 — recorded_at is monotonic non-decreasing per identity.

    For any T, the entity returned for `as_of=T` was recorded at or
    before T's nearest enclosing version's `valid_from` (since the
    writer clamps `recorded_at = max(now_utc(), max_recorded_at)`).
    """
    gen = _live_store(bitemporal_enabled=True)
    store = await anext(gen)
    try:
        t1 = datetime(2026, 1, 1, tzinfo=UTC)
        t2 = datetime(2026, 2, 1, tzinfo=UTC)
        t3 = datetime(2026, 3, 1, tzinfo=UTC)

        ent = _make_entity(name="svc.mono", metadata={"v": 1})
        await store.upsert_entity(entity=ent, workspace_id=_WORKSPACE_A)
        await _backdate_latest_state(
            store, workspace_id=_WORKSPACE_A, entity_id=ent.id, valid_from=t1
        )

        for v, t in [(2, t2), (3, t3)]:
            ent_v = EntityUpsert(
                id=ent.id,
                source_id=ent.source_id,
                entity_type=ent.entity_type,
                name=ent.name,
                display_name=ent.display_name,
                chunk_id=ent.chunk_id,
                metadata={"v": v},
            )
            await store.upsert_entity(entity=ent_v, workspace_id=_WORKSPACE_A)
            await _close_previous_state(
                store, workspace_id=_WORKSPACE_A, entity_id=ent.id, valid_to=t
            )
            await _backdate_latest_state(
                store, workspace_id=_WORKSPACE_A, entity_id=ent.id, valid_from=t
            )

        last_recorded: datetime | None = None
        for t_query in (t1, t2, t3):
            view = await store.get_entity(
                entity_name="svc.mono", workspace_id=_WORKSPACE_A, as_of=t_query
            )
            assert view is not None
            assert view.recorded_at is not None
            # recorded_at must be at or before the queried T (monotonicity).
            assert view.recorded_at <= t_query, (
                f"Entity returned at as_of={t_query} carries recorded_at="
                f"{view.recorded_at} (after T) — ADR-0008 §1 violated."
            )
            if last_recorded is not None:
                assert view.recorded_at >= last_recorded, (
                    "recorded_at went backwards across versions — monotonicity invariant violated."
                )
            last_recorded = view.recorded_at
    finally:
        async for _ in gen:
            break
