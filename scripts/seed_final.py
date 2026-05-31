import asyncio
import uuid

from omniscience_core.config import Settings
from omniscience_core.db import create_async_engine, create_session_factory
from omniscience_core.db.models import Source, SourceStatus, SourceType
from omniscience_embeddings.factory import create_embedding_provider
from omniscience_index.stores.neo4j_store import Neo4jGraphStore, Neo4jStoreConfig
from omniscience_index.stores.qdrant_config import QdrantConfig
from omniscience_index.stores.qdrant_store import QdrantVectorStore
from omniscience_index.writer import ChunkData, IndexWriter
from sqlalchemy import delete


async def seed():
    settings = Settings()
    engine = create_async_engine(settings)
    session_factory = create_session_factory(engine)
    graph_store = Neo4jGraphStore(config=Neo4jStoreConfig.from_settings(settings))
    vector_store = QdrantVectorStore(
        config=QdrantConfig(host=settings.qdrant_host, grpc_port=settings.qdrant_grpc_port,
                           http_port=settings.qdrant_http_port, api_key=settings.qdrant_api_key),
        embedding_provider=create_embedding_provider(settings)
    )
    await graph_store.connect()
    await vector_store.connect()

    ws_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    src_id = uuid.uuid4()

    # Full cleanup of Neo4j to be sure
    async with graph_store._driver.session(database=graph_store._config.database) as session:
        await session.execute_write(lambda tx: tx.run("MATCH (n) DETACH DELETE n"))

    async with session_factory() as session:
        await session.execute(delete(Source).where(Source.name == "demo"))
        src = Source(
            id=src_id, name="demo", type=SourceType.git,
            config={}, tenant_id=ws_id, status=SourceStatus.active,
        )
        session.add(src)
        await session.commit()

    writer = IndexWriter(session_factory, graph_store, vector_store)

    class E:
        def __init__(self, name):
            self.id = uuid.uuid4()
            self.entity_type = "function"
            self.name = name
            self.display_name = name
            self.metadata = {}

    class Edge:
        def __init__(self, src_id, target_name, etype):
            self.source_entity_id = src_id
            self.target_name = target_name
            self.edge_type = etype
            self.metadata = {}

    # 1. Auth.py
    res_a = await writer.upsert_document(src_id, "auth.py", "git://auth.py", "Auth", "h1", {},
                                       [ChunkData(0, "def validate(): pass", [0.1]*768)], ws_id)
    await writer.upsert_graph(src_id, res_a.document_id, [E("auth.validate")], [], ws_id)

    # 2. Payments.py (with edge to auth.validate)
    res_p = await writer.upsert_document(src_id, "pay.py", "git://pay.py", "Pay", "h2", {},
                                       [ChunkData(0, "auth.validate()", [0.2]*768)], ws_id)
    p_ent = E("payments.process")
    await writer.upsert_graph(
        src_id, res_p.document_id, [p_ent],
        [Edge(p_ent.id, "auth.validate", "calls")], ws_id,
    )

    await graph_store.resolve_pending_stubs(workspace_id=ws_id)
    print("DEMO_READY")
    await graph_store.close()
    await vector_store.close()

if __name__ == "__main__":
    asyncio.run(seed())
