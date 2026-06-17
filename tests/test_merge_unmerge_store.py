"""Unit tests for merge and unmerge capabilities in Neo4j store."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_index.stores.neo4j.store import Neo4jGraphStore, Neo4jStoreConfig


def _make_store_with_mock_driver() -> tuple[Neo4jGraphStore, MagicMock]:
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
    driver_mock._session_mock = session_mock

    config = Neo4jStoreConfig(
        uri="bolt://localhost:7687",
        username="neo4j",
        password="pw",
        database="neo4j",
        max_connection_pool_size=10,
        connection_acquisition_timeout_seconds=30.0,
        max_transaction_retry_time_seconds=30.0,
        default_max_depth=3,
    )
    store = Neo4jGraphStore(config=config)
    store._driver = driver_mock
    return store, driver_mock


@pytest.mark.asyncio
async def test_merge_nodes_failure_when_not_exist() -> None:
    store, driver_mock = _make_store_with_mock_driver()
    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()
    target_id = uuid.uuid4()

    # If the existence check returns empty, merge should return False
    driver_mock._session_mock.execute_read.return_value = []

    success = await store.merge_nodes(
        workspace_id=workspace_id,
        source_id=source_id,
        target_id=target_id,
    )
    assert success is False


@pytest.mark.asyncio
async def test_merge_nodes_success() -> None:
    store, driver_mock = _make_store_with_mock_driver()
    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()
    target_id = uuid.uuid4()

    # The existence check returns the nodes
    driver_mock._session_mock.execute_read.return_value = [
        {"aid": str(source_id), "bid": str(target_id)}
    ]

    # Mock transaction run return value
    tx_mock = MagicMock()
    tx_mock.run = AsyncMock()

    async def side_effect(func, *args, **kwargs):
        return await func(tx_mock)

    driver_mock._session_mock.execute_write.side_effect = side_effect

    success = await store.merge_nodes(
        workspace_id=workspace_id,
        source_id=source_id,
        target_id=target_id,
    )
    assert success is True
    assert tx_mock.run.call_count >= 1


@pytest.mark.asyncio
async def test_unmerge_node_failure_when_not_merged() -> None:
    store, driver_mock = _make_store_with_mock_driver()
    workspace_id = uuid.uuid4()
    merged_node_id = uuid.uuid4()

    # Check query returns nothing (not merged)
    driver_mock._session_mock.execute_read.return_value = []

    success = await store.unmerge_node(
        workspace_id=workspace_id,
        merged_node_id=merged_node_id,
    )
    assert success is False


@pytest.mark.asyncio
async def test_unmerge_node_success() -> None:
    store, driver_mock = _make_store_with_mock_driver()
    workspace_id = uuid.uuid4()
    merged_node_id = uuid.uuid4()
    target_id = uuid.uuid4()

    # Check query returns the target node ID
    driver_mock._session_mock.execute_read.return_value = [{"target_id": str(target_id)}]

    tx_mock = MagicMock()
    tx_mock.run = AsyncMock()

    async def side_effect(func, *args, **kwargs):
        return await func(tx_mock)

    driver_mock._session_mock.execute_write.side_effect = side_effect

    success = await store.unmerge_node(
        workspace_id=workspace_id,
        merged_node_id=merged_node_id,
    )
    assert success is True
    assert tx_mock.run.call_count >= 1
