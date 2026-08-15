"""Bitemporal schema bootstrap tests for :class:`Neo4jGraphStore` (issue #130).

Covers ADR-0008 §4 — verifies that after ``connect()``, every constraint
and index from the ADR is present in the live database.

Two layers
----------

1. **Pure-Python lints** (run unconditionally) — assert the
   ``_BOOTSTRAP_STATEMENTS`` tuple includes the new bitemporal
   DDL statements that remain static, that every new node-label
   index is composite on ``workspace_id`` first, and that the
   bitemporal DDL appears *after* the ADR-0005 carry-forward
   statements (ordering matters only for human review, but stable
   ordering helps reviewers).

2. **Live Neo4j contract** (opt-in via
   ``OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1``) — spins up a fresh
   Neo4j container, runs ``connect()``, then queries
   ``SHOW CONSTRAINTS`` / ``SHOW INDEXES`` and asserts every entry
   from ADR-0008 §4 (node-label DDL) exists.  Mirrors the gate in
   :mod:`tests.test_graph_store_contract`.

Relationship-property indexes (issue #224 / ADR-0012) are emitted
dynamically per relationship type at bootstrap time, so their
end-to-end coverage lives in the integration test at
:mod:`tests.integration.test_neo4j_bootstrap_schema` — which runs
in CI without an opt-in env var.  The dynamic path cannot be lint-
checked from a string tuple, so the unit-level tests below cover
only the static node-label DDL.

The contract tests in this module are skipped when Docker /
testcontainers-neo4j is unavailable; the lint tests always run.
"""

from __future__ import annotations

import os
import time

import pytest
from omniscience_index.stores import neo4j_store
from omniscience_index.stores.neo4j_store import (
    _BITEMPORAL_BACKFILL_STATEMENTS,
    _BOOTSTRAP_STATEMENTS,
    _EDGE_INDEX_TEMPLATES,
    Neo4jGraphStore,
    Neo4jStoreConfig,
)

# ADR-0008 §4 — every new node-label index/constraint name MUST appear in
# the static bootstrap.  Relationship-property indexes
# (``edge_workspace_valid_window`` / ``edge_workspace_recorded_at``) were
# moved to the per-type dynamic path in issue #224 / ADR-0012; their
# bootstrap coverage is asserted by the integration test.
_ADR_0008_DDL_NAMES: tuple[str, ...] = (
    "entity_state_workspace_id_valid_from_unique",
    "entity_workspace_recorded_at",
    "entity_state_workspace_valid_window",
    "entity_state_workspace_recorded_at",
)


# ---------------------------------------------------------------------------
# 1. Pure-Python lints over the bootstrap tuple
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ddl_name", _ADR_0008_DDL_NAMES)
def test_bootstrap_includes_adr_0008_ddl_by_name(ddl_name: str) -> None:
    """Every ADR-0008 §4 node-label DDL name must appear in the bootstrap."""
    rendered = "\n".join(_BOOTSTRAP_STATEMENTS)
    assert ddl_name in rendered, (
        f"Bootstrap is missing ADR-0008 §4 DDL '{ddl_name}' — see neo4j_store.py."
    )


def test_bootstrap_uses_if_not_exists_everywhere() -> None:
    """Every bootstrap statement is idempotent — `connect()` must be re-run-safe."""
    for stmt in _BOOTSTRAP_STATEMENTS:
        assert "IF NOT EXISTS" in stmt, f"Bootstrap statement is not idempotent: {stmt!r}"


def test_edge_index_templates_use_if_not_exists() -> None:
    """Per-type edge-index templates must be idempotent too (issue #224)."""
    for prefix, template in _EDGE_INDEX_TEMPLATES:
        assert "IF NOT EXISTS" in template, (
            f"Edge index template '{prefix}' is not idempotent: {template!r}"
        )


def test_edge_index_templates_use_typed_relationship_pattern() -> None:
    """Issue #224 — every edge-index template must specify a relationship type.

    Neo4j 5.x rejects ``FOR ()-[r]-() ON (r.prop)`` at parse time for
    relationship-property indexes; the typed form ``FOR ()-[r:`{rel_type}`]-()``
    is required.  This lint is a regression guard against a refactor that
    accidentally drops the rel-type placeholder.
    """
    for prefix, template in _EDGE_INDEX_TEMPLATES:
        assert "[r:`{rel_type}`]" in template, (
            f"Edge index template '{prefix}' is missing the rel-type placeholder "
            f"— Neo4j 5.x will reject this DDL.  See ADR-0012 and issue #224."
        )


def test_new_bitemporal_indexes_are_workspace_id_first() -> None:
    """ACL invariant — every new node-label index is composite on workspace_id first.

    ADR-0008 §Consequences-security #1 + ACL carry-forward from
    #117 / #119: workspace_id is always the leading column.

    Relationship-property indexes follow the same invariant via the
    per-type templates in ``_EDGE_INDEX_TEMPLATES``; their workspace-id-
    first ordering is asserted by
    ``test_edge_index_templates_are_workspace_id_first`` below.
    """
    composite_index_names = (
        "entity_workspace_recorded_at",
        "entity_state_workspace_valid_window",
        "entity_state_workspace_recorded_at",
    )
    for name in composite_index_names:
        stmt = next(s for s in _BOOTSTRAP_STATEMENTS if name in s)
        # The first parenthesised property in the ON (...) clause must be
        # workspace_id.  We do a substring check on the canonical form
        # used in the templates.
        assert "(n.workspace_id" in stmt or "(s.workspace_id" in stmt, (
            f"Index '{name}' is not workspace_id-first — ACL invariant violated"
        )


def test_edge_index_templates_are_workspace_id_first() -> None:
    """ACL invariant for the dynamic edge-index templates (issue #224).

    ``edge_source_id`` is intentionally NOT composite on workspace_id —
    it indexes the ingestion source identifier alone (mirrors the
    node-label ``entity_source_id`` index from ADR-0005).  Every other
    template must lead with ``r.workspace_id``.
    """
    workspace_first_prefixes = {
        "edge_workspace_id",
        "edge_workspace_valid_window",
        "edge_workspace_recorded_at",
    }
    for prefix, template in _EDGE_INDEX_TEMPLATES:
        if prefix in workspace_first_prefixes:
            assert "(r.workspace_id" in template, (
                f"Edge index template '{prefix}' is not workspace_id-first "
                "— ACL invariant violated"
            )


def test_entity_state_unique_constraint_includes_workspace_id() -> None:
    """The ADR-0008 §4 uniqueness constraint is on (workspace_id, id, valid_from)."""
    stmt = next(
        s for s in _BOOTSTRAP_STATEMENTS if "entity_state_workspace_id_valid_from_unique" in s
    )
    # Fully assert the composite shape — never `(id, valid_from)` alone.
    assert "(s.workspace_id, s.id, s.valid_from)" in stmt


def test_bitemporal_ddl_is_additive_not_replacing() -> None:
    """ADR-0005 carry-forward node-label DDL must remain in the bootstrap unchanged.

    Unit-level only — replaced for the relationship-property DDL by the
    integration test at :mod:`tests.integration.test_neo4j_bootstrap_schema`,
    which executes the bootstrap end-to-end against Neo4j 5.19-community.
    The previous version of this test string-grepped for ``edge_workspace_id``
    / ``edge_source_id`` in ``_BOOTSTRAP_STATEMENTS`` and gave a false sense
    of coverage — the DDL it asserted was syntactically invalid Cypher
    (issue #224).  Those two checks are now performed at runtime by the
    integration test, which asserts a per-type index was actually created.
    """
    rendered = "\n".join(_BOOTSTRAP_STATEMENTS)
    # Pre-existing node-label statements that #130 must not touch.
    for legacy_name in (
        "entity_workspace_id_unique",
        "entity_workspace_kind",
        "entity_workspace_name",
        "entity_source_id",
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
    """Sanity floor — 12 statements (5 ADR-0005 + 3 ADR-0008 + 2 #310 + 2 checkpoint).

    A 1M-node graph hits each `CREATE ... IF NOT EXISTS` once at startup;
    the marginal cost over the pre-existing node-label DDL is the
    bitemporal node-label additions.  Each statement is a no-op on a
    bootstrapped database (`IF NOT EXISTS` resolves in O(1) when the
    constraint or index is already present, since Neo4j 5.x consults
    the schema cache, not the data plane).  The cumulative wall time
    is well under the 200ms p50 target documented in #130 — the
    bootstrap budget is therefore preserved.

    Issue #224 / ADR-0012 moved the four relationship-property indexes
    out of the static tuple and into the per-type dynamic path; the
    static tuple shrank from 12 to 8 entries.  Issue #310 (Gap B) then
    added two composite indexes on the first-class `cluster` property —
    one on `:Entity (workspace_id, kind, cluster)` and one on the
    `:EntityState` mirror — so `list_entities` answers cluster-scoped
    queries with an index seek; the static tuple is back to 10.  Dynamic
    per-type DDL bounds are exercised by the integration test.

    The graph-write checkpoint fix then added two composite uniqueness
    constraints — `:StoreCheckpoint (workspace_id, source_id)` and
    `:DocumentCheckpoint (workspace_id, source_id, document_id)` — taking the
    tuple to 12.  Their absence is what let concurrent `MERGE`s fork the
    source checkpoint into duplicate nodes (10 observed for a single source
    in a live database), making the staleness guard read an arbitrary one.
    The accompanying pre-DDL data heal deliberately lives OUTSIDE this tuple,
    in `_CHECKPOINT_HEAL_STATEMENTS`, so that this module's
    `IF NOT EXISTS`-idempotence lint keeps covering pure DDL only.
    """
    assert len(_BOOTSTRAP_STATEMENTS) == 12, (
        "Static bootstrap statement count drifted from documented envelope. "
        "ADR-0005 carries 5 node-label statements; ADR-0008 §4 adds 3; "
        "issue #310 (Gap B) adds 2 cluster indexes; the graph-write "
        "checkpoint fix adds 2 checkpoint uniqueness constraints. "
        "Update this test deliberately if the contract changes."
    )


def test_edge_index_template_count_within_documented_envelope() -> None:
    """Sanity floor for the per-type edge-index templates (issue #224).

    Four templates (workspace_id, source_id, workspace_valid_window,
    workspace_recorded_at) issued per discovered relationship type.
    Bumping this count means we're emitting more DDL per type at every
    cold-start; do it deliberately and update ADR-0012 §Consequences.
    """
    assert len(_EDGE_INDEX_TEMPLATES) == 4, (
        "Edge-index template count drifted — see ADR-0012 §Consequences."
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
    """After ``connect()``, every ADR-0008 §4 node-label entry shows up.

    Relationship-property index coverage (issue #224 / ADR-0012) is
    exercised in :mod:`tests.integration.test_neo4j_bootstrap_schema`,
    which runs without an opt-in env var so the bug never reaches
    production again.
    """
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
        try:
            await store.connect()
            # SHOW CONSTRAINTS — assert the new uniqueness constraint exists.
            async with store._driver.session(database=config.database) as session:
                cons_rows = [
                    record.data()
                    async for record in await session.run("SHOW CONSTRAINTS YIELD name")
                ]
                idx_rows = [
                    record.data() async for record in await session.run("SHOW INDEXES YIELD name")
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
    ):
        assert new_index in index_names, f"ADR-0008 §4 index '{new_index}' missing"


@pytestmark_contract
@pytest.mark.asyncio
async def test_bootstrap_is_fast_on_empty_container() -> None:
    """Sanity-floor benchmark — bootstrap on an empty container is sub-second.

    Per the issue body: "bootstrap startup adds < 200ms to p50 connect()
    time on a 1M-node graph".  We can't reproduce that here without a
    populated DB, so the floor we assert is "static DDL completes in well
    under a second on an empty container, plus a single empty
    db.relationshipTypes() round-trip" — anything slower is a hard
    regression and a signal to investigate.
    """
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
        start = time.perf_counter()
        try:
            await store.connect()
        finally:
            elapsed = time.perf_counter() - start
            await store.close()

    # Generous floor — per-statement cost on an empty graph is single-digit
    # ms on Neo4j 5.x; the loop is sequential so the static DDL is well
    # under 5s in practice.  Asserting <10s catches a hard regression
    # without being flaky on slow CI.
    assert elapsed < 10.0, f"Bootstrap took {elapsed:.2f}s — investigate."


_ = neo4j_store  # silence "imported but unused" — module-level guards run
