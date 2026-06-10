"""Seed pre-fetched documents (JSON) into Omniscience with real Ollama embeddings.

Reads /tmp/docs.json (list of {external_id, uri, title, text, metadata}) and a source
name (+ optional source type, default "k8s") from argv, then upserts each as a
single-chunk document via IndexWriter.
Run inside the app container:
    /app/.venv/bin/python /tmp/seed_from_docs.py <source_name> [<source_type>]
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
from sqlalchemy import select

WS_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
SOURCE_NAME = sys.argv[1] if len(sys.argv) > 1 else "external-docs"
SOURCE_TYPE = SourceType(sys.argv[2]) if len(sys.argv) > 2 else SourceType.k8s


async def seed() -> None:
    docs = json.load(open("/tmp/docs.json"))
    settings = Settings()
    engine = create_async_engine(settings)
    session_factory = create_session_factory(engine)
    provider = create_embedding_provider(settings)
    graph_store = Neo4jGraphStore(config=Neo4jStoreConfig.from_settings(settings))
    vector_store = QdrantVectorStore(
        config=QdrantConfig(
            host=settings.qdrant_host,
            grpc_port=settings.qdrant_grpc_port,
            http_port=settings.qdrant_http_port,
            api_key=settings.qdrant_api_key,
        ),
        embedding_provider=provider,
    )
    await graph_store.connect()
    await vector_store.connect()

    # Get-or-create the source: the (source_id, external_id) pair is the
    # IndexWriter dedup key, so the source id must survive across runs —
    # a fresh id per run orphans every previous run's Qdrant points.
    async with session_factory() as session:
        src = (
            await session.execute(
                select(Source).where(Source.tenant_id == WS_ID, Source.name == SOURCE_NAME)
            )
        ).scalar_one_or_none()
        if src is None:
            src = Source(
                id=uuid.uuid4(),
                name=SOURCE_NAME,
                type=SOURCE_TYPE,
                config={"seeded_by": "seed_from_docs"},
                tenant_id=WS_ID,
                status=SourceStatus.active,
                # 30 min SLA enables FreshnessChecker staleness alerts.
                freshness_sla_seconds=1800,
            )
            session.add(src)
            await session.commit()
        src_id = src.id

    writer = IndexWriter(session_factory, graph_store, vector_store)
    model = getattr(settings, "ollama_embedding_model", None) or "nomic-embed-text"

    # Batch-embed all texts for speed.
    texts = [d["text"] for d in docs]
    vectors = await provider.embed(texts)
    actions: dict[str, int] = {}
    for i, d in enumerate(docs):
        chunk = ChunkData(
            ord=0,
            text=d["text"],
            embedding=vectors[i],
            embedding_model=model,
            embedding_provider="ollama",
            metadata=d.get("metadata", {}),
        )
        result = await writer.upsert_document(
            source_id=src_id,
            external_id=d["external_id"],
            uri=d["uri"],
            title=d.get("title"),
            content_hash=hashlib.sha256(d["text"].encode()).hexdigest(),
            metadata=d.get("metadata", {}),
            chunks=[chunk],
            workspace_id=WS_ID,
        )
        actions[result.action] = actions.get(result.action, 0) + 1
    print(
        f"Seeded {len(docs)} documents into source '{SOURCE_NAME}' (workspace {WS_ID}): {actions}"
    )
    await vector_store.close()
    await graph_store.close()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
