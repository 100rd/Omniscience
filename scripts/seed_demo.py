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
        config=QdrantConfig(
            host=settings.qdrant_host,
            grpc_port=settings.qdrant_grpc_port,
            http_port=settings.qdrant_http_port,
            api_key=settings.qdrant_api_key,
        ),
        embedding_provider=create_embedding_provider(settings),
    )

    await graph_store.connect()
    await vector_store.connect()

    ws_id = uuid.UUID("00000000-0000-0000-0000-000000000001")

    # 1. Full cleanup of Neo4j to be sure
    async with graph_store._driver.session(database=graph_store._config.database) as session:
        await session.execute_write(lambda tx: tx.run("MATCH (n) DETACH DELETE n"))

    # 2. Create/Reset Source
    src_id = uuid.uuid4()
    async with session_factory() as session:
        await session.execute(delete(Source).where(Source.name == "omniscience-demo"))
        demo_source = Source(
            id=src_id,
            name="omniscience-demo",
            type=SourceType.git,
            config={"repo_url": "git://demo-repo"},
            tenant_id=ws_id,
            status=SourceStatus.active,
        )
        session.add(demo_source)
        await session.commit()

    writer = IndexWriter(session_factory, graph_store, vector_store)
    print(f"Seeding demo data for workspace {ws_id}...")

    # Data for Payments
    chunks_p = [
        ChunkData(
            ord=0,
            text="""def process_payment(card):
    auth.validate_token()
    print('Paid')""",
            embedding=[0.1] * 768,
        )
    ]

    res_p = await writer.upsert_document(
        source_id=src_id,
        external_id="payments.py",
        uri="git://demo/payments.py",
        title="Payments Module",
        content_hash="hash_p1",
        metadata={"lang": "python"},
        chunks=chunks_p,
        workspace_id=ws_id,
    )

    # Manual graph insertion to ensure stubs are created
    ent_p1_id = uuid.uuid4()

    # Mock entities
    class MockEnt:
        def __init__(self, eid, etype, name):
            self.id = eid
            self.entity_type = etype
            self.name = name
            self.display_name = name.split(".")[-1]
            self.metadata = {}
            self.chunk_id = None

    class MockEdge:
        def __init__(self, src_id, target_name, etype):
            self.source_entity_id = src_id
            self.target_name = target_name
            self.edge_type = etype
            self.metadata = {}

    # Upsert payments.py (creates stub for auth.validate_token)
    await writer.upsert_graph(
        source_id=src_id,
        document_id=res_p.document_id,
        entities=[MockEnt(ent_p1_id, "function", "payments.process_payment")],
        edges=[MockEdge(ent_p1_id, "auth.validate_token", "calls")],
        workspace_id=ws_id,
    )

    # Upsert auth.py (materializes the stub)
    chunks_a = [
        ChunkData(
            ord=0,
            text="""def validate_token():
    return True""",
            embedding=[0.3] * 768,
        )
    ]

    res_a = await writer.upsert_document(
        source_id=src_id,
        external_id="auth.py",
        uri="git://demo/auth.py",
        title="Auth Module",
        content_hash="hash_a1",
        metadata={"lang": "python"},
        chunks=chunks_a,
        workspace_id=ws_id,
    )

    await writer.upsert_graph(
        source_id=src_id,
        document_id=res_a.document_id,
        entities=[MockEnt(uuid.uuid4(), "function", "auth.validate_token")],
        edges=[],
        workspace_id=ws_id,
    )

    # 3. Resolve Stubs
    resolved = await graph_store.resolve_pending_stubs(workspace_id=ws_id)
    print(f"Seeding complete! Resolved {resolved} stubs.")

    await graph_store.close()
    await vector_store.close()


if __name__ == "__main__":
    asyncio.run(seed())
