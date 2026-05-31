"""Bitemporal backfill tests for :class:`Neo4jGraphStore` (issue #130).

Covers ADR-0008 §8 phase 1 — the operator-driven backfill that
populates the bitemporal triple on legacy `:Entity` and edge rows
that pre-date the schema.

Two layers
----------

1. **Unit-level ACL guards** (run unconditionally) — assert that
   every backfill template references ``workspace_id`` (the
   `_ensure_workspace_predicate` import-time guard already enforces
   this; the assertions here document the contract for reviewers).

2. **Live-Neo4j idempotence + workspace-scoping contract** (opt-in
   via ``OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1``) — plant a small
   pre-bitemporal graph in workspaces A and B, run the backfill on
   workspace A only, assert:

   - Every node and every edge in workspace A carries the bitemporal
     triple with `valid_to IS NULL` (ADR-0008 §1 sentinel).
   - Every `:Entity` in workspace A has exactly one
     `(:EntityState)` reachable via `[:HAD_STATE]`.
   - Every node and every edge in workspace B is **untouched** —
     `valid_from`, `valid_to`, `recorded_at` remain absent and no
     `:EntityState` row exists for any B-side identity.
   - A second invocation of `backfill_bitemporal(workspace_id=A)`
     modifies zero rows (idempotence).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from omniscience_index.stores import neo4j_store
from omniscience_index.stores.neo4j_store import (
    _BACKFILL_EDGE_PROPS_CYPHER,
    _BACKFILL_ENTITY_PROPS_CYPHER,
    _BACKFILL_ENTITY_STATE_NODES_CYPHER,
    BACKFILL_DEFAULT_BATCH_SIZE,
    Neo4jGraphStore,
    Neo4jStoreConfig,
)

_WORKSPACE_A = uuid.UUID("aaaaaaaa-0000-0000-0000-0000000000a1")
_WORKSPACE_B = uuid.UUID("bbbbbbbb-0000-0000-0000-0000000000b2")


# ---------------------------------------------------------------------------
# 1. Pure-Python lints
# ---------------------------------------------------------------------------


def test_every_backfill_template_references_workspace_id() -> None:
    """ACL invariant — every backfill MATCH is workspace-scoped.

    Mirrors the import-time `_ensure_workspace_predicate` guard.  This
    test exists so a reviewer who looks here sees the contract spelled
    out at the assertion level too.
    """
    templates = {
        "_BACKFILL_ENTITY_PROPS_CYPHER": _BACKFILL_ENTITY_PROPS_CYPHER,
        "_BACKFILL_ENTITY_STATE_NODES_CYPHER": _BACKFILL_ENTITY_STATE_NODES_CYPHER,
        "_BACKFILL_EDGE_PROPS_CYPHER": _BACKFILL_EDGE_PROPS_CYPHER,
    }
    for name, cypher in templates.items():
        assert "workspace_id" in cypher, (
            f"Backfill template '{name}' lost workspace_id predicate — "
            "ADR-0008 §Consequences-security #1."
        )
        # The query must take it as a parameter, never as a literal.
        assert "$workspace_id" in cypher, (
            f"Backfill template '{name}' must parameterize workspace_id."
        )


def test_every_backfill_template_is_chunked() -> None:
    """ADR-0008 §Risks — backfill must be chunked, never `MATCH (n) SET ...`."""
    for cypher in (
        _BACKFILL_ENTITY_PROPS_CYPHER,
        _BACKFILL_ENTITY_STATE_NODES_CYPHER,
        _BACKFILL_EDGE_PROPS_CYPHER,
    ):
        assert "$batch_size" in cypher, (
            "Backfill template missing $batch_size — would lock the entire graph."
        )
        assert "LIMIT" in cypher, "Backfill template missing LIMIT — chunked loop required."


def test_every_backfill_template_has_idempotence_guard() -> None:
    """ADR-0008 §8 — every SET must be guarded so re-runs are no-ops."""
    # Phase 1a: guarded by `WHERE n.valid_from IS NULL`.
    assert "valid_from IS NULL" in _BACKFILL_ENTITY_PROPS_CYPHER
    # Phase 1c: same shape on edges.
    assert "valid_from IS NULL" in _BACKFILL_EDGE_PROPS_CYPHER
    # Phase 1b: guarded by NOT exists `[:HAD_STATE]->(:EntityState)` and a
    # MERGE on the (workspace_id, id, valid_from) uniqueness key — the
    # MERGE provides the idempotence; re-runs find the same row.
    assert "NOT (n)-[:HAD_STATE]" in _BACKFILL_ENTITY_STATE_NODES_CYPHER
    assert "MERGE (s:EntityState" in _BACKFILL_ENTITY_STATE_NODES_CYPHER


def test_default_batch_size_is_named_constant() -> None:
    """No magic numbers — batch size is module-level with rationale."""
    assert BACKFILL_DEFAULT_BATCH_SIZE > 0
    # Defensive lower bound — anything below 50 is a regression
    # (per-batch overhead dominates real wall time).
    assert BACKFILL_DEFAULT_BATCH_SIZE >= 50


# ---------------------------------------------------------------------------
# 2. Live-Neo4j contract — opt-in via env var
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


async def _live_store() -> AsyncIterator[Neo4jGraphStore]:
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
        )
        store = Neo4jGraphStore(config=config)
        await store.connect()
        try:
            yield store
        finally:
            await store.close()


async def _plant_legacy_entity(
    store: Neo4jGraphStore,
    *,
    workspace_id: uuid.UUID,
    name: str,
) -> str:
    """Insert an `:Entity` carrying only the legacy property triple.

    Returns the inserted node's id (string).  Bypasses the public
    upsert path so we plant rows that look exactly like pre-#130
    data — `created_at` / `updated_at` only, no `valid_*`/`recorded_at`.
    """
    entity_id = str(uuid.uuid4())
    source_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    cypher = """
    CREATE (n:Entity {
        workspace_id: $workspace_id,
        id: $id,
        source_id: $source_id,
        kind: 'service',
        name: $name,
        display_name: $name,
        chunk_id: null,
        metadata: {},
        created_at: $now,
        updated_at: $now
    })
    """
    async with store._driver.session(database=store._config.database) as session:
        await session.run(
            cypher,
            {
                "workspace_id": str(workspace_id),
                "id": entity_id,
                "source_id": source_id,
                "name": name,
                "now": now,
            },
        )
    return entity_id


async def _plant_legacy_edge(
    store: Neo4jGraphStore,
    *,
    workspace_id: uuid.UUID,
    source_entity_id: str,
    target_entity_id: str,
) -> None:
    """Plant a relationship with only legacy `created_at` / `updated_at`."""
    now = datetime.now(UTC).isoformat()
    cypher = """
    MATCH (a:Entity {workspace_id: $workspace_id, id: $source_id_ent})
    MATCH (b:Entity {workspace_id: $workspace_id, id: $target_id_ent})
    CREATE (a)-[:CALLS {
        workspace_id: $workspace_id,
        source_id: $source_id,
        edge_type: 'CALLS',
        metadata: {},
        created_at: $now,
        updated_at: $now
    }]->(b)
    """
    async with store._driver.session(database=store._config.database) as session:
        await session.run(
            cypher,
            {
                "workspace_id": str(workspace_id),
                "source_id_ent": source_entity_id,
                "target_id_ent": target_entity_id,
                "source_id": str(uuid.uuid4()),
                "now": now,
            },
        )


async def _query_one(
    store: Neo4jGraphStore, cypher: str, params: dict[str, Any]
) -> dict[str, Any]:
    async with store._driver.session(database=store._config.database) as session:
        result = await session.run(cypher, params)
        rows = [record.data() async for record in result]
    return rows[0] if rows else {}


@pytestmark_contract
@pytest.mark.asyncio
async def test_backfill_populates_bitemporal_triple_on_legacy_entities() -> None:
    """Phase 1a: legacy `:Entity` rows get `valid_from`, `valid_to=NULL`, `recorded_at`."""
    gen = _live_store()
    store = await anext(gen)
    try:
        a_id = await _plant_legacy_entity(store, workspace_id=_WORKSPACE_A, name="svc.a1")

        modified = await store.backfill_bitemporal(workspace_id=_WORKSPACE_A)

        assert modified > 0
        row = await _query_one(
            store,
            "MATCH (n:Entity {workspace_id: $ws, id: $id}) "
            "RETURN n.valid_from AS vf, n.valid_to AS vt, n.recorded_at AS ra",
            {"ws": str(_WORKSPACE_A), "id": a_id},
        )
        assert row.get("vf") is not None
        assert row.get("vt") is None  # ADR-0008 §1 still-valid sentinel
        assert row.get("ra") is not None
    finally:
        async for _ in gen:
            break


@pytestmark_contract
@pytest.mark.asyncio
async def test_backfill_creates_initial_entity_state_node_per_identity() -> None:
    """Phase 1b: every `:Entity` gets exactly one `[:HAD_STATE]->(:EntityState)`."""
    gen = _live_store()
    store = await anext(gen)
    try:
        a_id = await _plant_legacy_entity(store, workspace_id=_WORKSPACE_A, name="svc.a1")

        await store.backfill_bitemporal(workspace_id=_WORKSPACE_A)

        row = await _query_one(
            store,
            "MATCH (n:Entity {workspace_id: $ws, id: $id})-[:HAD_STATE]->(s:EntityState) "
            "RETURN count(s) AS state_count, "
            "       any(x IN collect(s.valid_to) WHERE x IS NULL) AS has_open_state",
            {"ws": str(_WORKSPACE_A), "id": a_id},
        )
        assert int(row.get("state_count", 0)) == 1
        assert bool(row.get("has_open_state"))
    finally:
        async for _ in gen:
            break


@pytestmark_contract
@pytest.mark.asyncio
async def test_backfill_populates_bitemporal_triple_on_legacy_edges() -> None:
    """Phase 1c: legacy edges get `valid_from`, `valid_to=NULL`, `recorded_at`."""
    gen = _live_store()
    store = await anext(gen)
    try:
        a_id = await _plant_legacy_entity(store, workspace_id=_WORKSPACE_A, name="svc.a1")
        b_id = await _plant_legacy_entity(store, workspace_id=_WORKSPACE_A, name="svc.a2")
        await _plant_legacy_edge(
            store,
            workspace_id=_WORKSPACE_A,
            source_entity_id=a_id,
            target_entity_id=b_id,
        )

        await store.backfill_bitemporal(workspace_id=_WORKSPACE_A)

        row = await _query_one(
            store,
            "MATCH ()-[r:CALLS]->() WHERE r.workspace_id = $ws "
            "RETURN r.valid_from AS vf, r.valid_to AS vt, r.recorded_at AS ra",
            {"ws": str(_WORKSPACE_A)},
        )
        assert row.get("vf") is not None
        assert row.get("vt") is None
        assert row.get("ra") is not None
    finally:
        async for _ in gen:
            break


@pytestmark_contract
@pytest.mark.asyncio
async def test_backfill_is_idempotent_second_run_modifies_zero_rows() -> None:
    """ADR-0008 §8 — re-runs are no-ops on already-migrated rows."""
    gen = _live_store()
    store = await anext(gen)
    try:
        a_id = await _plant_legacy_entity(store, workspace_id=_WORKSPACE_A, name="svc.a1")
        b_id = await _plant_legacy_entity(store, workspace_id=_WORKSPACE_A, name="svc.a2")
        await _plant_legacy_edge(
            store,
            workspace_id=_WORKSPACE_A,
            source_entity_id=a_id,
            target_entity_id=b_id,
        )

        first_pass = await store.backfill_bitemporal(workspace_id=_WORKSPACE_A)
        second_pass = await store.backfill_bitemporal(workspace_id=_WORKSPACE_A)

        assert first_pass > 0
        assert second_pass == 0, (
            "Backfill is not idempotent — second run modified rows.  "
            "Check the IS NULL guards in _BACKFILL_*_CYPHER."
        )
    finally:
        async for _ in gen:
            break


@pytestmark_contract
@pytest.mark.asyncio
async def test_backfill_workspace_scoped_does_not_touch_other_workspace() -> None:
    """ACL invariant — A's backfill leaves B's nodes/edges untouched.

    This is the cross-workspace isolation regression test mandated by
    ADR-0008 §Consequences-security #4.  Plants symmetric data in
    workspaces A and B, runs the backfill on A only, and asserts B's
    rows still look pre-bitemporal.
    """
    gen = _live_store()
    store = await anext(gen)
    try:
        # A: two entities + one edge.
        a1 = await _plant_legacy_entity(store, workspace_id=_WORKSPACE_A, name="svc.shared")
        a2 = await _plant_legacy_entity(store, workspace_id=_WORKSPACE_A, name="svc.internal-a")
        await _plant_legacy_edge(
            store, workspace_id=_WORKSPACE_A, source_entity_id=a1, target_entity_id=a2
        )
        # B: two entities + one edge with the same names (overlap).
        b1 = await _plant_legacy_entity(store, workspace_id=_WORKSPACE_B, name="svc.shared")
        b2 = await _plant_legacy_entity(store, workspace_id=_WORKSPACE_B, name="svc.internal-b")
        await _plant_legacy_edge(
            store, workspace_id=_WORKSPACE_B, source_entity_id=b1, target_entity_id=b2
        )

        # Run backfill ONLY on workspace A.
        await store.backfill_bitemporal(workspace_id=_WORKSPACE_A)

        # Workspace B nodes — bitemporal triple still absent.
        b_node_row = await _query_one(
            store,
            "MATCH (n:Entity {workspace_id: $ws}) "
            "RETURN count(n) AS total, "
            "       count(n.valid_from) AS with_vf, "
            "       count(n.recorded_at) AS with_ra",
            {"ws": str(_WORKSPACE_B)},
        )
        assert int(b_node_row.get("total", 0)) == 2
        assert int(b_node_row.get("with_vf", -1)) == 0, (
            "Workspace B nodes were touched by workspace A's backfill — ACL leak."
        )
        assert int(b_node_row.get("with_ra", -1)) == 0

        # Workspace B edges — bitemporal triple still absent.
        b_edge_row = await _query_one(
            store,
            "MATCH ()-[r:CALLS]->() WHERE r.workspace_id = $ws "
            "RETURN count(r) AS total, count(r.valid_from) AS with_vf",
            {"ws": str(_WORKSPACE_B)},
        )
        assert int(b_edge_row.get("total", 0)) == 1
        assert int(b_edge_row.get("with_vf", -1)) == 0

        # Workspace B identity nodes — no `[:HAD_STATE]->(:EntityState)` chain.
        b_state_row = await _query_one(
            store,
            "MATCH (n:Entity {workspace_id: $ws})-[:HAD_STATE]->(s:EntityState) "
            "RETURN count(s) AS total",
            {"ws": str(_WORKSPACE_B)},
        )
        assert int(b_state_row.get("total", 0)) == 0, (
            "Workspace A's backfill created EntityState rows in workspace B — ACL leak."
        )

        # Sanity — workspace A IS migrated (the positive side of the contract).
        a_node_row = await _query_one(
            store,
            "MATCH (n:Entity {workspace_id: $ws}) RETURN count(n.valid_from) AS with_vf",
            {"ws": str(_WORKSPACE_A)},
        )
        assert int(a_node_row.get("with_vf", 0)) == 2
    finally:
        async for _ in gen:
            break


@pytestmark_contract
@pytest.mark.asyncio
async def test_backfill_does_not_change_existing_writer_round_trip() -> None:
    """Pre-bitemporal data round-trips through `upsert_entity`/`upsert_edge` unchanged.

    Acceptance criterion from #130: "Pre-bitemporal data must continue
    to round-trip through `upsert_entity` / `upsert_edge` / `find_related`
    unchanged."  The schema additions are non-breaking — the writer
    still produces nodes that satisfy the existing constraints, and
    `find_related` still resolves seeds and traversals.
    """
    from omniscience_core.storage.graph import EdgeUpsert, EntityUpsert

    gen = _live_store()
    store = await anext(gen)
    try:
        seed = EntityUpsert(
            id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            entity_type="service",
            name="svc.seed",
            display_name="seed",
            chunk_id=None,
        )
        neighbour = EntityUpsert(
            id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            entity_type="service",
            name="svc.neighbour",
            display_name="neighbour",
            chunk_id=None,
        )
        await store.upsert_entity(entity=seed, workspace_id=_WORKSPACE_A)
        await store.upsert_entity(entity=neighbour, workspace_id=_WORKSPACE_A)
        await store.upsert_edge(
            edge=EdgeUpsert(
                source_entity_id=seed.id,
                target_entity_id=neighbour.id,
                edge_type="CALLS",
            ),
            workspace_id=_WORKSPACE_A,
        )

        result = await store.find_related(
            entity_name="svc.seed", workspace_id=_WORKSPACE_A, max_depth=1
        )
        assert result.seed.name == "svc.seed"
        related_names = {n.name for n in result.related}
        assert "svc.neighbour" in related_names
    finally:
        async for _ in gen:
            break


@pytestmark_contract
@pytest.mark.asyncio
async def test_backfill_rejects_zero_or_negative_batch_size() -> None:
    """Validate explicit caller error on bad input, not silent infinite loop."""
    gen = _live_store()
    store = await anext(gen)
    try:
        with pytest.raises(ValueError, match="backfill_batch_size_must_be_positive"):
            await store.backfill_bitemporal(workspace_id=_WORKSPACE_A, batch_size=0)
    finally:
        async for _ in gen:
            break


_ = neo4j_store  # silence "imported but unused" — module-level guards run
