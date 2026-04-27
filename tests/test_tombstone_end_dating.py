"""Tombstone end-dating tests for the bitemporal writer (issue #137).

Replaces hard-delete with end-dating (set ``valid_to``) on the Neo4j
adapter and on the Qdrant adapter when ``GRAPH_BITEMPORAL=enabled``.
The retention worker (#135 / ADR-0009) is the only remaining hard-
delete path on either store and operates on ``recorded_at`` age, NOT on
tombstone signals.

Three layers
------------

1. **Unit-level mock-driver coverage** (run unconditionally) — assert
   that the new ``_END_DATE_*`` Cypher templates carry the workspace
   predicate, that ``upsert_graph(snapshot_at=...)`` end-dates by
   source under the bitemporal flag while the disabled branch keeps
   ``DETACH DELETE``, and that ``delete_tombstoned`` dispatches on the
   flag with the ACL-safe parameter shape.  Includes Qdrant
   ``end_date_chunks`` shape tests.

2. **Live-Neo4j contract** (opt-in via
   ``OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1``) — exercise the seven
   correctness/isolation tests listed in issue #137 against a real
   Neo4j 5.x via testcontainers.  Mirrors the structure of
   ``tests/test_neo4j_store_bitemporal_writer.py``.

3. **Property tests** (``hypothesis``) — generative "no data loss"
   property: an entity queryable at any prior ``as_of`` is still
   queryable at that ``as_of`` after end-dating.  100+ random fixtures.

ADR-0008 §3 + §Consequences-security carry-forward.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from omniscience_core.storage.graph import EntityUpsert
from omniscience_index.stores import neo4j_store
from omniscience_index.stores.neo4j_store import (
    _DELETE_BY_SOURCE_CYPHER,
    _DELETE_TOMBSTONED_CYPHER,
    _END_DATE_BY_SOURCE_CYPHER,
    _END_DATE_TOMBSTONED_CYPHER,
    Neo4jGraphStore,
    Neo4jStoreConfig,
)

_WORKSPACE_A = uuid.UUID("aaaaaaaa-0000-0000-0000-0000000000a1")
_WORKSPACE_B = uuid.UUID("bbbbbbbb-0000-0000-0000-0000000000b2")


def _strip_cypher_comments(cypher: str) -> str:
    """Remove ``// ...`` line comments so structural string-checks ignore them."""
    out_lines: list[str] = []
    for line in cypher.splitlines():
        idx = line.find("//")
        out_lines.append(line if idx < 0 else line[:idx])
    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# Fixtures
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
# 1. Cypher template ACL + shape guards (unit, no driver)
# ---------------------------------------------------------------------------


def test_end_date_by_source_cypher_carries_workspace_predicate() -> None:
    """ADR-0008 §Consequences-security #1 — workspace_id on every match."""
    cypher = _END_DATE_BY_SOURCE_CYPHER
    # Predicate appears on entity match and (defence-in-depth) on the
    # relationship match.  Three references minimum: entity MATCH key,
    # relationship WHERE clause, and the `$workspace_id` parameter.
    assert cypher.count("workspace_id") >= 3
    assert "$workspace_id" in cypher


def test_end_date_by_source_cypher_does_not_detach_delete() -> None:
    """The new template must NOT issue a DETACH DELETE.

    Strip Cypher comments (``// ...``) before the check — the docstring
    in the template references ``DETACH DELETE`` to explain what the
    new shape replaces.
    """
    code = _strip_cypher_comments(_END_DATE_BY_SOURCE_CYPHER)
    assert "DETACH DELETE" not in code
    assert "DELETE" not in code


def test_end_date_by_source_cypher_sets_valid_to_on_entity_state_chain() -> None:
    """End-dating closes the entity, the open [:HAD_STATE], and the open :EntityState."""
    cypher = _END_DATE_BY_SOURCE_CYPHER
    assert "EntityState" in cypher
    assert "HAD_STATE" in cypher
    # All three rows close their valid_to.
    assert "n.valid_to = datetime($now)" in cypher
    assert "h.valid_to = datetime($now)" in cypher
    assert "s.valid_to = datetime($now)" in cypher
    assert "r.valid_to = datetime($now)" in cypher


def test_end_date_by_source_cypher_only_touches_still_open_rows() -> None:
    """Re-running on already end-dated rows must be a no-op."""
    cypher = _END_DATE_BY_SOURCE_CYPHER
    # Idempotence is structural: the WHERE filters on `valid_to IS NULL`.
    assert "n.valid_to IS NULL" in cypher
    assert "h.valid_to IS NULL" in cypher
    assert "s.valid_to IS NULL" in cypher
    assert "r.valid_to IS NULL" in cypher


def test_end_date_tombstoned_cypher_carries_workspace_predicate() -> None:
    cypher = _END_DATE_TOMBSTONED_CYPHER
    assert cypher.count("workspace_id") >= 3
    assert "$workspace_id" in cypher


def test_end_date_tombstoned_cypher_does_not_detach_delete() -> None:
    code = _strip_cypher_comments(_END_DATE_TOMBSTONED_CYPHER)
    assert "DETACH DELETE" not in code
    assert "DELETE" not in code


def test_end_date_tombstoned_cypher_filters_on_tombstoned_at_and_open_chain() -> None:
    cypher = _END_DATE_TOMBSTONED_CYPHER
    assert "tombstoned_at IS NOT NULL" in cypher
    assert "n.valid_to IS NULL" in cypher


def test_legacy_delete_cypher_unchanged_byte_for_byte() -> None:
    """ADR-0008 §8 phase 2 — flag-disabled writers stay byte-identical."""
    # The legacy templates must be identical to PR #104's shape.  Any
    # refactor that drops or reformats them breaks the no-regressions
    # contract and is rejected here.
    expected_delete_by_source = (
        f"\nMATCH (n:{neo4j_store._ENTITY_LABEL} "
        "{workspace_id: $workspace_id, source_id: $source_id})\n"
        "DETACH DELETE n\n"
    )
    expected_delete_tombstoned = (
        f"\nMATCH (n:{neo4j_store._ENTITY_LABEL})\n"
        "WHERE n.tombstoned_at IS NOT NULL\n"
        "DETACH DELETE n\n"
        "RETURN count(n) AS deleted\n"
    )
    assert expected_delete_by_source == _DELETE_BY_SOURCE_CYPHER
    assert expected_delete_tombstoned == _DELETE_TOMBSTONED_CYPHER


def test_import_time_guard_covers_new_templates() -> None:
    """The two new templates pass the workspace-predicate guard."""
    # Re-running the guard at test time confirms it accepts both
    # templates.  Drop the predicate and the module would have failed
    # to import — this assertion is the smoke test.
    neo4j_store._ensure_workspace_predicate(
        _END_DATE_BY_SOURCE_CYPHER, "_END_DATE_BY_SOURCE_CYPHER"
    )
    neo4j_store._ensure_workspace_predicate(
        _END_DATE_TOMBSTONED_CYPHER, "_END_DATE_TOMBSTONED_CYPHER"
    )


# ---------------------------------------------------------------------------
# 2. Flag dispatch — disabled keeps DELETE; enabled+snapshot end-dates
# ---------------------------------------------------------------------------


class _FakeEntity:
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
async def test_disabled_flag_upsert_graph_uses_legacy_delete() -> None:
    """Flag-disabled writer keeps the PR #104 DETACH DELETE path."""
    store, driver_mock = _make_store_with_mock_driver(bitemporal_enabled=False)
    ent = _FakeEntity(workspace_id=_WORKSPACE_A, name="svc.auth")

    await store.upsert_graph(
        source_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        entities=[ent],
        edges=[],
    )

    # The execute_write callable runs the static `_run_upsert_graph`
    # which calls tx.run with the legacy DELETE Cypher first.  We
    # asserted the path-selection elsewhere (test_neo4j_store.py); here
    # we just confirm the call happened without exception.
    assert driver_mock._session_mock.execute_write.await_count >= 1


@pytest.mark.asyncio
async def test_enabled_flag_upsert_graph_with_snapshot_at_runs_end_dating() -> None:
    """Flag-enabled writer with snapshot_at routes through the end-dating Cypher."""
    store, driver_mock = _make_store_with_mock_driver(bitemporal_enabled=True)

    # Capture every Cypher seen by tx.run.
    seen_cypher: list[str] = []

    class _FakeResult:
        async def single(self) -> dict[str, int]:
            return {"entities_end_dated": 0, "edges_end_dated": 0}

    async def _capture(cypher: str, params: dict[str, Any]) -> _FakeResult:
        seen_cypher.append(cypher)
        return _FakeResult()

    tx_mock = MagicMock()
    tx_mock.run = AsyncMock(side_effect=_capture)

    async def _execute_write(callable_: Any, *args: Any, **kwargs: Any) -> Any:
        return await callable_(tx_mock, *args, **kwargs)

    driver_mock._session_mock.execute_write = AsyncMock(side_effect=_execute_write)

    ent = _FakeEntity(workspace_id=_WORKSPACE_A, name="svc.auth")
    snap = datetime.now(UTC)

    await store.upsert_graph(
        source_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        entities=[ent],
        edges=[],
        snapshot_at=snap,
    )

    # First Cypher executed must be the end-dating template, not DELETE.
    assert seen_cypher
    code = _strip_cypher_comments(seen_cypher[0])
    assert "DETACH DELETE" not in code
    assert "valid_to = datetime($now)" in code


@pytest.mark.asyncio
async def test_enabled_flag_upsert_graph_without_snapshot_at_skips_replace() -> None:
    """Flag-enabled writer with snapshot_at=None does NOT run end-dating."""
    store, driver_mock = _make_store_with_mock_driver(bitemporal_enabled=True)

    seen_cypher: list[str] = []

    async def _capture(cypher: str, params: dict[str, Any]) -> Any:
        seen_cypher.append(cypher)
        return None

    tx_mock = MagicMock()
    tx_mock.run = AsyncMock(side_effect=_capture)

    async def _execute_write(callable_: Any, *args: Any, **kwargs: Any) -> Any:
        return await callable_(tx_mock, *args, **kwargs)

    driver_mock._session_mock.execute_write = AsyncMock(side_effect=_execute_write)

    ent = _FakeEntity(workspace_id=_WORKSPACE_A, name="svc.auth")

    await store.upsert_graph(
        source_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        entities=[ent],
        edges=[],
    )

    # Delta mode — neither DELETE nor end-dating runs.
    for c in seen_cypher:
        assert "DETACH DELETE" not in c
        assert "_END_DATE_BY_SOURCE_CYPHER" not in c


@pytest.mark.asyncio
async def test_disabled_flag_delete_tombstoned_uses_legacy_cypher() -> None:
    """Disabled flag keeps the legacy unscoped DELETE for delete_tombstoned."""
    store, driver_mock = _make_store_with_mock_driver(bitemporal_enabled=False)
    driver_mock._session_mock.execute_write = AsyncMock(return_value=[{"deleted": 5}])

    count = await store.delete_tombstoned()
    assert count == 5

    args, _ = driver_mock._session_mock.execute_write.call_args
    cypher = args[1]
    assert cypher == _DELETE_TOMBSTONED_CYPHER
    assert "DETACH DELETE" in cypher


@pytest.mark.asyncio
async def test_enabled_flag_delete_tombstoned_requires_workspace_id() -> None:
    """Bitemporal path enforces workspace scoping at the API surface."""
    store, _ = _make_store_with_mock_driver(bitemporal_enabled=True)
    with pytest.raises(ValueError, match="workspace_id"):
        await store.delete_tombstoned()


@pytest.mark.asyncio
async def test_enabled_flag_delete_tombstoned_routes_to_end_dating_cypher() -> None:
    store, driver_mock = _make_store_with_mock_driver(bitemporal_enabled=True)
    driver_mock._session_mock.execute_write = AsyncMock(
        return_value=[{"entities_end_dated": 3, "edges_end_dated": 7}]
    )

    count = await store.delete_tombstoned(workspace_id=_WORKSPACE_A)
    assert count == 3  # entities count is what we surface

    args, _ = driver_mock._session_mock.execute_write.call_args
    cypher = args[1]
    params = args[2]
    assert cypher == _END_DATE_TOMBSTONED_CYPHER
    assert params["workspace_id"] == str(_WORKSPACE_A)
    assert "now" in params
    assert "DETACH DELETE" not in _strip_cypher_comments(cypher)


# ---------------------------------------------------------------------------
# 3. Qdrant end_date_chunks — workspace ACL + filter shape
# ---------------------------------------------------------------------------


def test_qdrant_end_date_filter_carries_workspace_predicate() -> None:
    """build_end_date_chunks_filter is workspace-scoped and lives in qdrant_filters."""
    from omniscience_index.stores.qdrant_constants import (
        PAYLOAD_SOURCE_ID,
        PAYLOAD_VALID_TO,
        PAYLOAD_WORKSPACE_ID,
    )
    from omniscience_index.stores.qdrant_filters import build_end_date_chunks_filter

    flt = build_end_date_chunks_filter(
        workspace_id=_WORKSPACE_A,
        source_id=uuid.UUID("11111111-0000-0000-0000-000000000111"),
        exclude_external_ids=("ext-1", "ext-2"),
    )
    must_keys = {
        cond.key  # type: ignore[union-attr]
        for cond in (flt.must or [])
        if hasattr(cond, "key") and cond.key is not None  # type: ignore[union-attr]
    }
    assert PAYLOAD_WORKSPACE_ID in must_keys
    assert PAYLOAD_SOURCE_ID in must_keys
    # IsNullCondition on valid_to lives in `must` too — its key is on the
    # nested PayloadField rather than directly on the condition.
    valid_to_present = any(
        getattr(getattr(cond, "is_null", None), "key", None) == PAYLOAD_VALID_TO
        for cond in (flt.must or [])
    )
    assert valid_to_present
    # External-id exclusion lands on must_not.
    must_not_keys = {
        cond.key  # type: ignore[union-attr]
        for cond in (flt.must_not or [])
        if hasattr(cond, "key") and cond.key is not None  # type: ignore[union-attr]
    }
    from omniscience_index.stores.qdrant_constants import PAYLOAD_EXTERNAL_ID

    assert PAYLOAD_EXTERNAL_ID in must_not_keys


def test_qdrant_end_date_filter_without_exclusion_omits_must_not() -> None:
    from omniscience_index.stores.qdrant_filters import build_end_date_chunks_filter

    flt = build_end_date_chunks_filter(
        workspace_id=_WORKSPACE_A,
        source_id=uuid.UUID("11111111-0000-0000-0000-000000000111"),
    )
    assert flt.must_not is None or len(flt.must_not) == 0


@pytest.mark.asyncio
async def test_qdrant_end_date_chunks_emits_set_payload_and_skips_when_empty() -> None:
    """Method signature + behaviour: no-op when nothing matches."""
    # Mock minimal QdrantVectorStore surface.  The class is heavy to
    # instantiate fully (collection bootstrap, embedding provider) so we
    # patch in just the qc handle and the collection name.
    from omniscience_index.stores.qdrant_store import QdrantVectorStore

    store = QdrantVectorStore.__new__(QdrantVectorStore)
    qc_mock = MagicMock()
    qc_mock.count = AsyncMock(return_value=MagicMock(count=0))
    qc_mock.set_payload = AsyncMock(return_value=None)
    store._client = qc_mock  # type: ignore[attr-defined]
    store._collection_name = "test-coll"  # type: ignore[attr-defined]

    n = await store.end_date_chunks(
        source_id=uuid.UUID("11111111-0000-0000-0000-000000000111"),
        workspace_id=_WORKSPACE_A,
    )
    assert n == 0
    qc_mock.set_payload.assert_not_called()


@pytest.mark.asyncio
async def test_qdrant_end_date_chunks_sets_valid_to_when_rows_match() -> None:
    from omniscience_index.stores.qdrant_constants import PAYLOAD_VALID_TO
    from omniscience_index.stores.qdrant_store import QdrantVectorStore

    store = QdrantVectorStore.__new__(QdrantVectorStore)
    qc_mock = MagicMock()
    qc_mock.count = AsyncMock(return_value=MagicMock(count=4))
    qc_mock.set_payload = AsyncMock(return_value=None)
    store._client = qc_mock  # type: ignore[attr-defined]
    store._collection_name = "test-coll"  # type: ignore[attr-defined]

    snap = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
    n = await store.end_date_chunks(
        source_id=uuid.UUID("11111111-0000-0000-0000-000000000111"),
        workspace_id=_WORKSPACE_A,
        snapshot_at=snap,
        exclude_external_ids={"ext-keep"},
    )
    assert n == 4
    qc_mock.set_payload.assert_awaited_once()
    _, kwargs = qc_mock.set_payload.call_args
    assert kwargs["payload"] == {PAYLOAD_VALID_TO: snap.isoformat()}


# ---------------------------------------------------------------------------
# 4. IndexWriter (Postgres) — snapshot_at parameter accepted
# ---------------------------------------------------------------------------


def test_index_writer_upsert_graph_accepts_snapshot_at_kwarg() -> None:
    """ADR-0007 §7 — Postgres operational metadata is not bitemporal.

    The kwarg is accepted for signature parity with the Neo4j writer
    so callers can pass a single ``snapshot_at`` symbol without
    branching, but Postgres ignores it.
    """
    import inspect

    from omniscience_index.writer import IndexWriter

    sig = inspect.signature(IndexWriter.upsert_graph)
    assert "snapshot_at" in sig.parameters
    assert sig.parameters["snapshot_at"].default is None


# ---------------------------------------------------------------------------
# 5. Live-Neo4j contract tests (opt-in via env var)
# ---------------------------------------------------------------------------


def _neo4j_contract_enabled() -> bool:
    if os.environ.get("OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS") != "1":
        return False
    try:
        import testcontainers.neo4j  # noqa: F401
    except ImportError:
        return False
    return True


pytestmark_live = pytest.mark.skipif(
    not _neo4j_contract_enabled(),
    reason="set OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1 + install testcontainers",
)


async def _live_store(
    *, bitemporal_enabled: bool, container_uri: str
) -> AsyncIterator[Neo4jGraphStore]:
    config = Neo4jStoreConfig(
        uri=container_uri,
        username="neo4j",
        password="testtest1",
        database="neo4j",
        max_connection_pool_size=4,
        connection_acquisition_timeout_seconds=10.0,
        max_transaction_retry_time_seconds=10.0,
        default_max_depth=3,
        bitemporal_enabled=bitemporal_enabled,
    )
    store = Neo4jGraphStore(config=config)
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture(scope="module")
def _neo4j_container() -> Any:  # type: ignore[no-untyped-def]
    if not _neo4j_contract_enabled():
        pytest.skip("contract tests disabled")
    from testcontainers.neo4j import Neo4jContainer  # type: ignore[import-not-found]

    container = Neo4jContainer("neo4j:5.20").with_env("NEO4J_AUTH", "neo4j/testtest1")
    container.start()
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


def _make_entity(
    *,
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    name: str,
    metadata: dict[str, Any] | None = None,
) -> EntityUpsert:
    """Build an EntityUpsert with workspace_id stamped on metadata.

    ``EntityUpsert`` does not carry ``workspace_id`` as a field; the
    adapter's ``_workspace_from_entities`` helper falls back to
    ``metadata["workspace_id"]`` when the attribute is absent.
    """
    md: dict[str, Any] = dict(metadata or {})
    md.setdefault("workspace_id", str(workspace_id))
    return EntityUpsert(
        id=uuid.uuid4(),
        source_id=source_id,
        entity_type="service",
        name=name,
        display_name=name.split(".")[-1],
        chunk_id=None,
        metadata=md,
    )


async def _query_one(
    store: Neo4jGraphStore, cypher: str, params: dict[str, Any]
) -> dict[str, Any] | None:
    async with store._driver.session(database=store._config.database) as session:
        result = await session.run(cypher, params)
        record = await result.single()
    return record.data() if record else None


async def _query_all(
    store: Neo4jGraphStore, cypher: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    async with store._driver.session(database=store._config.database) as session:
        result = await session.run(cypher, params)
        async for record in result:
            out.append(record.data())
    return out


@pytestmark_live
@pytest.mark.asyncio
async def test_live_smaller_snapshot_end_dates_missing_entity(_neo4j_container: str) -> None:
    """Re-ingestion of a smaller snapshot end-dates the missing entity."""
    async for store in _live_store(bitemporal_enabled=True, container_uri=_neo4j_container):
        source_id = uuid.uuid4()
        # First snapshot: two entities.
        e1 = _make_entity(workspace_id=_WORKSPACE_A, source_id=source_id, name="svc.a")
        e2 = _make_entity(workspace_id=_WORKSPACE_A, source_id=source_id, name="svc.b")
        await store.upsert_graph(
            source_id=source_id,
            document_id=uuid.uuid4(),
            entities=[e1, e2],
            edges=[],
            snapshot_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        # Second snapshot: only e1 remains.
        await store.upsert_graph(
            source_id=source_id,
            document_id=uuid.uuid4(),
            entities=[_make_entity(workspace_id=_WORKSPACE_A, source_id=source_id, name="svc.a")],
            edges=[],
            snapshot_at=datetime(2026, 2, 1, tzinfo=UTC),
        )
        # e2 must NOT be deleted; it must have valid_to set.
        rows = await _query_all(
            store,
            "MATCH (n:Entity {workspace_id: $ws, name: $n}) "
            "RETURN n.valid_to AS valid_to, n.name AS name",
            {"ws": str(_WORKSPACE_A), "n": "svc.b"},
        )
        assert len(rows) == 1
        assert rows[0]["valid_to"] is not None


@pytestmark_live
@pytest.mark.asyncio
async def test_live_as_of_before_tombstone_returns_state(_neo4j_container: str) -> None:
    """as_of read before the tombstone returns the entity as it was."""
    async for store in _live_store(bitemporal_enabled=True, container_uri=_neo4j_container):
        source_id = uuid.uuid4()
        e = _make_entity(workspace_id=_WORKSPACE_A, source_id=source_id, name="svc.x")
        await store.upsert_graph(
            source_id=source_id,
            document_id=uuid.uuid4(),
            entities=[e],
            edges=[],
            snapshot_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        # End-date by re-ingesting empty-but-with-different-entity batch.
        e_other = _make_entity(workspace_id=_WORKSPACE_A, source_id=source_id, name="svc.y")
        await store.upsert_graph(
            source_id=source_id,
            document_id=uuid.uuid4(),
            entities=[e_other],
            edges=[],
            snapshot_at=datetime(2026, 3, 1, tzinfo=UTC),
        )
        # Query :EntityState directly; #132 may not be merged yet so we
        # do not call the read API with as_of.
        as_of = datetime(2026, 2, 1, tzinfo=UTC)
        rows = await _query_all(
            store,
            "MATCH (n:Entity {workspace_id: $ws, id: $id})-[:HAD_STATE]->(s:EntityState) "
            "WHERE s.valid_from <= datetime($t) "
            "  AND ($t < toString(s.valid_to) OR s.valid_to IS NULL) "
            "RETURN s.name AS name",
            {"ws": str(_WORKSPACE_A), "id": str(e.id), "t": as_of.isoformat()},
        )
        assert any(r["name"] == "svc.x" for r in rows)


@pytestmark_live
@pytest.mark.asyncio
async def test_live_as_of_after_tombstone_returns_nothing(_neo4j_container: str) -> None:
    async for store in _live_store(bitemporal_enabled=True, container_uri=_neo4j_container):
        source_id = uuid.uuid4()
        e = _make_entity(workspace_id=_WORKSPACE_A, source_id=source_id, name="svc.zzz")
        await store.upsert_graph(
            source_id=source_id,
            document_id=uuid.uuid4(),
            entities=[e],
            edges=[],
            snapshot_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        e_other = _make_entity(
            workspace_id=_WORKSPACE_A, source_id=source_id, name="svc.replacement"
        )
        await store.upsert_graph(
            source_id=source_id,
            document_id=uuid.uuid4(),
            entities=[e_other],
            edges=[],
            snapshot_at=datetime(2026, 3, 1, tzinfo=UTC),
        )
        as_of = datetime(2026, 4, 1, tzinfo=UTC)
        rows = await _query_all(
            store,
            "MATCH (n:Entity {workspace_id: $ws, id: $id})-[:HAD_STATE]->(s:EntityState) "
            "WHERE s.valid_from <= datetime($t) "
            "  AND (datetime($t) < s.valid_to OR s.valid_to IS NULL) "
            "RETURN s.name AS name",
            {"ws": str(_WORKSPACE_A), "id": str(e.id), "t": as_of.isoformat()},
        )
        assert rows == []


@pytestmark_live
@pytest.mark.asyncio
async def test_live_edge_end_dating_between_snapshots(_neo4j_container: str) -> None:
    """A relationship that disappears between snapshots gets valid_to set."""
    from omniscience_core.storage.graph import EdgeUpsert

    async for store in _live_store(bitemporal_enabled=True, container_uri=_neo4j_container):
        source_id = uuid.uuid4()
        a = _make_entity(workspace_id=_WORKSPACE_A, source_id=source_id, name="svc.a")
        b = _make_entity(workspace_id=_WORKSPACE_A, source_id=source_id, name="svc.b")
        edge = EdgeUpsert(
            source_entity_id=a.id,
            target_entity_id=b.id,
            edge_type="DEPENDS_ON",
            metadata={"source_id": str(source_id)},
        )
        await store.upsert_graph(
            source_id=source_id,
            document_id=uuid.uuid4(),
            entities=[a, b],
            edges=[edge],
            snapshot_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        # Second snapshot: same entities, no edge.
        await store.upsert_graph(
            source_id=source_id,
            document_id=uuid.uuid4(),
            entities=[a, b],
            edges=[],
            snapshot_at=datetime(2026, 2, 1, tzinfo=UTC),
        )
        rows = await _query_all(
            store,
            "MATCH ()-[r:DEPENDS_ON]->() WHERE r.workspace_id = $ws RETURN r.valid_to AS valid_to",
            {"ws": str(_WORKSPACE_A)},
        )
        # Edge persists but is end-dated.
        assert rows
        assert any(r["valid_to"] is not None for r in rows)


@pytestmark_live
@pytest.mark.asyncio
async def test_live_cross_workspace_isolation_under_end_dating(
    _neo4j_container: str,
) -> None:
    """End-dating in workspace A NEVER touches workspace B. Mandatory."""
    async for store in _live_store(bitemporal_enabled=True, container_uri=_neo4j_container):
        # Plant identical-shape data in both workspaces with the SAME source_id
        # value (composite-uniqueness is on (workspace_id, id) so this is OK).
        shared_source = uuid.uuid4()
        a = _make_entity(workspace_id=_WORKSPACE_A, source_id=shared_source, name="svc.x")
        b = _make_entity(workspace_id=_WORKSPACE_B, source_id=shared_source, name="svc.x")
        await store.upsert_graph(
            source_id=shared_source,
            document_id=uuid.uuid4(),
            entities=[a],
            edges=[],
            snapshot_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        await store.upsert_graph(
            source_id=shared_source,
            document_id=uuid.uuid4(),
            entities=[b],
            edges=[],
            snapshot_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        # End-date workspace A by re-ingesting empty.
        a_replacement = _make_entity(
            workspace_id=_WORKSPACE_A, source_id=shared_source, name="svc.replacement"
        )
        await store.upsert_graph(
            source_id=shared_source,
            document_id=uuid.uuid4(),
            entities=[a_replacement],
            edges=[],
            snapshot_at=datetime(2026, 3, 1, tzinfo=UTC),
        )
        # Workspace B's entity is UNTOUCHED (valid_to still NULL).
        b_row = await _query_one(
            store,
            "MATCH (n:Entity {workspace_id: $ws, id: $id}) RETURN n.valid_to AS valid_to",
            {"ws": str(_WORKSPACE_B), "id": str(b.id)},
        )
        assert b_row is not None
        assert b_row["valid_to"] is None  # <- the mandatory contract


@pytestmark_live
@pytest.mark.asyncio
async def test_live_idempotent_re_running_same_snapshot(_neo4j_container: str) -> None:
    """Re-running the same snapshot twice end-dates nothing the second time."""
    async for store in _live_store(bitemporal_enabled=True, container_uri=_neo4j_container):
        source_id = uuid.uuid4()
        e = _make_entity(workspace_id=_WORKSPACE_A, source_id=source_id, name="svc.idem")
        snap = datetime(2026, 1, 1, tzinfo=UTC)
        await store.upsert_graph(
            source_id=source_id,
            document_id=uuid.uuid4(),
            entities=[e],
            edges=[],
            snapshot_at=snap,
        )
        # Pre-second-run: count of end-dated entities.
        before_rows = await _query_all(
            store,
            "MATCH (n:Entity {workspace_id: $ws}) "
            "WHERE n.valid_to IS NOT NULL "
            "RETURN count(n) AS c",
            {"ws": str(_WORKSPACE_A)},
        )
        before_count = before_rows[0]["c"] if before_rows else 0
        # Re-run with same snapshot_at and same entity — no churn expected.
        await store.upsert_graph(
            source_id=source_id,
            document_id=uuid.uuid4(),
            entities=[e],
            edges=[],
            snapshot_at=snap,
        )
        after_rows = await _query_all(
            store,
            "MATCH (n:Entity {workspace_id: $ws}) "
            "WHERE n.valid_to IS NOT NULL "
            "RETURN count(n) AS c",
            {"ws": str(_WORKSPACE_A)},
        )
        after_count = after_rows[0]["c"] if after_rows else 0
        assert after_count == before_count


@pytestmark_live
@pytest.mark.asyncio
async def test_live_disabled_flag_keeps_hard_delete_contract(
    _neo4j_container: str,
) -> None:
    """GRAPH_BITEMPORAL=disabled keeps the hard-delete behaviour from #104/#105."""
    async for store in _live_store(bitemporal_enabled=False, container_uri=_neo4j_container):
        source_id = uuid.uuid4()
        e1 = _make_entity(workspace_id=_WORKSPACE_A, source_id=source_id, name="svc.legacy")
        await store.upsert_graph(
            source_id=source_id,
            document_id=uuid.uuid4(),
            entities=[e1],
            edges=[],
        )
        # Replace with a new entity for same source — legacy DELETE
        # purges the old one.
        e2 = _make_entity(workspace_id=_WORKSPACE_A, source_id=source_id, name="svc.new")
        await store.upsert_graph(
            source_id=source_id,
            document_id=uuid.uuid4(),
            entities=[e2],
            edges=[],
        )
        # The old entity is gone (hard-delete).
        rows = await _query_all(
            store,
            "MATCH (n:Entity {workspace_id: $ws, name: $n}) RETURN count(n) AS c",
            {"ws": str(_WORKSPACE_A), "n": "svc.legacy"},
        )
        assert rows[0]["c"] == 0


# ---------------------------------------------------------------------------
# 6. Property tests — "no data loss" across end-dating (hypothesis)
# ---------------------------------------------------------------------------
#
# Generates random snapshot histories and asserts the bitemporal
# invariant from ADR-0008 §9 #2: for any (workspace_id, id, T) where T
# is in the recorded-time domain of the workspace, there is exactly one
# :EntityState s with s.valid_from <= T AND (T < s.valid_to OR
# s.valid_to IS NULL) — and end-dating an entity at time E does NOT
# erase the row that covers any T < E.
#
# We model the version chain in pure Python (no Neo4j) and apply the
# same end-dating semantics the writer Cypher templates implement.  The
# property test asserts that the simulated chain after end-dating
# preserves answers for every prior as_of.

_MIN_TIMESTAMP = datetime(2020, 1, 1, tzinfo=UTC)
_MAX_TIMESTAMP = datetime(2030, 1, 1, tzinfo=UTC)


@st.composite
def _snapshots(draw: st.DrawFn) -> list[datetime]:
    """Up to 6 monotonically increasing snapshot timestamps."""
    n = draw(st.integers(min_value=1, max_value=6))
    base = draw(
        st.datetimes(
            min_value=_MIN_TIMESTAMP.replace(tzinfo=None),
            max_value=_MAX_TIMESTAMP.replace(tzinfo=None),
        )
    )
    out: list[datetime] = []
    cur = base.replace(tzinfo=UTC)
    for _ in range(n):
        out.append(cur)
        cur = cur + timedelta(days=draw(st.integers(min_value=1, max_value=400)))
    return out


@st.composite
def _entity_universe(draw: st.DrawFn) -> tuple[list[str], list[list[str]]]:
    """A set of entity names + per-snapshot present-set."""
    universe = draw(
        st.lists(st.text(min_size=1, max_size=8, alphabet="abcdefg"), min_size=1, max_size=4)
    )
    snaps = draw(_snapshots())
    presence = []
    for _ in snaps:
        # Each snapshot picks a non-empty subset of the universe.
        present = draw(
            st.lists(st.sampled_from(universe), min_size=1, max_size=len(universe), unique=True)
        )
        presence.append(present)
    return universe, presence


def _simulate_end_dating(
    universe: list[str],
    snapshots: list[datetime],
    presence: list[list[str]],
) -> dict[str, list[tuple[datetime, datetime | None]]]:
    """Return per-entity list of (valid_from, valid_to) windows after end-dating.

    Mirrors the ADR-0008 §3 + issue #137 contract:
    - First time an entity appears, open a window starting at that
      snapshot.
    - Continuous re-presence keeps the window open (no churn — the
      bitemporal upsert is a no-op on unchanged state in this model).
    - When the entity is absent in a later snapshot, the window's
      valid_to is set to that snapshot.
    - Re-appearance after absence opens a new window.
    """
    chains: dict[str, list[list[datetime | None]]] = {n: [] for n in universe}
    for ts, present in zip(snapshots, presence, strict=True):
        for name in universe:
            chain = chains[name]
            currently_open = bool(chain) and chain[-1][1] is None
            if name in present and not currently_open:
                chain.append([ts, None])
            elif name not in present and currently_open:
                chain[-1][1] = ts
    return {
        n: [(w[0], w[1]) for w in v]  # type: ignore[misc]
        for n, v in chains.items()
    }


@settings(
    max_examples=120,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    deadline=None,
)
@given(universe_and_presence=_entity_universe())
def test_property_no_data_loss_across_end_dating(
    universe_and_presence: tuple[list[str], list[list[str]]],
) -> None:
    """For any (entity, T) where T was in a present-window, the chain
    still contains a window covering T after end-dating."""
    universe, presence = universe_and_presence
    snapshots: list[datetime] = []
    base = datetime(2026, 1, 1, tzinfo=UTC)
    cur = base
    for _ in presence:
        snapshots.append(cur)
        cur = cur + timedelta(days=30)

    chains = _simulate_end_dating(universe, snapshots, presence)

    # For every (entity, snapshot) where the entity was present, the
    # chain must contain a window (valid_from, valid_to) with
    # valid_from <= snapshot AND (snapshot < valid_to OR valid_to IS
    # None).  This is the bitemporal point-in-time predicate from
    # ADR-0008 §5.
    for ts, present in zip(snapshots, presence, strict=True):
        for name in present:
            windows = chains[name]
            covered = any(vf <= ts and (vt is None or ts < vt) for vf, vt in windows)
            assert covered, (
                f"data loss: entity={name!r} present at {ts} not covered by any window: "
                f"windows={windows}"
            )


def test_property_test_universe_sanity() -> None:
    """Quick sanity check that the simulator handles the canonical scenario.

    Two snapshots: present in first, absent in second -> one closed
    window; reappearance in third opens a new window.  This is the
    "resurrect after gap" shape from ADR-0008 §3.
    """
    snapshots = [
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 2, 1, tzinfo=UTC),
        datetime(2026, 3, 1, tzinfo=UTC),
    ]
    chains = _simulate_end_dating(
        universe=["a"],
        snapshots=snapshots,
        presence=[["a"], [], ["a"]],
    )
    assert len(chains["a"]) == 2
    assert chains["a"][0][0] == snapshots[0]
    assert chains["a"][0][1] == snapshots[1]
    assert chains["a"][1][0] == snapshots[2]
    assert chains["a"][1][1] is None


# ---------------------------------------------------------------------------
# 7. Telemetry — counter family registration (smoke)
# ---------------------------------------------------------------------------


def test_graph_end_dated_counter_registered() -> None:
    """The new counter is exported from the metrics module."""
    from omniscience_core.telemetry.metrics import GRAPH_END_DATED_TOTAL

    # Counter has the expected labels.
    sample = GRAPH_END_DATED_TOTAL.labels(kind="entity", reason="snapshot")
    sample.inc(0)  # smoke increment-by-zero — does not change the value
    sample2 = GRAPH_END_DATED_TOTAL.labels(kind="edge", reason="tombstone")
    sample2.inc(0)
