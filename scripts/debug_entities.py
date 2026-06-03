import asyncio
import uuid

from omniscience_core.config import Settings
from omniscience_index.stores.neo4j_store import Neo4jGraphStore, Neo4jStoreConfig


async def list_all():
    settings = Settings()
    graph_store = Neo4jGraphStore(config=Neo4jStoreConfig.from_settings(settings))
    await graph_store.connect()

    ws_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    entities = await graph_store.get_all_entities(workspace_id=ws_id)
    print(f"Total entities: {len(entities)}")
    for e in entities:
        print(f"- {e.name} (Kind: {e.kind}, Source: {e.source})")

    await graph_store.close()


if __name__ == "__main__":
    asyncio.run(list_all())
