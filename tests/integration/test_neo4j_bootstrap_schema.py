"""End-to-end bootstrap test for :class:`Neo4jGraphStore` (issue #224 / ADR-0012).

This is the regression gate for the bootstrap-DDL bug fixed in issue #224.

Background
----------

The original implementation in ``packages/index/src/omniscience_index/stores/
neo4j_store.py`` declared four relationship-property indexes with the untyped
pattern ``CREATE INDEX ... FOR ()-[r]-() ON (r.prop)``.  Neo4j 5.x rejects
that syntax at parse time — a specific relationship type is required.  The
bug shipped because the existing unit tests (``test_neo4j_store_bitemporal_
schema.py``) only string-grepped for the index name in ``_BOOTSTRAP_STATEMENTS``
and never executed the DDL.  See ADR-0012 for the design decision.

Scope
-----

1. Spin up a fresh ``neo4j:5.19-community`` testcontainer.
2. Call ``Neo4jGraphStore.connect()`` against an empty database — the
   static bootstrap DDL must succeed and the dynamic per-type phase
   must be a no-op (no relationships exist yet).
3. Plant two relationships of different types via direct Cypher (NOT
   the writer — see note below).
4. Call ``_bootstrap_schema()`` again — the dynamic phase must now
   create four per-type indexes for each of the two types.
5. Assert via ``SHOW INDEXES`` that every expected per-type index
   exists with the expected shape.

Why direct Cypher for edge planting?
------------------------------------

This test is the regression gate for the **bootstrap path**, not for the
writer.  Planting edges via ``Neo4jGraphStore.upsert_edge`` would couple
this test to writer-path concerns (e.g., the existing latent issue with
``metadata`` being passed as a Map property — Neo4j 5.x rejects non-
primitive property values).  Direct Cypher keeps the test focused on the
DDL surface the bootstrap exercises.

Gate
----

This test runs WITHOUT the ``OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS`` opt-in
env var so it executes on every PR — that gate is precisely what allowed
the original bug to ship in PR #104 and stay un-noticed for a month.  The
only skip condition is "testcontainers + Docker unavailable", which is a
hard environmental constraint, not an opt-in.

Runtime expectations
--------------------

* The Neo4j 5.19-community image pull dominates first-run wall-clock
  (~minute on a cold cache).  Subsequent runs reuse the image and the
  test completes in roughly 20-40 seconds (one container per test,
  five tests).
* The Docker layer cache is shared across the existing
  ``test_graph_store_contract.py`` and ``test_neo4j_store_bitemporal_*``
  tests, so adding this test only widens coverage, not image pulls.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

# Hard skip when Docker / testcontainers-neo4j is unavailable — the test
# cannot run otherwise.  We do NOT gate behind
# OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1 because that gate is what let
# the original issue #224 bug ship.
pytest.importorskip("testcontainers.neo4j")

from omniscience_index.stores.neo4j_store import (
    _EDGE_INDEX_TEMPLATES,
    Neo4jGraphStore,
    Neo4jStoreConfig,
    _edge_index_name,
    _run_write_stmt,
)


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not _docker_available(),
        reason="Docker is not reachable; Neo4j contract tests need testcontainers.",
    ),
]


_NEO4J_PASSWORD = "issue_224_password"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def neo4j_store() -> AsyncIterator[Neo4jGraphStore]:
    """Spin up a fresh Neo4j 5.19-community container and connect to it.

    Per-test container — the test is destructive (creates indexes and
    plants edges) and the bootstrap behaviour we exercise is sensitive to
    the live ``db.relationshipTypes()`` set, so isolation matters.
    """
    from testcontainers.neo4j import Neo4jContainer  # type: ignore[import-not-found]

    # Pass password through the constructor — the Neo4jContainer wrapper
    # writes it into NEO4J_AUTH inside `_configure` and threads it back
    # into `get_driver()`.  Using `.with_env("NEO4J_AUTH", ...)` here
    # would be overridden by the wrapper's own `_configure` step.
    container = Neo4jContainer("neo4j:5.19-community", password=_NEO4J_PASSWORD)
    with container:
        config = Neo4jStoreConfig(
            uri=container.get_connection_url(),
            username="neo4j",
            password=_NEO4J_PASSWORD,
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _show_index_names(store: Neo4jGraphStore) -> set[str]:
    """Return the set of all index names in the live database."""
    async with store._driver.session(database=store._config.database) as session:
        result = await session.run("SHOW INDEXES YIELD name")
        return {record["name"] async for record in result}


async def _plant_edge(store: Neo4jGraphStore, *, edge_type: str) -> None:
    """Plant a single edge of ``edge_type`` via direct Cypher.

    Intentionally bypasses ``Neo4jGraphStore.upsert_edge`` — see module
    docstring.  Only primitive properties are set so the Bolt driver
    serialisation never trips on a Map value.
    """
    cypher = (
        "MERGE (a:Entity {workspace_id: $workspace_id, id: $a_id, name: $a_name}) "
        "MERGE (b:Entity {workspace_id: $workspace_id, id: $b_id, name: $b_name}) "
        f"MERGE (a)-[r:`{edge_type}` {{workspace_id: $workspace_id}}]->(b) "
        "ON CREATE SET r.edge_type = $edge_type, r.source_id = $source_id"
    )
    params = {
        "workspace_id": "22a4-test-workspace",
        "a_id": f"{edge_type}-a",
        "a_name": f"svc.{edge_type}.a",
        "b_id": f"{edge_type}-b",
        "b_name": f"svc.{edge_type}.b",
        "edge_type": edge_type,
        "source_id": f"src-{edge_type}",
    }
    async with store._driver.session(database=store._config.database) as session:
        await session.execute_write(_run_write_stmt, cypher, params)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_bootstrap_completes_on_empty_database(neo4j_store: Neo4jGraphStore) -> None:
    """Cold-start regression gate for issue #224.

    The fixture already invoked ``connect()`` against an empty database.
    If the bootstrap raises a ``CypherSyntaxError`` (the original bug),
    the fixture itself fails and we never reach this test body.  Reaching
    here is the assertion; we also sanity-check the static node-label
    indexes are present.
    """
    names = await _show_index_names(neo4j_store)
    # ADR-0005 + ADR-0008 §4 — static node-label indexes that must always
    # exist after bootstrap, regardless of the relationship-type set.
    expected_static = {
        "entity_workspace_kind",
        "entity_workspace_name",
        "entity_source_id",
        "entity_workspace_recorded_at",
        "entity_state_workspace_valid_window",
        "entity_state_workspace_recorded_at",
    }
    missing = expected_static - names
    assert not missing, f"Static bootstrap is missing indexes: {missing}"


async def test_bootstrap_empty_db_creates_no_relationship_indexes(
    neo4j_store: Neo4jGraphStore,
) -> None:
    """Dynamic phase is a no-op on a database with no relationships.

    No relationships means ``db.relationshipTypes()`` returns the empty
    set, so no per-type CREATE INDEX statements are issued.  This is
    correct: there is nothing to scan and therefore nothing to optimise.
    """
    names = await _show_index_names(neo4j_store)
    for prefix, _ in _EDGE_INDEX_TEMPLATES:
        # No name starting with `<prefix>__` should exist yet.
        for name in names:
            assert not name.startswith(f"{prefix}__"), (
                f"Unexpected per-type relationship index '{name}' present "
                "on empty DB — bootstrap should not pre-create per-type "
                "indexes for types that do not exist."
            )


async def test_bootstrap_creates_per_type_indexes_after_edges_exist(
    neo4j_store: Neo4jGraphStore,
) -> None:
    """The headline regression test — bootstrap covers every live rel type.

    Plant edges of two distinct types, re-run ``_bootstrap_schema()``,
    then assert that all four per-type indexes from ``_EDGE_INDEX_TEMPLATES``
    exist for each planted type.
    """
    planted_types = ("CALLS", "DEPENDS_ON")
    for edge_type in planted_types:
        await _plant_edge(neo4j_store, edge_type=edge_type)

    # Re-run bootstrap — this is the path that picks up the new types.
    await neo4j_store._bootstrap_schema()

    names = await _show_index_names(neo4j_store)
    expected_per_type: set[str] = set()
    for rel_type in planted_types:
        for prefix, _ in _EDGE_INDEX_TEMPLATES:
            expected_per_type.add(_edge_index_name(prefix, rel_type))

    missing = expected_per_type - names
    assert not missing, f"Bootstrap failed to create per-type relationship indexes: {missing}"


async def test_bootstrap_per_type_indexes_have_correct_shape(
    neo4j_store: Neo4jGraphStore,
) -> None:
    """Each per-type index must be on the expected relationship type + properties.

    Asserts via ``SHOW INDEXES YIELD name, entityType, labelsOrTypes,
    properties`` that the index is a RELATIONSHIP index on exactly the
    planted type and that the indexed properties match the template.
    """
    await _plant_edge(neo4j_store, edge_type="INHERITS")
    await neo4j_store._bootstrap_schema()

    async with neo4j_store._driver.session(database=neo4j_store._config.database) as session:
        result = await session.run(
            "SHOW INDEXES YIELD name, entityType, labelsOrTypes, properties"
        )
        rows = [record.data() async for record in result]

    by_name = {row["name"]: row for row in rows}
    expected_properties = {
        "edge_workspace_id": ["workspace_id"],
        "edge_source_id": ["source_id"],
        "edge_workspace_valid_window": ["workspace_id", "valid_from", "valid_to"],
        "edge_workspace_recorded_at": ["workspace_id", "recorded_at"],
    }
    for prefix, expected_props in expected_properties.items():
        name = _edge_index_name(prefix, "INHERITS")
        assert name in by_name, f"Missing per-type index '{name}'"
        row = by_name[name]
        assert row["entityType"] == "RELATIONSHIP", (
            f"Index '{name}' has entityType={row['entityType']!r}, expected 'RELATIONSHIP'"
        )
        assert row["labelsOrTypes"] == ["INHERITS"], (
            f"Index '{name}' covers types {row['labelsOrTypes']!r}, expected ['INHERITS']"
        )
        assert row["properties"] == expected_props, (
            f"Index '{name}' covers properties {row['properties']!r}, expected {expected_props!r}"
        )


async def test_bootstrap_is_idempotent_on_warm_database(
    neo4j_store: Neo4jGraphStore,
) -> None:
    """Re-running bootstrap on a populated DB must not raise.

    Every CREATE statement uses ``IF NOT EXISTS`` so the second invocation
    is a no-op at the schema-cache level.  This guards against a refactor
    that drops ``IF NOT EXISTS`` from one of the dynamic templates.
    """
    await _plant_edge(neo4j_store, edge_type="MENTIONS")
    # First pass — populates per-type indexes.
    await neo4j_store._bootstrap_schema()
    names_after_first = await _show_index_names(neo4j_store)

    # Second pass — must not raise, must not change the index set.
    await neo4j_store._bootstrap_schema()
    names_after_second = await _show_index_names(neo4j_store)

    assert names_after_first == names_after_second, (
        "Re-running bootstrap changed the index set — IF NOT EXISTS "
        "is missing somewhere in the dynamic templates."
    )
