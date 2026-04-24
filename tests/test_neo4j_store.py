"""Unit tests for :class:`Neo4jGraphStore` (issue #104, Phase 2a of epic #96).

Focus
-----

1.  **ACL invariant at the Cypher level.** Every read template in the
    adapter MUST reference ``workspace_id``.  Enforced by a regression
    guard over the module's Cypher templates — this is the lint-style
    rule ADR-0005 §Consequences mandates.

2.  **Workspace_id is propagated into every query.** The adapter wraps
    the official ``neo4j.AsyncDriver``; we patch the driver and inspect
    the params of every ``tx.run()`` call to assert ``workspace_id`` is
    present.

3.  **Depth clamping + edge-type validation.** The planner must be
    protected from user-supplied huge depths and injection attempts via
    the edge-type allowlist.

4.  **Idempotent MERGE semantics** in the write Cypher (string-level
    assertion that ``ON CREATE SET`` and ``ON MATCH SET`` both appear).

Contract tests that exercise a real Neo4j (via ``testcontainers-neo4j``)
live in :mod:`tests.test_graph_store_contract` and
:mod:`tests.test_graph_workspace_isolation_neo4j` — they are skipped
when Docker is unavailable.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from omniscience_core.storage.graph import (
    EdgeUpsert,
    EntityUpsert,
    GraphStore,
)
from omniscience_index.stores import neo4j_store
from omniscience_index.stores.neo4j_store import (
    Neo4jGraphStore,
    Neo4jStoreConfig,
    _build_traverse_cypher,
    _clamp_depth,
    _ensure_workspace_predicate,
    _validate_edge_types,
)

_WORKSPACE_A = uuid.UUID("aaaaaaaa-0000-0000-0000-0000000000a1")
_WORKSPACE_B = uuid.UUID("bbbbbbbb-0000-0000-0000-0000000000b2")


# ---------------------------------------------------------------------------
# Config + fixtures
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
    """Build a store whose async driver is mocked in place.

    Returns (store, mock_driver).  The mock driver exposes
    ``session(...).execute_read`` / ``.execute_write`` as AsyncMocks so
    unit tests can inspect what the adapter would send to Neo4j.
    """
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

    # Expose the session-level mock for assertions.
    driver_mock._session_mock = session_mock
    return store, driver_mock


# ---------------------------------------------------------------------------
# 1. ACL regression guards — Cypher template level
# ---------------------------------------------------------------------------


def test_every_read_template_references_workspace_id() -> None:
    """Lint-style guard: no read Cypher may omit ``workspace_id``."""
    templates = {
        "_GET_ENTITY_BY_NAME_CYPHER": neo4j_store._GET_ENTITY_BY_NAME_CYPHER,
        "_TRAVERSE_CYPHER_TEMPLATE": neo4j_store._TRAVERSE_CYPHER_TEMPLATE,
    }
    for name, cypher in templates.items():
        assert "workspace_id" in cypher, f"Read template {name} lost workspace_id predicate"


def test_every_write_template_references_workspace_id() -> None:
    """Writes also pin workspace_id — the MERGE key is composite."""
    templates = {
        "_UPSERT_ENTITY_CYPHER": neo4j_store._UPSERT_ENTITY_CYPHER,
        "_UPSERT_EDGE_CYPHER_TEMPLATE": neo4j_store._UPSERT_EDGE_CYPHER_TEMPLATE,
        "_DELETE_BY_SOURCE_CYPHER": neo4j_store._DELETE_BY_SOURCE_CYPHER,
    }
    for name, cypher in templates.items():
        assert "workspace_id" in cypher, f"Write template {name} lost workspace_id predicate"


def test_ensure_workspace_predicate_raises_on_missing() -> None:
    """The import-time guard rejects Cypher without the predicate."""
    with pytest.raises(RuntimeError, match="workspace_id"):
        _ensure_workspace_predicate("MATCH (n) RETURN n", "bogus_template")


def test_ensure_workspace_predicate_accepts_when_present() -> None:
    _ensure_workspace_predicate("MATCH (n {workspace_id: $workspace_id}) RETURN n", "ok")


# ---------------------------------------------------------------------------
# 2. Helpers: depth clamp + edge-type validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "ceiling", "expected"),
    [
        (0, 6, 1),  # <1 clamps up
        (-5, 6, 1),  # negative clamps up
        (1, 6, 1),
        (3, 6, 3),
        (6, 6, 6),
        (100, 6, 6),  # >ceiling clamps down
    ],
)
def test_clamp_depth(requested: int, ceiling: int, expected: int) -> None:
    assert _clamp_depth(requested, ceiling) == expected


@pytest.mark.parametrize(
    "bad_type",
    [
        "CALLS; DROP CONSTRAINT foo",  # Cypher injection attempt
        "foo-bar",  # hyphens rejected
        "foo bar",  # whitespace rejected
        "1_leading_digit",  # leading digit rejected
        "",  # empty rejected
        "`CALLS`",  # backtick rejected
    ],
)
def test_validate_edge_types_rejects_unsafe(bad_type: str) -> None:
    with pytest.raises(ValueError, match="invalid_edge_type"):
        _validate_edge_types([bad_type])


@pytest.mark.parametrize(
    "ok_type",
    ["CALLS", "depends_on", "DEPLOYED_BY", "CROSS_REF", "A1_b"],
)
def test_validate_edge_types_accepts_allowlist(ok_type: str) -> None:
    result = _validate_edge_types([ok_type])
    assert result == [ok_type]


def test_validate_edge_types_none_passthrough() -> None:
    assert _validate_edge_types(None) is None


def test_build_traverse_cypher_injects_clamped_depth() -> None:
    cypher = _build_traverse_cypher(max_depth=3, edge_types=None)
    assert "rels*1..3" in cypher
    # No filter clause when edge_types is None
    assert "r.edge_type IN" not in cypher


def test_build_traverse_cypher_emits_filter_when_types_provided() -> None:
    cypher = _build_traverse_cypher(max_depth=2, edge_types=["CALLS", "DEPENDS_ON"])
    assert "r.edge_type IN ['CALLS','DEPENDS_ON']" in cypher


# ---------------------------------------------------------------------------
# 3. Idempotent MERGE assertions
# ---------------------------------------------------------------------------


def test_upsert_entity_cypher_is_idempotent_merge() -> None:
    text = neo4j_store._UPSERT_ENTITY_CYPHER
    assert "MERGE" in text
    assert "ON CREATE SET" in text
    assert "ON MATCH SET" in text


def test_upsert_edge_cypher_is_idempotent_merge() -> None:
    text = neo4j_store._UPSERT_EDGE_CYPHER_TEMPLATE
    assert "MERGE" in text
    assert "ON CREATE SET" in text
    assert "ON MATCH SET" in text


# ---------------------------------------------------------------------------
# 4. Protocol conformance
# ---------------------------------------------------------------------------


def test_neo4j_store_is_runtime_graph_store() -> None:
    """Runtime isinstance check against the protocol."""
    store, _ = _make_store_with_mock_driver()
    assert isinstance(store, GraphStore)


# ---------------------------------------------------------------------------
# 5. Workspace_id propagation — read API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entity_passes_workspace_id_to_cypher() -> None:
    store, driver_mock = _make_store_with_mock_driver()
    session_mock = driver_mock._session_mock

    # session.execute_read receives (func, cypher, params); return [] to signal
    # "entity not found".
    session_mock.execute_read = AsyncMock(return_value=[])

    await store.get_entity(entity_name="svc.auth", workspace_id=_WORKSPACE_A)

    session_mock.execute_read.assert_awaited()
    args, _ = session_mock.execute_read.call_args
    # Signature: (_run_read_stmt, cypher, params)
    cypher = args[1]
    params = args[2]
    assert "workspace_id" in cypher
    assert params["workspace_id"] == str(_WORKSPACE_A)
    assert params["entity_name"] == "svc.auth"


@pytest.mark.asyncio
async def test_get_entity_returns_none_when_no_rows() -> None:
    store, driver_mock = _make_store_with_mock_driver()
    driver_mock._session_mock.execute_read = AsyncMock(return_value=[])

    result = await store.get_entity(entity_name="missing", workspace_id=_WORKSPACE_A)

    assert result is None


@pytest.mark.asyncio
async def test_get_entity_returns_view_when_row_present() -> None:
    store, driver_mock = _make_store_with_mock_driver()
    source_uuid = uuid.uuid4()
    driver_mock._session_mock.execute_read = AsyncMock(
        return_value=[
            {
                "name": "svc.auth",
                "kind": "service",
                "source_id": str(source_uuid),
                "chunk_text": "body",
            }
        ]
    )

    result = await store.get_entity(entity_name="svc.auth", workspace_id=_WORKSPACE_A)

    assert result is not None
    assert result.name == "svc.auth"
    assert result.kind == "service"
    assert result.source == str(source_uuid)


@pytest.mark.asyncio
async def test_find_related_raises_when_seed_missing() -> None:
    store, driver_mock = _make_store_with_mock_driver()
    driver_mock._session_mock.execute_read = AsyncMock(return_value=[])

    with pytest.raises(ValueError, match=r"entity_not_found:svc\.missing"):
        await store.find_related(
            entity_name="svc.missing",
            workspace_id=_WORKSPACE_A,
            max_depth=2,
        )


@pytest.mark.asyncio
async def test_find_related_passes_workspace_id_and_clamps_depth() -> None:
    store, driver_mock = _make_store_with_mock_driver()

    # Seed row with no neighbours (empty collect).
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

    result = await store.find_related(
        entity_name="svc.auth",
        workspace_id=_WORKSPACE_A,
        max_depth=999,  # Should clamp to ceiling (6)
        edge_types=["CALLS"],
    )

    args, _ = driver_mock._session_mock.execute_read.call_args
    cypher = args[1]
    params = args[2]
    assert "workspace_id" in cypher
    assert params["workspace_id"] == str(_WORKSPACE_A)
    assert "rels*1..6" in cypher  # clamped
    assert "r.edge_type IN ['CALLS']" in cypher
    assert result.seed.name == "svc.auth"
    assert result.related == []


@pytest.mark.asyncio
async def test_find_related_rejects_injection_in_edge_types() -> None:
    store, _driver_mock = _make_store_with_mock_driver()

    with pytest.raises(ValueError, match="invalid_edge_type"):
        await store.find_related(
            entity_name="svc.auth",
            workspace_id=_WORKSPACE_A,
            max_depth=1,
            edge_types=["CALLS; MATCH (n) DETACH DELETE n //"],
        )


# ---------------------------------------------------------------------------
# 6. Workspace_id propagation — write API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_entity_passes_workspace_id() -> None:
    store, driver_mock = _make_store_with_mock_driver()
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
    assert params["id"] == str(payload.id)


@pytest.mark.asyncio
async def test_upsert_edge_passes_workspace_id() -> None:
    store, driver_mock = _make_store_with_mock_driver()
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
async def test_upsert_edge_rejects_invalid_edge_type() -> None:
    store, _driver_mock = _make_store_with_mock_driver()
    bad = EdgeUpsert(
        source_entity_id=uuid.uuid4(),
        target_entity_id=uuid.uuid4(),
        edge_type="CALLS; DROP CONSTRAINT foo",
    )
    with pytest.raises(ValueError, match="invalid_edge_type"):
        await store.upsert_edge(edge=bad, workspace_id=_WORKSPACE_A)


@pytest.mark.asyncio
async def test_delete_tombstoned_returns_count() -> None:
    store, driver_mock = _make_store_with_mock_driver()
    driver_mock._session_mock.execute_write = AsyncMock(return_value=[{"deleted": 7}])

    count = await store.delete_tombstoned()

    assert count == 7


@pytest.mark.asyncio
async def test_delete_tombstoned_returns_zero_when_empty() -> None:
    store, driver_mock = _make_store_with_mock_driver()
    driver_mock._session_mock.execute_write = AsyncMock(return_value=[])

    assert await store.delete_tombstoned() == 0


# ---------------------------------------------------------------------------
# 7. upsert_graph: replace-by-source semantics + workspace derivation
# ---------------------------------------------------------------------------


class _FakeEntity:
    """Duck-typed parser-style entity carrying a workspace_id attribute."""

    def __init__(self, *, workspace_id: uuid.UUID, name: str) -> None:
        self.id = uuid.uuid4()
        self.source_id = uuid.uuid4()
        self.workspace_id = workspace_id
        self.entity_type = "service"
        self.name = name
        self.display_name = name.split(".")[-1]
        self.chunk_id: uuid.UUID | None = None
        self.metadata: dict[str, Any] = {}


@pytest.mark.asyncio
async def test_upsert_graph_rejects_empty_batch() -> None:
    store, _driver_mock = _make_store_with_mock_driver()
    with pytest.raises(ValueError, match="upsert_graph_empty_batch"):
        await store.upsert_graph(
            source_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            entities=[],
            edges=[],
        )


@pytest.mark.asyncio
async def test_upsert_graph_requires_workspace_on_entities() -> None:
    store, _driver_mock = _make_store_with_mock_driver()

    class _NoWorkspaceEntity:
        def __init__(self) -> None:
            self.id = uuid.uuid4()
            self.name = "x"
            self.display_name = "x"
            self.entity_type = "service"
            self.source_id = uuid.uuid4()
            self.chunk_id: uuid.UUID | None = None
            self.metadata: dict[str, Any] = {}  # no workspace_id

    with pytest.raises(ValueError, match="upsert_graph_missing_workspace_id"):
        await store.upsert_graph(
            source_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            entities=[_NoWorkspaceEntity()],
            edges=[],
        )


@pytest.mark.asyncio
async def test_upsert_graph_derives_workspace_from_entity() -> None:
    store, driver_mock = _make_store_with_mock_driver()
    ent = _FakeEntity(workspace_id=_WORKSPACE_A, name="svc.auth")

    await store.upsert_graph(
        source_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        entities=[ent],
        edges=[],
    )

    # Exactly one execute_write call (the transaction runner) — but it is
    # invoked with the bound method, not the inline lambda.  We assert the
    # call happened, and that the workspace derivation was accepted.
    assert driver_mock._session_mock.execute_write.await_count >= 1


# ---------------------------------------------------------------------------
# 8. Connect runs the schema bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_runs_bootstrap_statements() -> None:
    store, driver_mock = _make_store_with_mock_driver()

    await store.connect()

    # Connectivity was verified.
    driver_mock.verify_connectivity.assert_awaited_once()
    # One write per bootstrap statement.
    assert driver_mock._session_mock.execute_write.await_count == len(
        neo4j_store._BOOTSTRAP_STATEMENTS
    )


@pytest.mark.asyncio
async def test_close_closes_driver() -> None:
    store, driver_mock = _make_store_with_mock_driver()

    await store.close()

    driver_mock.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# 9. Keyword-only workspace_id — enforced by Python signature
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entity_refuses_positional_workspace_id() -> None:
    store, _driver_mock = _make_store_with_mock_driver()

    with pytest.raises(TypeError):
        # mypy would reject this; we still want the runtime guarantee.
        await store.get_entity("svc.auth", _WORKSPACE_A)  # type: ignore[misc]


@pytest.mark.asyncio
async def test_find_related_refuses_missing_workspace_id() -> None:
    store, _driver_mock = _make_store_with_mock_driver()

    with pytest.raises(TypeError):
        await store.find_related(entity_name="svc.auth")  # type: ignore[call-arg]
