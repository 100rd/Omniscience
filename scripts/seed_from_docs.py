"""Seed pre-fetched documents (JSON) into Omniscience with real Ollama embeddings.

Reads /tmp/docs.json (list of {external_id, uri, title, text, metadata}) and a source
name from argv, then upserts each as a single-chunk document via IndexWriter.
Run inside the app container:  /app/.venv/bin/python /tmp/seed_from_docs.py <source_name>
"""

import asyncio
import hashlib
import json
import sys
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

WS_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
SOURCE_NAME = sys.argv[1] if len(sys.argv) > 1 else "external-docs"


async def seed() -> None:
    docs = json.load(open("/tmp/docs.json"))
    settings = Settings()
    engine = create_async_engine(settings)
    session_factory = create_session_factory(engine)
    provider = create_embedding_provider(settings)
    graph_store = Neo4jGraphStore(config=Neo4jStoreConfig.from_settings(settings))
    vector_store = QdrantVectorStore(
        config=QdrantConfig(host=settings.qdrant_host, grpc_port=settings.qdrant_grpc_port,
                            http_port=settings.qdrant_http_port, api_key=settings.qdrant_api_key),
        embedding_provider=provider,
    )
    await graph_store.connect()
    await vector_store.connect()

    src_id = uuid.uuid4()
    async with session_factory() as session:
        await session.execute(delete(Source).where(Source.name == SOURCE_NAME))
        session.add(Source(id=src_id, name=SOURCE_NAME, type=SourceType.k8s,
                           config={"cluster": "qbiq-shared"}, tenant_id=WS_ID,
                           status=SourceStatus.active))
        await session.commit()

    writer = IndexWriter(session_factory, graph_store, vector_store)
    model = getattr(settings, "ollama_embedding_model", None) or "nomic-embed-text"

    # Batch-embed all texts for speed.
    texts = [d["text"] for d in docs]
    vectors = await provider.embed(texts)
    for i, d in enumerate(docs):
        chunk = ChunkData(ord=0, text=d["text"], embedding=vectors[i],
                          embedding_model=model, embedding_provider="ollama",
                          metadata=d.get("metadata", {}))
        await writer.upsert_document(
            source_id=src_id, external_id=d["external_id"], uri=d["uri"],
            title=d.get("title"), content_hash=hashlib.sha256(d["text"].encode()).hexdigest(),
            metadata=d.get("metadata", {}), chunks=[chunk], workspace_id=WS_ID,
        )
    print(f"Seeded {len(docs)} documents into source '{SOURCE_NAME}' (workspace {WS_ID})")
    await vector_store.close()
    await graph_store.close()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
