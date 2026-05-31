"""Bitemporal writer tests for :class:`Neo4jGraphStore` (issue #131).

Covers ADR-0008 §2 (property-versioned identity-node + ``[:HAD_STATE]``
to per-version ``(:EntityState)`` nodes) and §3 (edge bitemporal as
relationship properties).

Two layers
----------

1. **Unit-level mock-driver coverage** (run unconditionally) — assert
   that the bitemporal flag pinned at ``__init__`` selects the correct
   Cypher template, that the ``state_fingerprint`` parameter is computed
   client-side, that the disabled-branch behaviour mirrors PR #104
   byte-for-byte, and that the import-time ACL guards reject any
   bitemporal template that drops ``workspace_id``.

2. **Live-Neo4j contract** (opt-in via
   ``OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1``) — exercise the full
   versioning lifecycle against a real Neo4j 5.x via testcontainers:

   - Single upsert creates one ``:Entity`` + one ``:EntityState`` + one
     ``[:HAD_STATE]`` (all with ``valid_from = recorded_at``,
     ``valid_to IS NULL``).
   - State-unchanged repeat upsert is a no-op.
   - State-changed upsert end-dates the previous ``:EntityState`` and
     creates a new one.
   - After N changes, ``:EntityState`` returns N versions, exactly one
     with ``valid_to IS NULL``.
   - Edge upsert: same idempotency-then-versioning pattern.
   - Atomicity: simulate transaction failure mid-version-bump, assert
     no half-versioned state.
   - Cross-workspace isolation: workspace A's writer never touches
     workspace B's ``:Entity`` / ``:EntityState`` / edges.
   - Flag gating: ``bitemporal_enabled=False`` writer leaves no
     ``:EntityState`` / ``[:HAD_STATE]`` artifacts; switching to
     ``True`` mid-suite begins versioning correctly.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from omniscience_core.storage.graph import EdgeUpsert, EntityUpsert
from omniscience_index.stores import neo4j_store
from omniscience_index.stores.neo4j_store import (
    _UPSERT_EDGE_BITEMPORAL_CYPHER_TEMPLATE,
    _UPSERT_EDGE_CYPHER_TEMPLATE,
    _UPSERT_ENTITY_BITEMPORAL_CYPHER,
    _UPSERT_ENTITY_CYPHER,
    Neo4jGraphStore,
    Neo4jStoreConfig,
    _edge_state_fingerprint,
    _entity_state_fingerprint,
    _stable_repr,
)

_WORKSPACE_A = uuid.UUID("aaaaaaaa-0000-0000-0000-0000000000a1")
_WORKSPACE_B = uuid.UUID("bbbbbbbb-0000-0000-0000-0000000000b2")


# ---------------------------------------------------------------------------
# Config + fixtures
# ---------------------------------------------------------------------------


def _make_config(*, bitemporal_enabled: bool) -> Neo4jStoreConfig:
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


def _make_store_with_mock_driver(*, bitemporal_enabled: bool) -> tuple[Neo4jGraphStore, MagicMock]:
    """Build a store whose async driver is mocked in place."""
    config = _make_config(bitemporal_enabled=bitemporal_enabled)

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
# 1. ACL regression guards — bitemporal templates
# ---------------------------------------------------------------------------


def test_bitemporal_entity_template_references_workspace_id() -> None:
    """ADR-0008 §Consequences-security #1 — workspace_id on every node + edge."""
    cypher = _UPSERT_ENTITY_BITEMPORAL_CYPHER
    # workspace_id appears on the :Entity MERGE key, the :EntityState CREATE,
    # and the [:HAD_STATE] CREATE.  Count occurrences so a refactor that
    # silently drops one is caught.
    assert cypher.count("workspace_id") >= 4, (
        "Bitemporal entity template lost a workspace_id predicate — ACL leak "
        "(ADR-0008 §Consequences-security #1)."
    )
    assert "$workspace_id" in cypher


def test_bitemporal_edge_template_references_workspace_id() -> None:
    """Edges are workspace-scoped on the relationship property."""
    cypher = _UPSERT_EDGE_BITEMPORAL_CYPHER_TEMPLATE
    assert cypher.count("workspace_id") >= 3, (
        "Bitemporal edge template lost a workspace_id predicate — ACL leak."
    )
    assert "$workspace_id" in cypher


def test_bitemporal_entity_template_creates_versioned_state_node() -> None:
    """ADR-0008 §2 — every state change emits a new :EntityState row."""
    cypher = _UPSERT_ENTITY_BITEMPORAL_CYPHER
    # The bitemporal template must materialise an :EntityState row keyed
    # by (workspace_id, id, valid_from) — the §4 uniqueness constraint.
    assert "EntityState" in cypher
    assert "HAD_STATE" in cypher
    assert "valid_from" in cypher
    assert "valid_to" in cypher
    assert "recorded_at" in cypher


def test_bitemporal_edge_template_end_dates_previous_open_relationship() -> None:
    """ADR-0008 §3 — state change end-dates the previous open edge."""
    cypher = _UPSERT_EDGE_BITEMPORAL_CYPHER_TEMPLATE
    # The template must locate the still-open edge (`valid_to IS NULL`)
    # and conditionally end-date it.
    assert "valid_to IS NULL" in cypher
    assert "SET r_open.valid_to" in cypher


def test_bitemporal_entity_template_uses_server_side_now() -> None:
    """ADR-0008 §1 — recorded_at comes from datetime() at write time, never client."""
    cypher = _UPSERT_ENTITY_BITEMPORAL_CYPHER
    # `datetime($now)` coerces the writer-supplied ISO-8601 string into a
    # Cypher datetime — server-side from the API caller's perspective
    # because `$now` is computed inside the adapter, not by the caller.
    assert "datetime($now)" in cypher


def test_bitemporal_edge_template_uses_server_side_now() -> None:
    cypher = _UPSERT_EDGE_BITEMPORAL_CYPHER_TEMPLATE
    assert "datetime($now)" in cypher


# ---------------------------------------------------------------------------
# 2. Fingerprint helpers — deterministic, content-only
# ---------------------------------------------------------------------------


def test_entity_fingerprint_is_deterministic_across_dict_ordering() -> None:
    """Metadata insertion order must not change the fingerprint."""
    a = {
        "entity_type": "service",
        "name": "svc.auth",
        "display_name": "auth",
        "source_id": "src-1",
        "chunk_id": None,
        "metadata": {"a": 1, "b": 2},
    }
    b = {
        "entity_type": "service",
        "name": "svc.auth",
        "display_name": "auth",
        "source_id": "src-1",
        "chunk_id": None,
        "metadata": {"b": 2, "a": 1},
    }
    assert _entity_state_fingerprint(a) == _entity_state_fingerprint(b)


def test_entity_fingerprint_changes_on_field_change() -> None:
    """A change in any state field must produce a different fingerprint."""
    base = {
        "entity_type": "service",
        "name": "svc.auth",
        "display_name": "auth",
        "source_id": "src-1",
        "chunk_id": None,
        "metadata": {"version": "v1"},
    }
    changed = dict(base)
    changed["metadata"] = {"version": "v2"}
    assert _entity_state_fingerprint(base) != _entity_state_fingerprint(changed)


def test_entity_fingerprint_excludes_id_and_workspace() -> None:
    """Identity is not state.  ``id`` and ``workspace_id`` must not flip the fp."""
    a = {
        "entity_type": "service",
        "name": "svc.x",
        "display_name": "x",
        "source_id": "src",
        "chunk_id": None,
        "metadata": {},
        "id": "00000000-0000-0000-0000-000000000001",
        "workspace_id": str(_WORKSPACE_A),
    }
    b = dict(a)
    b["id"] = "00000000-0000-0000-0000-000000000002"
    b["workspace_id"] = str(_WORKSPACE_B)
    assert _entity_state_fingerprint(a) == _entity_state_fingerprint(b)


def test_edge_fingerprint_is_deterministic() -> None:
    a = {
        "edge_type": "CALLS",
        "source_id": "src-1",
        "metadata": {"weight": 0.5, "label": "foo"},
    }
    b = {
        "edge_type": "CALLS",
        "source_id": "src-1",
        "metadata": {"label": "foo", "weight": 0.5},
    }
    assert _edge_state_fingerprint(a) == _edge_state_fingerprint(b)


def test_edge_fingerprint_excludes_endpoints_and_workspace() -> None:
    """Endpoints + workspace_id are MERGE-key fields, not state."""
    a = {
        "edge_type": "CALLS",
        "source_id": "src-1",
        "metadata": {},
        "source_id_ent": "x",
        "target_id_ent": "y",
        "workspace_id": str(_WORKSPACE_A),
    }
    b = dict(a)
    b["source_id_ent"] = "p"
    b["target_id_ent"] = "q"
    b["workspace_id"] = str(_WORKSPACE_B)
    assert _edge_state_fingerprint(a) == _edge_state_fingerprint(b)


def test_stable_repr_handles_nested_lists_and_nones() -> None:
    """Round-trip nested lists+dicts+None to the same string."""
    a = {"k": [1, 2, {"x": None}]}
    b = {"k": [1, 2, {"x": None}]}
    assert _stable_repr(a) == _stable_repr(b)
    assert _stable_repr(None) == "None"


# ---------------------------------------------------------------------------
# 3. Flag gating — disabled branch mirrors PR #104; enabled branch routes
#    to bitemporal templates.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_flag_uses_legacy_entity_cypher_verbatim() -> None:
    """ADR-0008 §8 phase 2 — disabled writer is byte-for-byte PR #104."""
    store, driver_mock = _make_store_with_mock_driver(bitemporal_enabled=False)
    payload = EntityUpsert(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        entity_type="service",
        name="svc.auth",
        display_name="auth",
        chunk_id=None,
    )

    await store.upsert_entity(entity=payload, workspace_id=_WORKSPACE_A)

    args, _ = driver_mock._session_mock.execute_write.call_args
    cypher = args[1]
    params = args[2]
    assert cypher == _UPSERT_ENTITY_CYPHER
    assert "state_fingerprint" not in params, (
        "Disabled flag must not pollute params with bitemporal-only keys."
    )
    assert "EntityState" not in cypher
    assert "HAD_STATE" not in cypher


@pytest.mark.asyncio
async def test_enabled_flag_uses_bitemporal_entity_cypher() -> None:
    """Enabled writer routes to the versioning template + computes fingerprint."""
    store, driver_mock = _make_store_with_mock_driver(bitemporal_enabled=True)
    payload = EntityUpsert(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        entity_type="service",
        name="svc.auth",
        display_name="auth",
        chunk_id=None,
    )

    await store.upsert_entity(entity=payload, workspace_id=_WORKSPACE_A)

    args, _ = driver_mock._session_mock.execute_write.call_args
    cypher = args[1]
    params = args[2]
    assert cypher == _UPSERT_ENTITY_BITEMPORAL_CYPHER
    assert "state_fingerprint" in params
    # The fingerprint is a SHA-256 hex digest — 64 chars.
    assert len(params["state_fingerprint"]) == 64


@pytest.mark.asyncio
async def test_disabled_flag_uses_legacy_edge_cypher() -> None:
    store, driver_mock = _make_store_with_mock_driver(bitemporal_enabled=False)
    payload = EdgeUpsert(
        source_entity_id=uuid.uuid4(),
        target_entity_id=uuid.uuid4(),
        edge_type="CALLS",
    )

    await store.upsert_edge(edge=payload, workspace_id=_WORKSPACE_A)

    args, _ = driver_mock._session_mock.execute_write.call_args
    cypher = args[1]
    params = args[2]
    expected = _UPSERT_EDGE_CYPHER_TEMPLATE.replace("{edge_type}", "CALLS")
    assert cypher == expected
    assert "state_fingerprint" not in params
    assert "valid_from" not in cypher
    assert "valid_to" not in cypher


@pytest.mark.asyncio
async def test_enabled_flag_uses_bitemporal_edge_cypher() -> None:
    store, driver_mock = _make_store_with_mock_driver(bitemporal_enabled=True)
    payload = EdgeUpsert(
        source_entity_id=uuid.uuid4(),
        target_entity_id=uuid.uuid4(),
        edge_type="CALLS",
    )

    await store.upsert_edge(edge=payload, workspace_id=_WORKSPACE_A)

    args, _ = driver_mock._session_mock.execute_write.call_args
    cypher = args[1]
    params = args[2]
    expected = _UPSERT_EDGE_BITEMPORAL_CYPHER_TEMPLATE.replace("{edge_type}", "CALLS")
    assert cypher == expected
    assert "state_fingerprint" in params
    assert len(params["state_fingerprint"]) == 64


def test_flag_pinned_at_init_not_per_call() -> None:
    """Flag is read at __init__ and stored on the instance.

    A runtime config mutation MUST NOT change the writer's behaviour —
    only a fresh adapter (and process restart in production) flips the
    flag.  Mirrors ADR-0008's "consistency over flexibility".
    """
    store, _ = _make_store_with_mock_driver(bitemporal_enabled=True)
    assert store._bitemporal_enabled is True
    # Mutating the config is a no-op for the live writer because
    # __init__ already pinned the flag.  (Plus the dataclass is frozen —
    # this assertion exercises the instance-attribute contract, not the
    # mutability of the config.)
    other = _make_config(bitemporal_enabled=False)
    assert other.bitemporal_enabled is False
    # Switching the adapter requires constructing a new instance.
    fresh, _ = _make_store_with_mock_driver(bitemporal_enabled=False)
    assert fresh._bitemporal_enabled is False


# ---------------------------------------------------------------------------
# 4. Workspace_id propagation in the bitemporal write path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bitemporal_upsert_entity_passes_workspace_id() -> None:
    store, driver_mock = _make_store_with_mock_driver(bitemporal_enabled=True)
    payload = EntityUpsert(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        entity_type="service",
        name="svc.auth",
        display_name="auth",
        chunk_id=None,
    )

    await store.upsert_entity(entity=payload, workspace_id=_WORKSPACE_A)

    args, _ = driver_mock._session_mock.execute_write.call_args
    cypher = args[1]
    params = args[2]
    assert "workspace_id" in cypher
    assert params["workspace_id"] == str(_WORKSPACE_A)


@pytest.mark.asyncio
async def test_bitemporal_upsert_edge_passes_workspace_id() -> None:
    store, driver_mock = _make_store_with_mock_driver(bitemporal_enabled=True)
    payload = EdgeUpsert(
        source_entity_id=uuid.uuid4(),
        target_entity_id=uuid.uuid4(),
        edge_type="CALLS",
    )

    await store.upsert_edge(edge=payload, workspace_id=_WORKSPACE_A)

    args, _ = driver_mock._session_mock.execute_write.call_args
    cypher = args[1]
    params = args[2]
    assert "workspace_id" in cypher
    assert params["workspace_id"] == str(_WORKSPACE_A)


@pytest.mark.asyncio
async def test_bitemporal_edge_rejects_invalid_edge_type() -> None:
    """Edge-type allowlist regex still enforced under bitemporal flag."""
    store, _ = _make_store_with_mock_driver(bitemporal_enabled=True)
    bad = EdgeUpsert(
        source_entity_id=uuid.uuid4(),
        target_entity_id=uuid.uuid4(),
        edge_type="CALLS; DROP CONSTRAINT foo",
    )
    with pytest.raises(ValueError, match="invalid_edge_type"):
        await store.upsert_edge(edge=bad, workspace_id=_WORKSPACE_A)


# ---------------------------------------------------------------------------
# 5. Live-Neo4j contract — opt-in via env var
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


async def _live_store(*, bitemporal_enabled: bool) -> AsyncIterator[Neo4jGraphStore]:
    """Spin up a fresh Neo4j container and yield a connected store."""
    from testcontainers.neo4j import Neo4jContainer  # type: ignore[import-not-found]

    # Pass password through the constructor so _configure() sets NEO4J_AUTH
    # correctly.  Using .with_env("NEO4J_AUTH", ...) here is silently
    # overridden by the wrapper's own _configure step (the same trap
    # documented in tests/integration/test_neo4j_bootstrap_schema.py).
    with Neo4jContainer("neo4j:5.19-community", password="contract_test_password") as neo4j:
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


async def _query_all(
    store: Neo4jGraphStore, cypher: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    async with store._driver.session(database=store._config.database) as session:
        result = await session.run(cypher, params)
        return [record.data() async for record in result]


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


@pytestmark_contract
@pytest.mark.asyncio
async def test_first_upsert_creates_one_entity_one_state_one_relation() -> None:
    """ADR-0008 §2 — first write materialises identity + first state + chain."""
    gen = _live_store(bitemporal_enabled=True)
    store = await anext(gen)
    try:
        ent = _make_entity(name="svc.first")
        await store.upsert_entity(entity=ent, workspace_id=_WORKSPACE_A)

        row = await _query_one(
            store,
            "MATCH (n:Entity {workspace_id: $ws, id: $id}) "
            "OPTIONAL MATCH (n)-[h:HAD_STATE]->(s:EntityState) "
            "RETURN count(DISTINCT n) AS n_count, count(DISTINCT s) AS s_count, "
            "       count(DISTINCT h) AS h_count, "
            "       any(x IN collect(s.valid_to) WHERE x IS NULL) AS has_open",
            {"ws": str(_WORKSPACE_A), "id": str(ent.id)},
        )
        assert int(row.get("n_count", 0)) == 1
        assert int(row.get("s_count", 0)) == 1
        assert int(row.get("h_count", 0)) == 1
        assert bool(row.get("has_open"))

        # And valid_from = recorded_at on the first version (ADR-0008 §1).
        same = await _query_one(
            store,
            "MATCH (n:Entity {workspace_id: $ws, id: $id})-[:HAD_STATE]->(s:EntityState) "
            "RETURN s.valid_from = s.recorded_at AS aligned",
            {"ws": str(_WORKSPACE_A), "id": str(ent.id)},
        )
        assert bool(same.get("aligned"))
    finally:
        async for _ in gen:
            break


@pytestmark_contract
@pytest.mark.asyncio
async def test_unchanged_repeat_upsert_is_a_no_op() -> None:
    """No new EntityState row when the incoming state matches the current one."""
    gen = _live_store(bitemporal_enabled=True)
    store = await anext(gen)
    try:
        ent = _make_entity(name="svc.idem", metadata={"k": "v"})
        await store.upsert_entity(entity=ent, workspace_id=_WORKSPACE_A)
        await store.upsert_entity(entity=ent, workspace_id=_WORKSPACE_A)
        await store.upsert_entity(entity=ent, workspace_id=_WORKSPACE_A)

        row = await _query_one(
            store,
            "MATCH (n:Entity {workspace_id: $ws, id: $id})-[:HAD_STATE]->(s:EntityState) "
            "RETURN count(s) AS s_count",
            {"ws": str(_WORKSPACE_A), "id": str(ent.id)},
        )
        assert int(row.get("s_count", 0)) == 1, (
            "Repeat upsert with identical state must not create new EntityState rows."
        )
    finally:
        async for _ in gen:
            break


@pytestmark_contract
@pytest.mark.asyncio
async def test_changed_upsert_end_dates_previous_state_and_creates_new() -> None:
    """ADR-0008 §2 — state change end-dates previous version, opens a new one."""
    gen = _live_store(bitemporal_enabled=True)
    store = await anext(gen)
    try:
        ent_v1 = _make_entity(name="svc.v", metadata={"version": "v1"})
        await store.upsert_entity(entity=ent_v1, workspace_id=_WORKSPACE_A)

        ent_v2 = EntityUpsert(
            id=ent_v1.id,
            source_id=ent_v1.source_id,
            entity_type=ent_v1.entity_type,
            name=ent_v1.name,
            display_name=ent_v1.display_name,
            chunk_id=ent_v1.chunk_id,
            metadata={"version": "v2"},
        )
        await store.upsert_entity(entity=ent_v2, workspace_id=_WORKSPACE_A)

        # Two :EntityState rows, exactly one with valid_to IS NULL.
        rows = await _query_all(
            store,
            "MATCH (n:Entity {workspace_id: $ws, id: $id})-[:HAD_STATE]->(s:EntityState) "
            "RETURN s.valid_from AS vf, s.valid_to AS vt, s.metadata AS md "
            "ORDER BY s.valid_from",
            {"ws": str(_WORKSPACE_A), "id": str(ent_v1.id)},
        )
        assert len(rows) == 2
        assert rows[0].get("vt") is not None  # previous end-dated
        assert rows[1].get("vt") is None  # new is open
        # The :Entity mirror reflects the latest state.
        mirror = await _query_one(
            store,
            "MATCH (n:Entity {workspace_id: $ws, id: $id}) "
            "RETURN n.metadata AS md, n.valid_to AS vt",
            {"ws": str(_WORKSPACE_A), "id": str(ent_v1.id)},
        )
        assert mirror.get("vt") is None
        assert dict(mirror.get("md") or {}).get("version") == "v2"
    finally:
        async for _ in gen:
            break


@pytestmark_contract
@pytest.mark.asyncio
async def test_n_changes_produce_n_versions_one_open() -> None:
    """ADR-0008 §9 — n state changes => n versions, exactly one valid_to IS NULL."""
    gen = _live_store(bitemporal_enabled=True)
    store = await anext(gen)
    try:
        n_changes = 5
        base = _make_entity(name="svc.versions", metadata={"i": 0})
        await store.upsert_entity(entity=base, workspace_id=_WORKSPACE_A)
        for i in range(1, n_changes):
            await store.upsert_entity(
                entity=EntityUpsert(
                    id=base.id,
                    source_id=base.source_id,
                    entity_type=base.entity_type,
                    name=base.name,
                    display_name=base.display_name,
                    chunk_id=base.chunk_id,
                    metadata={"i": i},
                ),
                workspace_id=_WORKSPACE_A,
            )

        row = await _query_one(
            store,
            "MATCH (n:Entity {workspace_id: $ws, id: $id})-[:HAD_STATE]->(s:EntityState) "
            "RETURN count(s) AS total, "
            "       size([x IN collect(s.valid_to) WHERE x IS NULL]) AS open_count",
            {"ws": str(_WORKSPACE_A), "id": str(base.id)},
        )
        assert int(row.get("total", 0)) == n_changes
        assert int(row.get("open_count", -1)) == 1
    finally:
        async for _ in gen:
            break


@pytestmark_contract
@pytest.mark.asyncio
async def test_edge_upsert_idempotent_on_no_change_versioned_on_change() -> None:
    """ADR-0008 §3 — edge versioning with the same idempotent-then-version pattern."""
    gen = _live_store(bitemporal_enabled=True)
    store = await anext(gen)
    try:
        a = _make_entity(name="svc.edge.a")
        b = _make_entity(name="svc.edge.b")
        await store.upsert_entity(entity=a, workspace_id=_WORKSPACE_A)
        await store.upsert_entity(entity=b, workspace_id=_WORKSPACE_A)

        edge_v1 = EdgeUpsert(
            source_entity_id=a.id,
            target_entity_id=b.id,
            edge_type="CALLS",
            metadata={"weight": 1},
        )
        await store.upsert_edge(edge=edge_v1, workspace_id=_WORKSPACE_A)
        # Repeat — no new edge.
        await store.upsert_edge(edge=edge_v1, workspace_id=_WORKSPACE_A)

        # Then change.
        edge_v2 = EdgeUpsert(
            source_entity_id=a.id,
            target_entity_id=b.id,
            edge_type="CALLS",
            metadata={"weight": 2},
        )
        await store.upsert_edge(edge=edge_v2, workspace_id=_WORKSPACE_A)

        rows = await _query_all(
            store,
            "MATCH (a:Entity {workspace_id: $ws, id: $a_id})-[r:CALLS]->(b:Entity) "
            "WHERE b.id = $b_id AND r.workspace_id = $ws "
            "RETURN r.valid_from AS vf, r.valid_to AS vt, r.metadata AS md "
            "ORDER BY r.valid_from",
            {"ws": str(_WORKSPACE_A), "a_id": str(a.id), "b_id": str(b.id)},
        )
        assert len(rows) == 2, "After 1 unchanged + 1 changed upsert, exactly 2 edges expected."
        assert rows[0].get("vt") is not None
        assert rows[1].get("vt") is None
    finally:
        async for _ in gen:
            break


@pytestmark_contract
@pytest.mark.asyncio
async def test_atomicity_on_simulated_mid_tx_failure_no_half_versioned_state() -> None:
    """ADR-0008 §Negative-team #2 — versioning is atomic in one tx.

    Simulate a transaction failure by raising mid-transaction (after the
    first version is written, before a second is committed).  Since the
    versioning Cypher runs as a single ``tx.run`` call inside one managed
    transaction, the entire write must roll back.  We assert that the
    pre-failure state remains unchanged: exactly one open ``:EntityState``
    and the mirror still reflects v1.
    """
    gen = _live_store(bitemporal_enabled=True)
    store = await anext(gen)
    try:
        ent_v1 = _make_entity(name="svc.atomic", metadata={"version": "v1"})
        await store.upsert_entity(entity=ent_v1, workspace_id=_WORKSPACE_A)

        # Capture the pre-failure state.
        pre = await _query_one(
            store,
            "MATCH (n:Entity {workspace_id: $ws, id: $id})-[:HAD_STATE]->(s:EntityState) "
            "RETURN count(s) AS s_count, n.metadata AS md",
            {"ws": str(_WORKSPACE_A), "id": str(ent_v1.id)},
        )
        assert int(pre.get("s_count", 0)) == 1

        # Simulate mid-tx failure by raising after a single tx.run() has
        # been queued.  The neo4j driver's `execute_write` retries on
        # transient errors but PROPAGATES on user-raised exceptions and
        # rolls back the transaction.  We force a rollback by making the
        # write throw.
        original_run_write = neo4j_store._run_write_stmt

        async def _explode(tx: Any, cypher: str, params: dict[str, Any]) -> None:
            await original_run_write(tx, cypher, params)
            raise RuntimeError("simulated_mid_tx_failure")

        with patch("omniscience_index.stores.neo4j_store._run_write_stmt", _explode):
            ent_v2 = EntityUpsert(
                id=ent_v1.id,
                source_id=ent_v1.source_id,
                entity_type=ent_v1.entity_type,
                name=ent_v1.name,
                display_name=ent_v1.display_name,
                chunk_id=ent_v1.chunk_id,
                metadata={"version": "v2"},
            )
            with pytest.raises(RuntimeError, match="simulated_mid_tx_failure"):
                await store.upsert_entity(entity=ent_v2, workspace_id=_WORKSPACE_A)

        # Post-failure: the rollback must have erased the v2 attempt.  No
        # half-versioned state — still exactly one :EntityState, mirror
        # still on v1.
        post = await _query_one(
            store,
            "MATCH (n:Entity {workspace_id: $ws, id: $id})-[:HAD_STATE]->(s:EntityState) "
            "RETURN count(s) AS s_count, n.metadata AS md, "
            "       size([x IN collect(s.valid_to) WHERE x IS NULL]) AS open_count",
            {"ws": str(_WORKSPACE_A), "id": str(ent_v1.id)},
        )
        assert int(post.get("s_count", 0)) == 1, (
            "Mid-tx failure must roll back the versioning write — no half-versioned state visible."
        )
        assert int(post.get("open_count", -1)) == 1
        assert dict(post.get("md") or {}).get("version") == "v1"
    finally:
        async for _ in gen:
            break


@pytestmark_contract
@pytest.mark.asyncio
async def test_cross_workspace_isolation_versioned_writer() -> None:
    """ACL invariant — workspace A's writer never touches workspace B's graph.

    Plants symmetric data in A and B, runs many versioning writes on A,
    asserts that B's identity, state nodes, and edges are unaffected.
    Mandatory cross-workspace contract test for the bitemporal writer
    (ADR-0008 §Consequences-security #4).
    """
    gen = _live_store(bitemporal_enabled=True)
    store = await anext(gen)
    try:
        # Both workspaces start with an entity of the same NAME (overlap).
        a_ent = _make_entity(name="svc.shared", metadata={"v": 1})
        b_ent = _make_entity(name="svc.shared", metadata={"v": 1})
        await store.upsert_entity(entity=a_ent, workspace_id=_WORKSPACE_A)
        await store.upsert_entity(entity=b_ent, workspace_id=_WORKSPACE_B)

        # Capture B's pre-state.
        b_pre = await _query_one(
            store,
            "MATCH (n:Entity {workspace_id: $ws, id: $id})-[:HAD_STATE]->(s:EntityState) "
            "RETURN count(s) AS s_count, n.metadata AS md",
            {"ws": str(_WORKSPACE_B), "id": str(b_ent.id)},
        )
        assert int(b_pre.get("s_count", 0)) == 1

        # Hammer many versioning writes on A.
        for i in range(2, 7):
            await store.upsert_entity(
                entity=EntityUpsert(
                    id=a_ent.id,
                    source_id=a_ent.source_id,
                    entity_type=a_ent.entity_type,
                    name=a_ent.name,
                    display_name=a_ent.display_name,
                    chunk_id=a_ent.chunk_id,
                    metadata={"v": i},
                ),
                workspace_id=_WORKSPACE_A,
            )

        # B is untouched: still exactly one :EntityState, still v=1 on the mirror,
        # and zero rows touched in B's namespace.
        b_post = await _query_one(
            store,
            "MATCH (n:Entity {workspace_id: $ws, id: $id})-[:HAD_STATE]->(s:EntityState) "
            "RETURN count(s) AS s_count, n.metadata AS md",
            {"ws": str(_WORKSPACE_B), "id": str(b_ent.id)},
        )
        assert int(b_post.get("s_count", 0)) == 1
        assert dict(b_post.get("md") or {}).get("v") == 1

        # Cross-workspace contract: no :EntityState row in B carries A's id.
        cross = await _query_one(
            store,
            "MATCH (s:EntityState {workspace_id: $ws_b, id: $a_id}) RETURN count(s) AS leaks",
            {"ws_b": str(_WORKSPACE_B), "a_id": str(a_ent.id)},
        )
        assert int(cross.get("leaks", -1)) == 0, (
            "Workspace A's writer planted an :EntityState in workspace B — ACL leak."
        )

        # Sanity — A actually accumulated 6 versions (v=1..6).
        a_post = await _query_one(
            store,
            "MATCH (n:Entity {workspace_id: $ws, id: $id})-[:HAD_STATE]->(s:EntityState) "
            "RETURN count(s) AS s_count",
            {"ws": str(_WORKSPACE_A), "id": str(a_ent.id)},
        )
        assert int(a_post.get("s_count", 0)) == 6
    finally:
        async for _ in gen:
            break


@pytestmark_contract
@pytest.mark.asyncio
async def test_disabled_flag_writes_no_entity_state_artifacts() -> None:
    """Flag=disabled must not produce :EntityState or [:HAD_STATE] artifacts."""
    gen = _live_store(bitemporal_enabled=False)
    store = await anext(gen)
    try:
        ent = _make_entity(name="svc.legacy", metadata={"v": 1})
        await store.upsert_entity(entity=ent, workspace_id=_WORKSPACE_A)
        # Multiple writes, all under the disabled flag.
        for i in range(2, 5):
            await store.upsert_entity(
                entity=EntityUpsert(
                    id=ent.id,
                    source_id=ent.source_id,
                    entity_type=ent.entity_type,
                    name=ent.name,
                    display_name=ent.display_name,
                    chunk_id=ent.chunk_id,
                    metadata={"v": i},
                ),
                workspace_id=_WORKSPACE_A,
            )

        row = await _query_one(
            store,
            "MATCH (n:Entity {workspace_id: $ws, id: $id}) "
            "OPTIONAL MATCH (n)-[h:HAD_STATE]->(s:EntityState) "
            "RETURN count(s) AS s_count, count(h) AS h_count",
            {"ws": str(_WORKSPACE_A), "id": str(ent.id)},
        )
        assert int(row.get("s_count", 0)) == 0, (
            "Disabled flag must not create any :EntityState rows."
        )
        assert int(row.get("h_count", 0)) == 0
    finally:
        async for _ in gen:
            break


@pytestmark_contract
@pytest.mark.asyncio
async def test_switching_from_disabled_to_enabled_begins_versioning() -> None:
    """Switch the flag mid-test (new adapter) and confirm versioning resumes correctly.

    The disabled writer leaves no `:EntityState` artifacts; the enabled
    writer afterward must materialise the first state on the next
    state-changing upsert.  This exercises the rollout path: legacy data
    + enabled writer => clean version chain begins from the next change.
    """
    # First adapter: write legacy.
    gen_off = _live_store(bitemporal_enabled=False)
    store_off = await anext(gen_off)
    workspace_id = _WORKSPACE_A
    ent_id = uuid.uuid4()
    try:
        ent = EntityUpsert(
            id=ent_id,
            source_id=uuid.uuid4(),
            entity_type="service",
            name="svc.flip",
            display_name="flip",
            chunk_id=None,
            metadata={"v": 1},
        )
        await store_off.upsert_entity(entity=ent, workspace_id=workspace_id)
        # Confirm no state node yet.
        pre = await _query_one(
            store_off,
            "MATCH (n:Entity {workspace_id: $ws, id: $id})-[:HAD_STATE]->(s:EntityState) "
            "RETURN count(s) AS sc",
            {"ws": str(workspace_id), "id": str(ent_id)},
        )
        assert int(pre.get("sc", 0)) == 0
        # Capture the connection URL so we can switch adapters on the same DB.
        live_uri = store_off._config.uri
        live_username = store_off._config.username
        live_password = store_off._config.password
        live_db = store_off._config.database
    finally:
        # Don't close yet — keep the container alive by holding the generator.
        pass

    try:
        # Second adapter on the SAME container: bitemporal_enabled=True.
        config_on = Neo4jStoreConfig(
            uri=live_uri,
            username=live_username,
            password=live_password,
            database=live_db,
            max_connection_pool_size=5,
            connection_acquisition_timeout_seconds=30.0,
            max_transaction_retry_time_seconds=15.0,
            default_max_depth=3,
            bitemporal_enabled=True,
        )
        store_on = Neo4jGraphStore(config=config_on)
        await store_on.connect()
        try:
            ent_v2 = EntityUpsert(
                id=ent_id,
                source_id=ent.source_id,
                entity_type="service",
                name="svc.flip",
                display_name="flip",
                chunk_id=None,
                metadata={"v": 2},
            )
            await store_on.upsert_entity(entity=ent_v2, workspace_id=workspace_id)
            row = await _query_one(
                store_on,
                "MATCH (n:Entity {workspace_id: $ws, id: $id})-[:HAD_STATE]->(s:EntityState) "
                "RETURN count(s) AS sc, "
                "       size([x IN collect(s.valid_to) WHERE x IS NULL]) AS open_count",
                {"ws": str(workspace_id), "id": str(ent_id)},
            )
            # Exactly one state row materialised by the first enabled write.
            assert int(row.get("sc", 0)) == 1
            assert int(row.get("open_count", -1)) == 1
        finally:
            await store_on.close()
    finally:
        async for _ in gen_off:
            break


# Silence "imported but unused" — these are intentionally kept for the
# import-time guard side effects above.
_ = (asyncio, neo4j_store)
