import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_core.storage.graph import EntityUpsert
from omniscience_index.stores.neo4j.store import Neo4jGraphStore

pytestmark = pytest.mark.asyncio


def _iterable_result(*records: Any) -> AsyncMock:
    """An ``AsyncResult`` stand-in that yields ``records`` and supports ``single()``."""
    res_mock = AsyncMock()
    res_mock.single.return_value = records[0] if records else None

    def _aiter() -> Any:
        items = iter(records)

        class _Iter:
            async def __anext__(self) -> Any:
                try:
                    return next(items)
                except StopIteration:
                    raise StopAsyncIteration from None

        return _Iter()

    res_mock.__aiter__ = MagicMock(side_effect=_aiter)
    return res_mock


def _make_store_with_mock_driver() -> tuple[Neo4jGraphStore, MagicMock]:
    """Build a store whose async driver is mocked in place.

    Uses a MagicMock session_ctx with explicit __aenter__/__aexit__ so that
    ``async with driver.session(...) as session`` resolves to session_mock
    (not the auto-generated __aenter__() return value of an AsyncMock).
    """
    driver_mock = MagicMock()

    # tx_mock records run() calls inside the closures passed to execute_write
    tx_mock = MagicMock()
    tx_mock.run = AsyncMock()

    # execute_write receives a single closure; we must await it so tx_mock.run
    # call counts reflect what the production code actually executes.
    async def _fake_execute_write(fn, *args, **kwargs):
        await fn(tx_mock, *args, **kwargs)

    session_mock = MagicMock()
    session_mock.execute_write = AsyncMock(side_effect=_fake_execute_write)

    # Wrap in a proper async context manager
    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session_mock)
    session_ctx.__aexit__ = AsyncMock(return_value=None)
    driver_mock.session = MagicMock(return_value=session_ctx)

    config = MagicMock()
    config.uri = "bolt://mock"
    store = Neo4jGraphStore(config=config)
    store._driver = driver_mock
    return store, tx_mock


async def test_neo4j_epoch_forced_replay():
    store, tx_mock = _make_store_with_mock_driver()
    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()
    ent_id = uuid.uuid4()

    # 1. Write version 10, epoch 1
    ent = EntityUpsert(
        id=ent_id,
        source_id=source_id,
        entity_type="test_kind",
        name="test_1",
        display_name="test_1",
        chunk_id=None,
        metadata={},
        version=10,
        epoch=1,
    )

    # Mocking the checkpoint-read result.  The store consumes every row of the
    # result (duplicate-tolerant fold) rather than calling ``single()``, so the
    # mock must be async-iterable.
    record_mock = MagicMock()
    record_mock.__getitem__.side_effect = lambda key: (
        5 if key == "version" else 0
    )  # existing version 5, epoch 0
    res_mock = _iterable_result(record_mock)
    tx_mock.run.return_value = res_mock

    await store.upsert_entity(entity=ent, workspace_id=workspace_id)

    # Verify MERGE is called because 10 > 5
    assert tx_mock.run.call_count == 3  # check version, MERGE checkpoint, MERGE entity

    # 2. Write version 5, epoch 1 (Should skip)
    tx_mock.reset_mock()
    tx_mock.run.return_value = res_mock
    ent.version = 5
    record_mock.__getitem__.side_effect = lambda key: (
        10 if key == "version" else 1
    )  # existing 10, epoch 1
    await store.upsert_entity(entity=ent, workspace_id=workspace_id)
    assert tx_mock.run.call_count == 1  # Only the check, MERGE is skipped

    # 3. Write version 5, epoch 2 (Should pass due to new epoch)
    tx_mock.reset_mock()
    tx_mock.run.return_value = res_mock
    ent.version = 5
    ent.epoch = 2
    record_mock.__getitem__.side_effect = lambda key: (
        10 if key == "version" else 1
    )  # existing 10, epoch 1
    await store.upsert_entity(entity=ent, workspace_id=workspace_id)
    assert tx_mock.run.call_count == 3  # check, MERGE checkpoint, MERGE entity

    # 4. Write version 4, epoch 2 (Should skip)
    tx_mock.reset_mock()
    tx_mock.run.return_value = res_mock
    ent.version = 4
    record_mock.__getitem__.side_effect = lambda key: (
        5 if key == "version" else 2
    )  # existing 5, epoch 2
    await store.upsert_entity(entity=ent, workspace_id=workspace_id)
    assert tx_mock.run.call_count == 1

    # 5. Write version 3, epoch 2, forced_replay=True (Should pass)
    tx_mock.reset_mock()
    tx_mock.run.return_value = res_mock
    ent.version = 3
    ent.forced_replay = True
    await store.upsert_entity(entity=ent, workspace_id=workspace_id)
    assert tx_mock.run.call_count == 3
