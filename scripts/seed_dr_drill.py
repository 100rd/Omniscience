#!/usr/bin/env python3
"""Seed a small, deterministic dataset for the DR drill CI workflow.

Inserts:
  - 1 workspace (fixed UUID)
  - 1 source (fixed UUID, status=active)
  - 3 documents with deterministic IDs and doc_version values
  - 3 chunks per document (9 chunks total)

No real embeddings are generated — a fixed-dimension zero vector is stored
directly so the drill runs without an embedding service.

Controlled by env var OMNISCIENCE_DR_DRILL=1 to prevent accidental
execution in a non-drill environment.
"""

from __future__ import annotations

import asyncio
import os
import uuid

from omniscience_core.config import Settings
from omniscience_core.db import create_async_engine, create_session_factory
from omniscience_core.db.models import Chunk, Document, Source, SourceStatus, SourceType

# Guard: only run in the DR drill environment
if not os.environ.get("OMNISCIENCE_DR_DRILL"):
    raise RuntimeError("OMNISCIENCE_DR_DRILL env var must be set to run the DR drill seed script.")

# Fixed UUIDs for deterministic verification
WORKSPACE_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
SOURCE_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")

DOC_IDS = [
    uuid.UUID("30000000-0000-0000-0000-000000000001"),
    uuid.UUID("30000000-0000-0000-0000-000000000002"),
    uuid.UUID("30000000-0000-0000-0000-000000000003"),
]

# 3 chunks per document; 9 total
CHUNKS_PER_DOC = 3

# Zero vector: must match the embedding dimension expected by Qdrant.
# The embedding provider in DR drill mode (OMNISCIENCE_EMBEDDING_PROVIDER=local)
# uses a 384-dim model; we supply the vector directly to Postgres so the
# rebuild script reads it from chunk.embedding instead of re-embedding.
EMBEDDING_DIM = 384
ZERO_EMBEDDING = [0.0] * EMBEDDING_DIM


async def seed() -> None:
    settings = Settings()
    engine = create_async_engine(settings)
    session_factory = create_session_factory(engine)

    async with session_factory() as session, session.begin():
        # Upsert source

        source = Source(
            id=SOURCE_ID,
            name="dr-drill-source",
            type=SourceType.git,
            config={"repo_url": "git://dr-drill"},
            tenant_id=WORKSPACE_ID,
            status=SourceStatus.active,
        )
        await session.merge(source)

        # Insert documents + chunks
        for idx, doc_id in enumerate(DOC_IDS):
            doc = Document(
                id=doc_id,
                source_id=SOURCE_ID,
                external_id=f"doc-{idx + 1}",
                uri=f"git://dr-drill/doc-{idx + 1}.md",
                title=f"DR Drill Document {idx + 1}",
                content_hash=f"hash-doc-{idx + 1}",
                doc_version=idx + 1,
                doc_metadata={"workspace_id": str(WORKSPACE_ID)},
            )
            await session.merge(doc)

            for ord_idx in range(CHUNKS_PER_DOC):
                chunk = Chunk(
                    id=uuid.uuid5(doc_id, f"chunk-{ord_idx}"),
                    document_id=doc_id,
                    ord=ord_idx,
                    text=f"Chunk text {idx + 1}.{ord_idx}",
                    embedding=ZERO_EMBEDDING,
                    embedding_model="local-384",
                    embedding_provider="local",
                    parser_version="1.0",
                    chunker_strategy="fixed",
                    chunk_metadata={"workspace_id": str(WORKSPACE_ID)},
                )
                await session.merge(chunk)

    print(
        f"DR drill seed complete: workspace={WORKSPACE_ID} source={SOURCE_ID}"
        f" docs={len(DOC_IDS)} chunks={len(DOC_IDS) * CHUNKS_PER_DOC}"
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
