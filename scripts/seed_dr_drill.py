#!/usr/bin/env python3
"""Seed a small, deterministic dataset for the DR drill CI workflow.

Inserts:
  - 1 workspace (fixed UUID)
  - 1 source (fixed UUID, status=active)
  - 3 documents with deterministic IDs and doc_version values
  - 3 chunks per document (9 chunks total)

AP5 change (consilium-v9 Batch D)
----------------------------------
The zero-vector bypass has been REMOVED.  Previously the seed stored
``embedding=[0.0] * 384`` directly, which meant the rebuild path that
checks ``if not chunk.embedding`` would always call the provider — but
the *value* stored was fake and did not test the real recompute-from-SoT
path.

Now the seed stores NO embedding (``embedding=None``/empty).  When the
drill runs ``rebuild_all_projections.py --recompute-embeddings``, every
chunk will be re-embedded from its ``chunk.text`` via the live provider.
When ``OMNISCIENCE_EMBEDDING_PROVIDER=local`` is set (the DR drill CI
env), the local model produces deterministic 384-dim vectors from the
text, exercising the true rebuild-from-SoT path.

The ``dr_verify.py --hash-assert`` step then compares the stored vectors
against fresh embeddings produced from the same SoT text, proving
hash-equivalence (AP5 invariant).

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
from sqlalchemy import insert

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


# AP5: chunk text templates.  Each chunk gets a deterministic text string
# derived from its document and ordinal index so the embedding provider
# produces the same vector on every rebuild from the same SoT text.
#
# The text must be non-empty and meaningful enough for the local embedding
# model to produce a stable non-zero vector.
def _chunk_text(doc_idx: int, ord_idx: int) -> str:
    """Return a deterministic, non-trivial text for the given chunk position.

    The text is long enough (~20 tokens) so that the local/sentence-transformer
    model produces a non-trivial embedding.  Using a fixed template guarantees
    that two rebuilds from the same Postgres SoT produce bit-identical vectors
    (the AP5 hash-equivalence invariant).
    """
    topics = [
        "database migration strategy for distributed systems",
        "kubernetes deployment configuration and rollout policy",
        "observability and alerting for microservice infrastructure",
    ]
    return (
        f"DR drill document {doc_idx + 1}, chunk {ord_idx}: "
        f"{topics[doc_idx % len(topics)]}. "
        f"This chunk exists to verify the rebuild-from-SoT path (AP5)."
    )


def _chunk_row(doc_idx: int, doc_id: uuid.UUID, ord_idx: int) -> dict[str, object]:
    """Build a row without naming the removed Postgres ``embedding`` column."""
    return {
        "id": uuid.uuid5(doc_id, f"chunk-{ord_idx}"),
        "document_id": doc_id,
        "ord": ord_idx,
        "text": _chunk_text(doc_idx, ord_idx),
        "embedding_model": "local-384",
        "embedding_provider": "local",
        "parser_version": "1.0",
        "chunker_strategy": "fixed",
        "chunk_metadata": {"workspace_id": str(WORKSPACE_ID)},
    }


async def seed() -> None:
    if not os.environ.get("OMNISCIENCE_DR_DRILL"):
        raise RuntimeError("OMNISCIENCE_DR_DRILL must be set to run the DR drill seed script.")

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

            # Core INSERT emits only supplied keys. ORM merge/select would name the
            # mapped legacy embedding attribute even though migration 0004 removed
            # that physical column for the default Qdrant backend.
            chunk_rows = [_chunk_row(idx, doc_id, ord_idx) for ord_idx in range(CHUNKS_PER_DOC)]
            await session.execute(insert(Chunk), chunk_rows)

    print(
        f"DR drill seed complete: workspace={WORKSPACE_ID} source={SOURCE_ID}"
        f" docs={len(DOC_IDS)} chunks={len(DOC_IDS) * CHUNKS_PER_DOC}"
        " (no pre-stored embeddings; rebuild will recompute from SoT text)"
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
