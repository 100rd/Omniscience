#!/usr/bin/env python3
"""Auto-rebuild script to completely sync Neo4j and Qdrant from Postgres records.

Wipes Neo4j and Qdrant, then rebuilds them from active Postgres records.
Usage:
    python scripts/rebuild_all_projections.py --yes

WARNING — SANCTIONED DR EXCEPTION (ADR-0015)
=============================================
This script writes directly to Neo4j and Qdrant, bypassing the outbox
single-writer invariant (AP1, consilium-v8).  This is permitted ONLY when:

  1. Both Neo4j and Qdrant have been (or will be) completely wiped.
  2. No other writer process is running during the rebuild.
  3. Postgres is the source of truth and is known-good.

After the rebuild completes, trigger a reconcile scan to verify projections:
    POST /admin/reconcile/trigger

See docs/decisions/0015-rebuild-direct-write-exception.md for rationale.
"""

import asyncio
import sys
import uuid

from omniscience_core.config import Settings
from omniscience_core.db import create_async_engine, create_session_factory
from omniscience_core.db.models import Chunk, Document, Edge, Entity, Source, SourceStatus
from omniscience_embeddings import create_embedding_provider
from omniscience_index.stores.neo4j_store import Neo4jGraphStore, Neo4jStoreConfig
from omniscience_index.stores.qdrant_config import QdrantConfig
from omniscience_index.stores.qdrant_constants import COLLECTION_NAME_PREFIX
from omniscience_index.stores.qdrant_store import QdrantVectorStore
from qdrant_client import AsyncQdrantClient
from sqlalchemy import select


class EntityWrapper:
    """Duck-typed wrapper for Entity to expose required fields for Cypher upsert."""

    def __init__(self, db_entity: Entity, workspace_id: uuid.UUID) -> None:
        self.id = db_entity.id
        self.chunk_id = db_entity.chunk_id
        self.entity_type = db_entity.entity_type
        self.name = db_entity.name
        self.display_name = db_entity.display_name
        self.metadata = dict(db_entity.entity_metadata or {})
        self.metadata["workspace_id"] = str(workspace_id)
        self.workspace_id = workspace_id


class EdgeWrapper:
    """Duck-typed wrapper for Edge to expose required fields for Cypher upsert."""

    def __init__(self, db_edge: Edge, workspace_id: uuid.UUID) -> None:
        self.source_entity_id = db_edge.source_entity_id
        self.target_entity_id = db_edge.target_entity_id
        self.edge_type = db_edge.edge_type
        self.metadata = dict(db_edge.edge_metadata or {})
        self.metadata["workspace_id"] = str(workspace_id)


async def main() -> None:
    if "--yes" not in sys.argv:
        print("Refusing to rebuild without --yes")
        sys.exit(1)

    settings = Settings()

    print("Wiping Neo4j and Qdrant...")

    # 1. Wipe Neo4j
    gs = Neo4jGraphStore(config=Neo4jStoreConfig.from_settings(settings))
    await gs.connect()
    async with gs._driver.session(database=gs._config.database) as session:
        result = await session.run("MATCH (n) DETACH DELETE n")
        summary = await result.consume()
        print(f"neo4j: deleted {summary.counters.nodes_deleted} nodes")
    await gs.close()

    # 2. Wipe Qdrant
    qdrant_client = AsyncQdrantClient(
        host=settings.qdrant_host,
        grpc_port=settings.qdrant_grpc_port,
        port=settings.qdrant_http_port,
        api_key=settings.qdrant_api_key,
        https=False,
        prefer_grpc=True,
    )
    for c in (await qdrant_client.get_collections()).collections:
        if c.name.startswith(COLLECTION_NAME_PREFIX):
            await qdrant_client.delete_collection(c.name)
            print(f"qdrant: dropped collection {c.name}")
    await qdrant_client.close()

    print("Rebuilding projections from Postgres...")

    # 3. Connect to stores for writing
    embedding_provider = create_embedding_provider(settings)
    qdrant_config = QdrantConfig(
        host=settings.qdrant_host,
        grpc_port=settings.qdrant_grpc_port,
        http_port=settings.qdrant_http_port,
        api_key=settings.qdrant_api_key,
        https=False,
        prefer_grpc=True,
    )
    vector_store = QdrantVectorStore(config=qdrant_config, embedding_provider=embedding_provider)
    await vector_store.connect()

    graph_store = Neo4jGraphStore(config=Neo4jStoreConfig.from_settings(settings))
    await graph_store.connect()

    # 4. Fetch data from Postgres and rebuild
    engine = create_async_engine(settings)
    session_factory = create_session_factory(engine)
    async with session_factory() as db_session:
        # Fetch active sources
        stmt = select(Source).where(Source.status == SourceStatus.active)
        result = await db_session.execute(stmt)
        sources = result.scalars().all()

        for source in sources:
            workspace_id = source.tenant_id
            if not workspace_id:
                print(
                    f"Skipping source {source.name} ({source.id}) - missing tenant_id/workspace_id"
                )
                continue

            print(f"Processing source: {source.name} (tenant/workspace: {workspace_id})")

            # Fetch active documents (not tombstoned)
            doc_stmt = select(Document).where(
                Document.source_id == source.id, Document.tombstoned_at.is_(None)
            )
            doc_result = await db_session.execute(doc_stmt)
            documents = doc_result.scalars().all()

            for doc in documents:
                print(f"  Rebuilding document: {doc.uri} (ext_id: {doc.external_id})")

                # Fetch chunks
                chunk_stmt = select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.ord)
                chunk_result = await db_session.execute(chunk_stmt)
                chunks = chunk_result.scalars().all()

                if not chunks:
                    continue

                # Prepare payloads
                payloads = []
                for chunk in chunks:
                    embedding = chunk.embedding
                    if not embedding:
                        print(f"    Generating embedding for chunk {chunk.id}...")
                        vectors = await embedding_provider.embed([chunk.text])
                        embedding = vectors[0]

                    payload = {
                        "ord": int(chunk.ord),
                        "text": str(chunk.text),
                        "embedding": embedding,
                        "symbol": chunk.symbol,
                        "metadata": dict(chunk.chunk_metadata or {}),
                        "embedding_model": str(chunk.embedding_model or ""),
                        "embedding_provider": str(chunk.embedding_provider or ""),
                        "parser_version": str(chunk.parser_version or ""),
                        "chunker_strategy": str(chunk.chunker_strategy or ""),
                    }
                    payloads.append(payload)

                # Upsert to vector store
                doc_metadata = dict(doc.doc_metadata or {})
                doc_metadata["workspace_id"] = str(workspace_id)
                await vector_store.upsert_chunks(
                    source_id=source.id,
                    external_id=doc.external_id,
                    uri=doc.uri,
                    title=doc.title,
                    content_hash=doc.content_hash,
                    metadata=doc_metadata,
                    chunks=payloads,
                    version=doc.doc_version,
                )

                # Fetch entities
                chunk_ids = [c.id for c in chunks]
                ent_stmt = select(Entity).where(Entity.chunk_id.in_(chunk_ids))
                ent_result = await db_session.execute(ent_stmt)
                entities = ent_result.scalars().all()

                # Fetch edges
                edges = []
                if entities:
                    ent_ids = [e.id for e in entities]
                    edge_stmt = select(Edge).where(Edge.source_entity_id.in_(ent_ids))
                    edge_result = await db_session.execute(edge_stmt)
                    edges = edge_result.scalars().all()

                # Wrap and upsert to graph store
                wrapped_entities = [EntityWrapper(ent, workspace_id) for ent in entities]
                wrapped_edges = [EdgeWrapper(edge, workspace_id) for edge in edges]

                if wrapped_entities:
                    await graph_store.upsert_graph(
                        source_id=source.id,
                        document_id=doc.id,
                        entities=wrapped_entities,
                        edges=wrapped_edges,
                        version=doc.doc_version,
                    )

    # 5. Clean up connections
    await vector_store.close()
    await graph_store.close()
    await engine.dispose()
    print("Rebuild complete.")


if __name__ == "__main__":
    asyncio.run(main())
