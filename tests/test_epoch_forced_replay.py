import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_core.storage.graph import EntityUpsert
from omniscience_index.stores.neo4j.store import Neo4jGraphStore

pytestmark = pytest.mark.asyncio


def _make_store_with_mock_driver() -> tuple[Neo4jGraphStore, MagicMock]:
    driver_mock = MagicMock()
    session_mock = AsyncMock()
    tx_mock = AsyncMock()
    session_mock.execute_write.side_effect = lambda f, *args, **kw: f(tx_mock, *args, **kw)
    driver_mock.session.return_value = session_mock
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

    # Mocking single() return from the version check
    record_mock = MagicMock()
    record_mock.__getitem__.side_effect = lambda key: (
        5 if key == "version" else 0
    )  # existing version 5, epoch 0
    res_mock = AsyncMock()
    res_mock.single.return_value = record_mock
    tx_mock.run.return_value = res_mock

    await store.upsert_entity(entity=ent, workspace_id=workspace_id)

    # Verify MERGE is called because 10 > 5
    assert tx_mock.run.call_count == 3  # check version, MERGE checkpoint, MERGE entity

    # 2. Write version 5, epoch 1 (Should skip)
    tx_mock.reset_mock()
    ent.version = 5
    record_mock.__getitem__.side_effect = lambda key: (
        10 if key == "version" else 1
    )  # existing 10, epoch 1
    await store.upsert_entity(entity=ent, workspace_id=workspace_id)
    assert tx_mock.run.call_count == 1  # Only the check, MERGE is skipped

    # 3. Write version 5, epoch 2 (Should pass due to new epoch)
    tx_mock.reset_mock()
    ent.version = 5
    ent.epoch = 2
    record_mock.__getitem__.side_effect = lambda key: (
        10 if key == "version" else 1
    )  # existing 10, epoch 1
    await store.upsert_entity(entity=ent, workspace_id=workspace_id)
    assert tx_mock.run.call_count == 3  # check, MERGE checkpoint, MERGE entity

    # 4. Write version 4, epoch 2 (Should skip)
    tx_mock.reset_mock()
    ent.version = 4
    record_mock.__getitem__.side_effect = lambda key: (
        5 if key == "version" else 2
    )  # existing 5, epoch 2
    await store.upsert_entity(entity=ent, workspace_id=workspace_id)
    assert tx_mock.run.call_count == 1

    # 5. Write version 3, epoch 2, forced_replay=True (Should pass)
    tx_mock.reset_mock()
    ent.version = 3
    ent.forced_replay = True
    await store.upsert_entity(entity=ent, workspace_id=workspace_id)
    assert tx_mock.run.call_count == 3
