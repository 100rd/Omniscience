"""Full wipe of all indexed data — Postgres, Qdrant, Neo4j.

Deletes every source, document, chunk, ingestion run, and (deprecated SQL)
entity/edge row, drops all omniscience-managed Qdrant collections (they are
recreated idempotently by the vector store on next connect), and clears the
Neo4j graph.  Auth and tenancy survive: workspaces, api_tokens, audit_log,
alembic_version are untouched.

Run inside the app container:
    /app/.venv/bin/python /tmp/wipe_all_index_data.py --yes
"""

import asyncio
import sys

from omniscience_core.config import Settings
from omniscience_core.db import create_async_engine
from omniscience_index.stores.neo4j_store import Neo4jGraphStore, Neo4jStoreConfig
from omniscience_index.stores.qdrant_constants import COLLECTION_NAME_PREFIX
from qdrant_client import AsyncQdrantClient
from sqlalchemy import text

WIPE_TABLES = [
    "chunks",
    "edges",
    "entities",
    "entity_emitter",
    "documents",
    "ingestion_runs",
    "sources",
]


async def main() -> None:
    if "--yes" not in sys.argv:
        print("Refusing to wipe without --yes")
        sys.exit(1)

    settings = Settings()

    engine = create_async_engine(settings)
    async with engine.begin() as conn:
        for table in WIPE_TABLES:
            # Table names come from the hardcoded WIPE_TABLES allow-list,
            # never from user input.
            result = await conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608
            print(f"postgres: {table}: deleted {result.rowcount} rows")
    await engine.dispose()

    client = AsyncQdrantClient(
        host=settings.qdrant_host,
        grpc_port=settings.qdrant_grpc_port,
        port=settings.qdrant_http_port,
        api_key=settings.qdrant_api_key,
        https=False,
        prefer_grpc=True,
    )
    for c in (await client.get_collections()).collections:
        if c.name.startswith(COLLECTION_NAME_PREFIX):
            await client.delete_collection(c.name)
            print(f"qdrant: dropped collection {c.name}")
    await client.close()

    gs = Neo4jGraphStore(config=Neo4jStoreConfig.from_settings(settings))
    await gs.connect()
    async with gs._driver.session(database=gs._config.database) as session:
        result = await session.run("MATCH (n) DETACH DELETE n")
        summary = await result.consume()
        print(f"neo4j: deleted {summary.counters.nodes_deleted} nodes")
    await gs.close()

    print("Wipe complete.")


if __name__ == "__main__":
    asyncio.run(main())
