"""Stub-entity identity: the key the by-name edge writer MERGEs on.

Docker-free counterpart to ``tests/integration/test_neo4j_edge_idempotency.py``.
The integration module proves the *observable* property (an edge set that does
not grow) against a real Neo4j; this one proves the *mechanism* against the
real store code with a mocked driver, so the gate still runs where the Docker
API is unavailable.

What went wrong
---------------

``_UPSERT_EDGE_BY_NAME_CYPHER_TEMPLATE`` MERGEd the placeholder ("stub")
target on ``name`` and stamped it with a fresh ``uuid4()``::

    MERGE (b:Entity {workspace_id: $workspace_id, name: $target_name})
    ON CREATE SET b.id = $generated_id      # uuid4 — new on every call

``name`` has an index but no uniqueness *constraint*, and Neo4j only takes a
cross-transaction lock for MERGE on a constrained key.  Ten concurrent refs
therefore created ten `:Entity` nodes sharing one name — and from then on that
MERGE bound all ten, so the relationship MERGE downstream of it ran ten times
and multiplied the edge on **every** subsequent write.  The live graph held
8898 ``depends_on`` relationships where 1159 were due.

The assertions below are the ones that fail on the pre-fix code:

* the stub id must be a pure function of ``(workspace_id, target_name)``, so
  two writers and two replays converge on one node — ``uuid4()`` cannot;
* the stub must be MERGEd on ``id``, the constrained key, not on ``name``.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from omniscience_index.stores.neo4j._cypher import _UPSERT_EDGE_BY_NAME_CYPHER_TEMPLATE
from omniscience_index.stores.neo4j.mappers import _stub_entity_id
from omniscience_index.stores.neo4j.store import Neo4jGraphStore, Neo4jStoreConfig

# No module-level ``pytest.mark.asyncio``: this module mixes async and sync
# tests, and ``asyncio_mode = "auto"`` (pyproject.toml) already collects the
# async ones.  A blanket mark would warn on every sync test here.

_WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ARN = "arn:aws:iam::000000000000:root"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _make_config() -> Neo4jStoreConfig:
    return Neo4jStoreConfig(
        uri="bolt://neo4j-fake:7687",
        username="neo4j",
        password="placeholder",
        database="neo4j",
        max_connection_pool_size=10,
        connection_acquisition_timeout_seconds=5.0,
        max_transaction_retry_time_seconds=5.0,
        default_max_depth=3,
    )


def _make_store_with_mock_driver() -> tuple[Neo4jGraphStore, MagicMock]:
    """Store whose driver is mocked; ``tx_mock.run`` records every statement."""
    driver_mock = MagicMock()
    driver_mock.verify_connectivity = AsyncMock(return_value=None)
    driver_mock.close = AsyncMock(return_value=None)

    tx_mock = MagicMock()
    tx_mock.run = AsyncMock()

    async def _fake_execute_write(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await fn(tx_mock, *args, **kwargs)

    session_mock = MagicMock()
    session_mock.execute_read = AsyncMock(return_value=[])
    session_mock.execute_write = AsyncMock(side_effect=_fake_execute_write)

    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session_mock)
    session_ctx.__aexit__ = AsyncMock(return_value=None)
    driver_mock.session = MagicMock(return_value=session_ctx)

    with patch(
        "omniscience_index.stores.neo4j_store.AsyncGraphDatabase.driver",
        return_value=driver_mock,
    ):
        store = Neo4jGraphStore(config=_make_config())

    driver_mock._tx_mock = tx_mock
    return store, driver_mock


class _Entity:
    """Duck-typed parser-style entity (matches ``_entity_to_params``)."""

    def __init__(self, name: str) -> None:
        self.id = uuid.uuid5(uuid.NAMESPACE_URL, name)
        self.entity_type = "aws_live"
        self.name = name
        self.display_name = name
        self.chunk_id: uuid.UUID | None = None
        self.metadata: dict[str, Any] = {"arn": name}


class _NameEdge:
    """Edge whose target is named but not defined by the batch."""

    def __init__(self, source: _Entity, target_name: str) -> None:
        self.source_entity_id = source.id
        self.target_entity_id: uuid.UUID | None = None
        self.target_name = target_name
        self.edge_type = "depends_on"
        self.metadata: dict[str, Any] = {"relation": "in_account"}


def _stub_ids_from(tx_mock: MagicMock) -> list[str]:
    """Every ``stub_id`` parameter the store sent to the driver."""
    return [
        str(call.args[1]["stub_id"])
        for call in tx_mock.run.await_args_list
        if len(call.args) > 1 and isinstance(call.args[1], dict) and "stub_id" in call.args[1]
    ]


# ---------------------------------------------------------------------------
# The stub id must be a pure function of (workspace_id, name)
# ---------------------------------------------------------------------------


async def test_repeated_writes_reuse_one_stub_id() -> None:
    """Two writes of the same named target must address the same node.

    With ``uuid4()`` each write proposed a brand-new identity, leaving ``name``
    — the unconstrained key — as the only thing tying them together.
    """
    store, driver_mock = _make_store_with_mock_driver()

    for _ in range(3):
        await store.upsert_edge_by_name(
            source_entity_id=uuid.uuid4(),
            target_name=_ACCOUNT_ARN,
            edge_type="depends_on",
            workspace_id=_WORKSPACE,
            metadata={"relation": "in_account"},
        )

    observed = _stub_ids_from(driver_mock._tx_mock)
    assert len(observed) == 3
    assert len(set(observed)) == 1, f"stub id was not stable across writes: {set(observed)}"
    assert observed[0] == str(_stub_entity_id(_WORKSPACE, _ACCOUNT_ARN))


async def test_graph_replay_reuses_one_stub_id() -> None:
    """The batch write path must be stable too, not only the single-edge API."""
    store, driver_mock = _make_store_with_mock_driver()
    entity = _Entity("arn:aws:s3:::bucket-alpha")

    for _ in range(2):
        await store.upsert_graph(
            source_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            entities=[entity],
            edges=[_NameEdge(entity, _ACCOUNT_ARN)],
            workspace_id=_WORKSPACE,
        )

    observed = _stub_ids_from(driver_mock._tx_mock)
    assert len(observed) == 2
    assert len(set(observed)) == 1, f"replay proposed a second stub: {set(observed)}"


async def test_distinct_targets_get_distinct_stub_ids() -> None:
    """Determinism must not collapse two different names onto one node."""
    store, driver_mock = _make_store_with_mock_driver()

    for target in (_ACCOUNT_ARN, "arn:aws:iam::111111111111:root"):
        await store.upsert_edge_by_name(
            source_entity_id=uuid.uuid4(),
            target_name=target,
            edge_type="depends_on",
            workspace_id=_WORKSPACE,
        )

    observed = _stub_ids_from(driver_mock._tx_mock)
    assert len(set(observed)) == 2


def test_stub_ids_are_workspace_scoped() -> None:
    """Two workspaces naming the same ARN must not share one node (ACL)."""
    other = uuid.UUID("00000000-0000-0000-0000-000000000002")
    assert _stub_entity_id(_WORKSPACE, _ACCOUNT_ARN) != _stub_entity_id(other, _ACCOUNT_ARN)


def test_stub_id_is_stable_for_str_and_uuid_workspaces() -> None:
    """Callers pass ``workspace_id`` as both types; the id must not fork."""
    assert _stub_entity_id(_WORKSPACE, _ACCOUNT_ARN) == _stub_entity_id(
        str(_WORKSPACE), _ACCOUNT_ARN
    )


# ---------------------------------------------------------------------------
# The MERGE key itself
# ---------------------------------------------------------------------------


def test_stub_is_not_merged_on_the_unconstrained_name_key() -> None:
    """MERGE must key on ``id``; ``name`` cannot carry a uniqueness constraint.

    Real entities legitimately share names across sources, so ``name`` can
    never be constrained — which is exactly why MERGEing the stub on it took
    no cross-transaction lock and admitted both the fork and the fan-out.
    """
    cypher = _UPSERT_EDGE_BY_NAME_CYPHER_TEMPLATE

    assert "MERGE (b:Entity {workspace_id: $workspace_id, name: $target_name})" not in cypher
    assert "MERGE (b:Entity {workspace_id: $workspace_id, id: target_id})" in cypher
    assert "$stub_id" in cypher
    # The old parameter must be gone, not merely unused.
    assert "$generated_id" not in cypher


def test_by_name_edge_write_converges_on_metadata_change() -> None:
    """A re-ingest whose metadata changed must update, not keep the first value.

    Without ``ON MATCH SET`` the relationship froze at whatever the first write
    saw, so a converged edge *count* would still hide diverged edge *content*.
    """
    assert "ON MATCH SET" in _UPSERT_EDGE_BY_NAME_CYPHER_TEMPLATE


# ---------------------------------------------------------------------------
# Cypher injection through the by-name edge path
# ---------------------------------------------------------------------------
#
# Relationship type cannot be a Cypher parameter, so the by-name path renders
# ``edge_type`` into the template with ``str.replace``.  Every OTHER edge path
# validates the type first: ``upsert_edge`` checks it directly, and
# ``_edge_to_params`` raises ``invalid_edge_type`` for the resolved path.
#
# The by-name path had no check at all, and could not inherit one:
# ``_edge_to_params`` returns ``None`` for an unresolvable target BEFORE it
# reaches its own validation, and ``_run_upsert_graph`` reads that ``None`` as
# "take the stub path" rather than "this edge was rejected".  So an edge_type
# straight off a parser reached ``.replace()`` unexamined.

_INJECTED_EDGE_TYPE = (
    "x`{workspace_id:$workspace_id}]->(b) WITH a MATCH (z:Entity) DETACH DELETE z // "
)


class _MaliciousEdge:
    """An unresolvable-target edge whose ``edge_type`` is an injection payload."""

    def __init__(self, source: _Entity, edge_type: str) -> None:
        self.source_entity_id = source.id
        self.target_entity_id: uuid.UUID | None = None
        self.target_name = _ACCOUNT_ARN
        self.edge_type = edge_type
        self.metadata: dict[str, Any] = {}


async def test_a_malicious_edge_type_is_rejected_before_it_is_rendered() -> None:
    """The payload closes the relationship pattern and appends an unscoped delete.

    Rendered, the statement becomes a ``MATCH (z:Entity) DETACH DELETE z`` with
    no ``workspace_id`` predicate — every tenant's graph, from one parser
    field.  The write must be refused, and nothing may reach the driver.
    """
    store, driver_mock = _make_store_with_mock_driver()
    entity = _Entity("arn:aws:s3:::bucket-injection")

    with pytest.raises(ValueError, match=r"^invalid_edge_type:"):
        await store.upsert_graph(
            source_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            entities=[entity],
            edges=[_MaliciousEdge(entity, _INJECTED_EDGE_TYPE)],
            workspace_id=_WORKSPACE,
        )

    rendered = [
        str(call.args[0]) for call in driver_mock._tx_mock.run.await_args_list if call.args
    ]
    assert not any("DETACH DELETE z" in stmt for stmt in rendered), (
        "the injected clause reached the driver"
    )


async def test_upsert_edge_by_name_rejects_a_malicious_edge_type() -> None:
    """The public single-edge entry point renders the same slot."""
    store, driver_mock = _make_store_with_mock_driver()

    with pytest.raises(ValueError, match=r"^invalid_edge_type:"):
        await store.upsert_edge_by_name(
            source_entity_id=uuid.uuid4(),
            target_name=_ACCOUNT_ARN,
            edge_type=_INJECTED_EDGE_TYPE,
            workspace_id=_WORKSPACE,
        )

    assert driver_mock._tx_mock.run.await_count == 0


async def test_an_edge_type_with_a_trailing_newline_is_rejected() -> None:
    """``re.match`` accepts it because ``$`` also matches before a final newline.

    Neo4j treats ``depends_on`` and ``depends_on\\n`` as two distinct
    relationship types, so admitting one forks the type — and the per-type
    index DDL in ``_bootstrap_schema`` cannot render the second.
    """
    store, _driver_mock = _make_store_with_mock_driver()
    entity = _Entity("arn:aws:s3:::bucket-newline")

    with pytest.raises(ValueError, match=r"^invalid_edge_type:"):
        await store.upsert_graph(
            source_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            entities=[entity],
            edges=[_MaliciousEdge(entity, "depends_on\n")],
            workspace_id=_WORKSPACE,
        )


def test_a_noop_replay_does_not_advance_the_edge_timestamp() -> None:
    """``updated_at`` must move only on a real change — it feeds ``recorded_at``.

    ``_BACKFILL_EDGE_PROPS_CYPHER`` derives ``r.recorded_at = r.updated_at``,
    so stamping ``updated_at`` on an unchanged replay would restate when the
    fact was *recorded* as when it was last *looked at*, corrupting the very
    axis ADR-0008 exists to protect.  An unconditional
    ``ON MATCH SET ... r.updated_at = $now`` is therefore a defect here, not a
    convenience.
    """
    cypher = _UPSERT_EDGE_BY_NAME_CYPHER_TEMPLATE
    on_match = cypher.split("ON MATCH SET", 1)[1]

    assert "r.updated_at = $now" not in on_match, (
        "updated_at must be guarded by a change predicate, not assigned outright"
    )
    assert "ELSE r.updated_at END" in on_match
    # The guard must be evaluated before metadata is overwritten: Cypher
    # applies SET items in order and later ones observe earlier ones.
    assert on_match.index("r.updated_at") < on_match.index("r.metadata = $metadata")
