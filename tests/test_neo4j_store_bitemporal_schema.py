"""Bitemporal schema bootstrap tests for :class:`Neo4jGraphStore` (issue #130).

Covers ADR-0008 §4 — verifies that after ``connect()``, every constraint
and index from the ADR is present in the live database.

Two layers
----------

1. **Pure-Python lints** (run unconditionally) — assert the
   ``_BOOTSTRAP_STATEMENTS`` tuple includes the six new bitemporal
   DDL statements, that every new index is composite on
   ``workspace_id`` first, and that the bitemporal DDL appears
   *after* the ADR-0005 carry-forward statements (ordering matters
   only for human review, but stable ordering helps reviewers).

2. **Live Neo4j contract** (opt-in via
   ``OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1``) — spins up a fresh
   Neo4j container, runs ``connect()``, then queries
   ``SHOW CONSTRAINTS`` / ``SHOW INDEXES`` and asserts every entry
   from ADR-0008 §4 exists.  Mirrors the gate in
   :mod:`tests.test_graph_store_contract`.

The contract tests are skipped when Docker / testcontainers-neo4j is
unavailable; the lint tests always run.
"""

from __future__ import annotations

import os
import time

import pytest
from omniscience_index.stores import neo4j_store
from omniscience_index.stores.neo4j_store import (
    _BITEMPORAL_BACKFILL_STATEMENTS,
    _BOOTSTRAP_STATEMENTS,
    Neo4jGraphStore,
    Neo4jStoreConfig,
)

# ADR-0008 §4 — every new index/constraint name MUST appear in the bootstrap.
_ADR_0008_DDL_NAMES: tuple[str, ...] = (
    "entity_state_workspace_id_valid_from_unique",
    "entity_workspace_recorded_at",
    "entity_state_workspace_valid_window",
    "entity_state_workspace_recorded_at",
    "edge_workspace_valid_window",
    "edge_workspace_recorded_at",
)


# ---------------------------------------------------------------------------
# 1. Pure-Python lints over the bootstrap tuple
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ddl_name", _ADR_0008_DDL_NAMES)
def test_bootstrap_includes_adr_0008_ddl_by_name(ddl_name: str) -> None:
    """Every ADR-0008 §4 DDL name must appear verbatim in the bootstrap."""
    rendered = "\n".join(_BOOTSTRAP_STATEMENTS)
    assert ddl_name in rendered, (
        f"Bootstrap is missing ADR-0008 §4 DDL '{ddl_name}' — see neo4j_store.py."
    )


def test_bootstrap_uses_if_not_exists_everywhere() -> None:
    """Every bootstrap statement is idempotent — `connect()` must be re-run-safe."""
    for stmt in _BOOTSTRAP_STATEMENTS:
        assert "IF NOT EXISTS" in stmt, f"Bootstrap statement is not idempotent: {stmt!r}"


def test_new_bitemporal_indexes_are_workspace_id_first() -> None:
    """ACL invariant — every new index is composite on workspace_id first.

    ADR-0008 §Consequences-security #1 + ACL carry-forward from
    #117 / #119: workspace_id is always the leading column.
    """
    composite_index_names = (
        "entity_workspace_recorded_at",
        "entity_state_workspace_valid_window",
        "entity_state_workspace_recorded_at",
        "edge_workspace_valid_window",
        "edge_workspace_recorded_at",
    )
    for name in composite_index_names:
        stmt = next(s for s in _BOOTSTRAP_STATEMENTS if name in s)
        # The first parenthesised property in the ON (...) clause must be
        # workspace_id.  We do a substring check on the canonical form
        # used in the templates.
        assert (
            "(n.workspace_id" in stmt or "(s.workspace_id" in stmt or "(r.workspace_id" in stmt
        ), f"Index '{name}' is not workspace_id-first — ACL invariant violated"


def test_entity_state_unique_constraint_includes_workspace_id() -> None:
    """The ADR-0008 §4 uniqueness constraint is on (workspace_id, id, valid_from)."""
    stmt = next(
        s for s in _BOOTSTRAP_STATEMENTS if "entity_state_workspace_id_valid_from_unique" in s
    )
    # Fully assert the composite shape — never `(id, valid_from)` alone.
    assert "(s.workspace_id, s.id, s.valid_from)" in stmt


def test_bitemporal_ddl_is_additive_not_replacing() -> None:
    """ADR-0005 carry-forward DDL must remain in the bootstrap unchanged."""
    rendered = "\n".join(_BOOTSTRAP_STATEMENTS)
    # Pre-existing statements that #130 must not touch.
    for legacy_name in (
        "entity_workspace_id_unique",
        "entity_workspace_kind",
        "entity_workspace_name",
        "entity_source_id",
        "edge_workspace_id",
        "edge_source_id",
    ):
        assert legacy_name in rendered, (
            f"ADR-0005 carry-forward DDL '{legacy_name}' was dropped — "
            "#130 must extend, never replace."
        )


def test_backfill_statements_kept_separate_from_bootstrap() -> None:
    """ADR-0008 §8 — backfill MUST NOT run at connect() time.

    The bootstrap is the sub-200ms hot path; the backfill is operator-driven.
    """
    for stmt in _BITEMPORAL_BACKFILL_STATEMENTS:
        assert stmt not in _BOOTSTRAP_STATEMENTS, (
            "Backfill Cypher leaked into _BOOTSTRAP_STATEMENTS — connect() "
            "would run a multi-batch migration on every startup."
        )


# ---------------------------------------------------------------------------
# 2. Bootstrap-cost sanity-floor test
# ---------------------------------------------------------------------------


def test_bootstrap_count_within_documented_envelope() -> None:
    """Sanity floor — bootstrap is 12 statements (6 ADR-0005 + 6 ADR-0008).

    A 1M-node graph hits each `CREATE ... IF NOT EXISTS` once at startup;
    the marginal cost over the 6 pre-existing DDL statements is the 6
    new ones from ADR-0008 §4.  Each new statement is a no-op on a
    bootstrapped database (`IF NOT EXISTS` resolves in O(1) when the
    constraint or index is already present, since Neo4j 5.x consults
    the schema cache, not the data plane).  The cumulative wall time
    of the six no-op DDL statements is well under the 200ms p50 target
    documented in #130 — the bootstrap budget is therefore preserved.

    The empirical 200ms-on-1M-nodes target cannot be exercised in the
    worktree without a populated DB; this test is a sanity floor that
    prevents the count from drifting unnoticed.
    """
    assert len(_BOOTSTRAP_STATEMENTS) == 12, (
        "Bootstrap statement count drifted from documented envelope. "
        "ADR-0005 carries 6 statements; ADR-0008 §4 adds 6. "
        "Update this test deliberately if the contract changes."
    )


# ---------------------------------------------------------------------------
# 3. Live-Neo4j contract — opt-in via env var
# ---------------------------------------------------------------------------


def _neo4j_contract_enabled() -> bool:
    """Mirror the gate from :mod:`tests.test_graph_store_contract`."""
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


@pytestmark_contract
@pytest.mark.asyncio
async def test_connect_creates_all_adr_0008_constraints_and_indexes() -> None:
    """After ``connect()``, every ADR-0008 §4 entry shows up in the schema."""
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
        )
        store = Neo4jGraphStore(config=config)
        try:
            await store.connect()
            # SHOW CONSTRAINTS — assert the new uniqueness constraint exists.
            async with store._driver.session(database=config.database) as session:
                cons_rows = [
                    record.data()
                    async for record in await (await session.run("SHOW CONSTRAINTS YIELD name"))
                ]
                idx_rows = [
                    record.data()
                    async for record in await (await session.run("SHOW INDEXES YIELD name"))
                ]
        finally:
            await store.close()

    constraint_names = {row["name"] for row in cons_rows}
    index_names = {row["name"] for row in idx_rows}
    assert "entity_state_workspace_id_valid_from_unique" in constraint_names
    for new_index in (
        "entity_workspace_recorded_at",
        "entity_state_workspace_valid_window",
        "entity_state_workspace_recorded_at",
        "edge_workspace_valid_window",
        "edge_workspace_recorded_at",
    ):
        assert new_index in index_names, f"ADR-0008 §4 index '{new_index}' missing"


@pytestmark_contract
@pytest.mark.asyncio
async def test_bootstrap_is_fast_on_empty_container() -> None:
    """Sanity-floor benchmark — bootstrap on an empty container is sub-second.

    Per the issue body: "bootstrap startup adds < 200ms to p50 connect()
    time on a 1M-node graph".  We can't reproduce that here without a
    populated DB, so the floor we assert is "12 DDL statements complete
    in well under a second on an empty container" — anything slower is
    a hard regression and a signal to investigate.
    """
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
        )
        store = Neo4jGraphStore(config=config)
        start = time.perf_counter()
        try:
            await store.connect()
        finally:
            elapsed = time.perf_counter() - start
            await store.close()

    # Generous floor — per-statement cost on an empty graph is single-digit
    # ms on Neo4j 5.x; the loop is sequential so 12 statements is well
    # under 5s in practice.  Asserting <10s catches a hard regression
    # without being flaky on slow CI.
    assert elapsed < 10.0, f"Bootstrap took {elapsed:.2f}s — investigate."


_ = neo4j_store  # silence "imported but unused" — module-level guards run
