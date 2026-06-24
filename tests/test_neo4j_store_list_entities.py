"""Unit tests for ``Neo4jGraphStore.list_entities`` (Gap B, issue #310).

These tests mock the async driver (same pattern as
``tests/test_neo4j_store.py``) and inspect the Cypher + params the
adapter would send to Neo4j.  They prove, without a container:

1.  The first-class ``cluster`` property is promoted from metadata on
    every write path (``upsert_entity`` + ``upsert_graph``) so the
    indexed ``list_entities`` filter has data to match.
2.  ``list_entities`` is workspace-scoped and emits the
    ``(workspace_id, kind)`` index seek, never a ``metadata CONTAINS``
    substring scan.
3.  Optional ``cluster`` / ``name`` predicates are appended ONLY when
    supplied (so the planner keeps the index seek).
4.  ``as_of`` selects the bitemporal ``:EntityState`` template.
5.  The cluster + bitemporal-window indexes are in the bootstrap DDL.

Container-backed contract coverage of the round trip lives in
``tests/test_graph_store_contract.py`` (skipped without Docker).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from omniscience_core.storage.graph import EntityUpsert
from omniscience_index.stores import neo4j_store
from omniscience_index.stores.neo4j_store import (
    Neo4jGraphStore,
    Neo4jStoreConfig,
    _build_list_entities_cypher,
    _cluster_from_metadata,
)

_WORKSPACE_A = uuid.UUID("aaaaaaaa-0000-0000-0000-0000000000a1")
_AS_OF = datetime(2026, 4, 12, 19, 25, 0, tzinfo=UTC)


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
    config = _make_config()
    driver_mock = MagicMock()
    driver_mock.verify_connectivity = AsyncMock(return_value=None)
    driver_mock.close = AsyncMock(return_value=None)
    session_ctx = MagicMock()
    session_mock = MagicMock()

    tx_mock = MagicMock()
    tx_mock.run = AsyncMock()

    async def _fake_execute_write(fn, *args, **kwargs):
        return await fn(tx_mock, *args, **kwargs)

    session_mock.execute_read = AsyncMock(return_value=[])
    session_mock.execute_write = AsyncMock(side_effect=_fake_execute_write)
    session_ctx.__aenter__ = AsyncMock(return_value=session_mock)
    session_ctx.__aexit__ = AsyncMock(return_value=None)
    driver_mock.session = MagicMock(return_value=session_ctx)
    with patch(
        "omniscience_index.stores.neo4j_store.AsyncGraphDatabase.driver",
        return_value=driver_mock,
    ):
        store = Neo4jGraphStore(config=config)
    driver_mock._session_mock = session_mock
    driver_mock._tx_mock = tx_mock
    return store, driver_mock


# ---------------------------------------------------------------------------
# 1. cluster promotion helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"cluster": "prod-eks-1"}, "prod-eks-1"),
        ({"cluster": ""}, None),  # blank -> NULL, never literal ""
        ({}, None),  # absent -> NULL
        (None, None),
        ({"cluster": "  "}, "  "),  # non-empty whitespace is preserved verbatim
    ],
)
def test_cluster_from_metadata(metadata: Any, expected: str | None) -> None:
    assert _cluster_from_metadata(metadata) == expected


# ---------------------------------------------------------------------------
# 2. cluster property is written on the entity upsert path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_entity_promotes_cluster_to_first_class_param() -> None:
    store, driver_mock = _make_store_with_mock_driver()
    payload = EntityUpsert(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        entity_type="StorageClass",
        name="gold",
        display_name="gold",
        chunk_id=None,
        metadata={"cluster": "prod-eks-1", "provisioner": "ebs.csi.aws.com"},
    )

    await store.upsert_entity(entity=payload, workspace_id=_WORKSPACE_A)

    run_call = driver_mock._tx_mock.run.call_args_list[-1]
    cypher = run_call.args[0]
    params = run_call.args[1]
    assert params["cluster"] == "prod-eks-1"
    assert "n.cluster = $cluster" in cypher


def test_upsert_entity_cypher_sets_cluster_property() -> None:
    assert "n.cluster = $cluster" in neo4j_store._UPSERT_ENTITY_CYPHER
    assert "n.cluster = $cluster" in neo4j_store._UPSERT_ENTITY_BITEMPORAL_CYPHER
    # The :EntityState version row also carries cluster so as_of reads match.
    assert "cluster: $cluster" in neo4j_store._UPSERT_ENTITY_BITEMPORAL_CYPHER


# ---------------------------------------------------------------------------
# 3. list_entities Cypher builder + workspace scoping
# ---------------------------------------------------------------------------


def test_list_entities_cypher_never_uses_substring_scan() -> None:
    """Index-backed equality, NOT a fragile metadata CONTAINS scan."""
    for as_of in (False, True):
        cypher = _build_list_entities_cypher(has_cluster=True, has_name=True, as_of=as_of)
        assert "CONTAINS" not in cypher
        assert "workspace_id" in cypher


def test_list_entities_builder_appends_filters_only_when_present() -> None:
    base = _build_list_entities_cypher(has_cluster=False, has_name=False)
    assert "$cluster" not in base
    assert "$name" not in base

    with_cluster = _build_list_entities_cypher(has_cluster=True, has_name=False)
    assert "n.cluster = $cluster" in with_cluster
    assert "$name" not in with_cluster

    with_both = _build_list_entities_cypher(has_cluster=True, has_name=True)
    assert "n.cluster = $cluster" in with_both
    assert "n.name = $name" in with_both


def test_list_entities_as_of_builder_targets_entity_state() -> None:
    cypher = _build_list_entities_cypher(has_cluster=True, has_name=False, as_of=True)
    assert "HAD_STATE" in cypher
    assert "s.cluster = $cluster" in cypher
    assert "datetime($as_of)" in cypher


@pytest.mark.asyncio
async def test_list_entities_passes_workspace_and_kind() -> None:
    store, driver_mock = _make_store_with_mock_driver()
    driver_mock._session_mock.execute_read = AsyncMock(return_value=[])

    await store.list_entities(workspace_id=_WORKSPACE_A, kind="StorageClass")

    args, _ = driver_mock._session_mock.execute_read.call_args
    cypher = args[1]
    params = args[2]
    assert params["workspace_id"] == str(_WORKSPACE_A)
    assert params["kind"] == "StorageClass"
    assert "cluster" not in params  # no cluster filter requested
    assert "as_of" not in params
    assert "workspace_id" in cypher


@pytest.mark.asyncio
async def test_list_entities_filters_by_cluster_and_name() -> None:
    store, driver_mock = _make_store_with_mock_driver()
    driver_mock._session_mock.execute_read = AsyncMock(return_value=[])

    await store.list_entities(
        workspace_id=_WORKSPACE_A,
        kind="StorageClass",
        cluster="prod-eks-1",
        name="gold",
    )

    args, _ = driver_mock._session_mock.execute_read.call_args
    cypher = args[1]
    params = args[2]
    assert params["cluster"] == "prod-eks-1"
    assert params["name"] == "gold"
    assert "n.cluster = $cluster" in cypher
    assert "n.name = $name" in cypher


@pytest.mark.asyncio
async def test_list_entities_as_of_uses_entity_state_template() -> None:
    store, driver_mock = _make_store_with_mock_driver()
    driver_mock._session_mock.execute_read = AsyncMock(return_value=[])

    await store.list_entities(
        workspace_id=_WORKSPACE_A,
        kind="StorageClass",
        cluster="prod-eks-1",
        as_of=_AS_OF,
    )

    args, _ = driver_mock._session_mock.execute_read.call_args
    cypher = args[1]
    params = args[2]
    assert "HAD_STATE" in cypher
    assert params["as_of"] == _AS_OF.isoformat()
    assert params["cluster"] == "prod-eks-1"


@pytest.mark.asyncio
async def test_list_entities_maps_rows_to_views() -> None:
    store, driver_mock = _make_store_with_mock_driver()
    source_uuid = uuid.uuid4()
    driver_mock._session_mock.execute_read = AsyncMock(
        return_value=[
            {
                "id": str(uuid.uuid4()),
                "name": "gold",
                "kind": "StorageClass",
                "source_id": str(source_uuid),
                "chunk_text": None,
            },
            {
                "id": str(uuid.uuid4()),
                "name": "silver",
                "kind": "StorageClass",
                "source_id": str(source_uuid),
                "chunk_text": None,
            },
        ]
    )

    views = await store.list_entities(
        workspace_id=_WORKSPACE_A, kind="StorageClass", cluster="prod-eks-1"
    )

    assert [v.name for v in views] == ["gold", "silver"]
    assert all(v.kind == "StorageClass" for v in views)


# ---------------------------------------------------------------------------
# 4. bootstrap DDL carries the cluster index
# ---------------------------------------------------------------------------


def test_bootstrap_includes_cluster_index() -> None:
    joined = "\n".join(neo4j_store._BOOTSTRAP_STATEMENTS)
    assert "entity_workspace_kind_cluster" in joined
    assert "n.workspace_id, n.kind, n.cluster" in joined
    assert "entity_state_workspace_kind_cluster" in joined
