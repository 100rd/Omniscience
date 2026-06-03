import asyncio
import uuid

from omniscience_core.config import Settings
from omniscience_index.stores.neo4j_store import Neo4jGraphStore, Neo4jStoreConfig


async def resolve():
    settings = Settings()
    graph_store = Neo4jGraphStore(config=Neo4jStoreConfig.from_settings(settings))
    await graph_store.connect()

    ws_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    count = await graph_store.resolve_pending_stubs(workspace_id=ws_id)
    print(f"Resolved {count} stubs in workspace {ws_id}")

    await graph_store.close()


if __name__ == "__main__":
    asyncio.run(resolve())
