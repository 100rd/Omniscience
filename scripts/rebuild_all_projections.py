#!/usr/bin/env python3
"""Auto-rebuild script to completely sync Neo4j and Qdrant from Postgres records.

Wipes Neo4j and Qdrant, then rebuilds them from active Postgres records.
Usage:
    python scripts/rebuild_all_projections.py --yes [--rto-seconds 900]
    python scripts/rebuild_all_projections.py --yes --verify-only
    python scripts/rebuild_all_projections.py --yes --recompute-embeddings

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

RTO (Recovery Time Objective)
==============================
Default budget: 900 seconds (15 minutes).  Override with --rto-seconds N.
The script exits with code 2 if elapsed time exceeds the budget.
See docs/decisions/0015-rebuild-direct-write-exception.md §RTO for guidance.

--recompute-embeddings (AP5)
=============================
When set, the rebuild IGNORES stored ``chunk.embedding`` values and
recomputes all embeddings from ``chunk.text`` via the live embedding
provider.  This exercises the true rebuild-from-SoT path (not
restore-from-backup) as required by AP5 / consilium-v9.

Default behaviour (flag absent): reuse stored embeddings only when the
legacy pre-v0.2 Postgres column still exists.  On the current schema,
migration 0004 removed that column, so embeddings are regenerated from
``chunk.text`` automatically.

For true DR validation (where you need to prove that the embedding
provider + SoT text can reproduce the stored vectors), always pass
``--recompute-embeddings``.  The ``dr-drill.yml`` nightly workflow uses
this flag.
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from typing import Any

from dr_verify import (
    DEFAULT_RTO_SECONDS,
    DRVerificationResult,
    check_rto,
    verify_projections,
)
from omniscience_core.config import Settings
from omniscience_core.db import create_async_engine, create_session_factory
from omniscience_core.db.models import Chunk, Document, Edge, Entity, Source, SourceStatus
from omniscience_core.storage.vector import ChunkPayload
from omniscience_embeddings import create_embedding_provider
from omniscience_index.stores.neo4j_store import Neo4jGraphStore, Neo4jStoreConfig
from omniscience_index.stores.qdrant_config import QdrantConfig
from omniscience_index.stores.qdrant_constants import COLLECTION_NAME_PREFIX
from omniscience_index.stores.qdrant_store import QdrantVectorStore
from qdrant_client import AsyncQdrantClient
from sqlalchemy import func, select, text

#: AP5 flag: exported so tests can assert the flag exists and is parseable.
RECOMPUTE_EMBEDDINGS_FLAG: str = "--recompute-embeddings"


def _chunk_projection(*, include_embedding: bool) -> list[Any]:
    """Return an explicit chunk projection compatible with migration 0004."""
    columns: list[Any] = [
        Chunk.id.label("id"),
        Chunk.document_id.label("document_id"),
        Chunk.ord.label("ord"),
        Chunk.text.label("text"),
        Chunk.symbol.label("symbol"),
        Chunk.embedding_model.label("embedding_model"),
        Chunk.embedding_provider.label("embedding_provider"),
        Chunk.parser_version.label("parser_version"),
        Chunk.chunker_strategy.label("chunker_strategy"),
        Chunk.chunk_metadata.label("metadata"),
    ]
    if include_embedding:
        columns.append(Chunk.embedding.label("embedding"))
    return columns


async def _has_legacy_embedding_column(db_session: Any) -> bool:
    """Return whether this database predates the Qdrant cutover migration."""
    result = await db_session.execute(
        text(
            "SELECT EXISTS ("
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'chunks' AND column_name = 'embedding'"
            ")"
        )
    )
    return bool(result.scalar_one())


async def _reset_qdrant_collections(
    qdrant_client: AsyncQdrantClient,
    vector_store: QdrantVectorStore,
) -> None:
    """Drop managed collections, then bootstrap the active collection again."""
    for collection in (await qdrant_client.get_collections()).collections:
        if collection.name.startswith(COLLECTION_NAME_PREFIX):
            await qdrant_client.delete_collection(collection.name)
            print(f"qdrant: dropped collection {collection.name}")

    # connect() bootstraps only after close() clears the store's lifecycle state.
    await vector_store.close()
    await vector_store.connect()


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


# ---------------------------------------------------------------------------
# Verification adapters — wrap the real stores to satisfy dr_verify protocols
# ---------------------------------------------------------------------------


class _PgAdapter:
    """Postgres SoT adapter for dr_verify._PgCountsSource."""

    def __init__(self, session_factory: Any) -> None:
        self._sf = session_factory

    async def get_document_counts_by_source(self, *, workspace_id: uuid.UUID) -> dict[str, int]:
        async with self._sf() as session:
            stmt = (
                select(Document.source_id, func.count(Document.id))
                .join(Source, Document.source_id == Source.id)
                .where(
                    Source.tenant_id == workspace_id,
                    Document.tombstoned_at.is_(None),
                )
                .group_by(Document.source_id)
            )
            result = await session.execute(stmt)
            return {str(row[0]): int(row[1]) for row in result.all()}

    async def get_chunk_counts_by_source(self, *, workspace_id: uuid.UUID) -> dict[str, int]:
        async with self._sf() as session:
            stmt = (
                select(Document.source_id, func.count(Chunk.id))
                .join(Source, Document.source_id == Source.id)
                .join(Chunk, Chunk.document_id == Document.id)
                .where(
                    Source.tenant_id == workspace_id,
                    Document.tombstoned_at.is_(None),
                )
                .group_by(Document.source_id)
            )
            result = await session.execute(stmt)
            return {str(row[0]): int(row[1]) for row in result.all()}

    async def get_max_doc_version_by_source(self, *, workspace_id: uuid.UUID) -> dict[str, int]:
        async with self._sf() as session:
            stmt = (
                select(Document.source_id, func.max(Document.doc_version))
                .join(Source, Document.source_id == Source.id)
                .where(
                    Source.tenant_id == workspace_id,
                    Document.tombstoned_at.is_(None),
                )
                .group_by(Document.source_id)
            )
            result = await session.execute(stmt)
            return {str(row[0]): int(row[1] or 0) for row in result.all()}


class _QdrantAdapter:
    """Qdrant adapter for dr_verify._QdrantCounts."""

    def __init__(
        self,
        vector_store: QdrantVectorStore,
        raw_client: AsyncQdrantClient,
    ) -> None:
        self._vs = vector_store
        self._qc = raw_client

    async def count_chunks(
        self,
        *,
        workspace_id: uuid.UUID,
        source_id: uuid.UUID | None = None,
        as_of: Any | None = None,
    ) -> int:
        return await self._vs.count_chunks(workspace_id=workspace_id, source_id=source_id)

    async def get_checkpoint_versions(self, *, workspace_id: uuid.UUID) -> dict[str, int]:
        from omniscience_index.stores.qdrant_filters import QdrantFilterBuilder

        flt = QdrantFilterBuilder(workspace_id=workspace_id).with_checkpoint().build()
        records, _ = await self._qc.scroll(
            collection_name=self._vs.collection_name,
            scroll_filter=flt,
            limit=10_000,
            with_payload=["source_id", "version"],
            with_vectors=False,
        )
        return {
            str(r.payload.get("source_id")): int(r.payload.get("version", 0))
            for r in records
            if r.payload
        }


class _Neo4jAdapter:
    """Neo4j adapter for dr_verify._Neo4jCounts."""

    def __init__(self, graph_store: Neo4jGraphStore) -> None:
        self._gs = graph_store

    async def count_entities_by_source(self, *, workspace_id: uuid.UUID) -> dict[str, int]:
        return await self._gs.count_entities_by_source(workspace_id=workspace_id)

    async def get_checkpoint_versions(self, *, workspace_id: uuid.UUID) -> dict[str, int]:
        query = (
            "MATCH (c:StoreCheckpoint {workspace_id: $workspace_id}) "
            "RETURN c.source_id AS source_id, c.version AS version"
        )
        async with self._gs._driver.session(database=self._gs._config.database) as session:
            records = await session.run(query, {"workspace_id": str(workspace_id)})
            return {str(record["source_id"]): int(record["version"]) async for record in records}


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args() -> tuple[bool, bool, int, bool]:
    """Return (confirm_yes, verify_only, rto_seconds, recompute_embeddings).

    Recognised flags (positional scanning, no argparse to keep deps light):
      --yes                  required for destructive rebuild
      --verify-only          skip wipe/rebuild; only run post-rebuild verification
      --rto-seconds N        RTO budget in seconds (default: DEFAULT_RTO_SECONDS)
      --recompute-embeddings ignore stored chunk.embedding; recompute from chunk.text
                             via the live embedding provider (AP5 rebuild-from-SoT path)
    """
    args = sys.argv[1:]
    confirm_yes = "--yes" in args
    verify_only = "--verify-only" in args
    recompute_embeddings = RECOMPUTE_EMBEDDINGS_FLAG in args

    rto_seconds = DEFAULT_RTO_SECONDS
    if "--rto-seconds" in args:
        idx = args.index("--rto-seconds")
        try:
            rto_seconds = int(args[idx + 1])
        except (IndexError, ValueError):
            print("--rto-seconds requires an integer argument")
            sys.exit(1)

    return confirm_yes, verify_only, rto_seconds, recompute_embeddings


# ---------------------------------------------------------------------------
# Verification helper (workspace loop)
# ---------------------------------------------------------------------------


async def _run_verification(
    *,
    session_factory: Any,
    vector_store: QdrantVectorStore,
    qdrant_client: AsyncQdrantClient,
    graph_store: Neo4jGraphStore,
    workspace_ids: list[uuid.UUID],
) -> bool:
    """Run verification for all workspaces; print summary; return overall pass."""
    pg_adapter = _PgAdapter(session_factory)
    qdrant_adapter = _QdrantAdapter(vector_store, qdrant_client)
    neo4j_adapter = _Neo4jAdapter(graph_store)

    all_passed = True
    for workspace_id in workspace_ids:
        result: DRVerificationResult = await verify_projections(
            workspace_id=workspace_id,
            pg=pg_adapter,
            qdrant=qdrant_adapter,
            neo4j=neo4j_adapter,
        )
        for line in result.summary_lines():
            print(line)
        if not result.passed:
            all_passed = False

    return all_passed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    confirm_yes, verify_only, rto_seconds, recompute_embeddings = _parse_args()

    if not confirm_yes and not verify_only:
        print("Refusing to rebuild without --yes (or use --verify-only for check-only mode)")
        sys.exit(1)

    if recompute_embeddings:
        print(
            "AP5 --recompute-embeddings: stored chunk.embedding values will be IGNORED. "
            "All embeddings will be recomputed from chunk.text via the live provider."
        )

    start_time = time.monotonic()

    settings = Settings()

    # Build Qdrant client + stores (needed for both rebuild and verify paths)
    qdrant_client = AsyncQdrantClient(
        host=settings.qdrant_host,
        grpc_port=settings.qdrant_grpc_port,
        port=settings.qdrant_http_port,
        api_key=settings.qdrant_api_key,
        https=False,
        prefer_grpc=True,
    )
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

    engine = create_async_engine(settings)
    session_factory = create_session_factory(engine)

    # Collect workspace IDs (tenant_id values from active sources)
    workspace_ids: list[uuid.UUID] = []
    async with session_factory() as db_session:
        has_legacy_embedding = await _has_legacy_embedding_column(db_session)

        src_stmt = select(Source).where(Source.status == SourceStatus.active)
        src_result = await db_session.execute(src_stmt)
        sources_all = src_result.scalars().all()
        workspace_ids = list({s.tenant_id for s in sources_all if s.tenant_id is not None})

    # ------------------------------------------------------------------
    # --verify-only: skip wipe/rebuild, go straight to verification
    # ------------------------------------------------------------------
    if verify_only:
        print("Running post-rebuild verification (--verify-only)...")
        all_passed = await _run_verification(
            session_factory=session_factory,
            vector_store=vector_store,
            qdrant_client=qdrant_client,
            graph_store=graph_store,
            workspace_ids=workspace_ids,
        )
        await vector_store.close()
        await graph_store.close()
        await qdrant_client.close()
        await engine.dispose()
        check_rto(start_time=start_time, budget_seconds=rto_seconds, exit_on_exceeded=True)
        if not all_passed:
            sys.exit(3)
        return

    # ------------------------------------------------------------------
    # Full rebuild path
    # ------------------------------------------------------------------
    print("Wiping Neo4j and Qdrant...")

    # 1. Wipe Neo4j
    async with graph_store._driver.session(database=graph_store._config.database) as session:
        result_neo4j = await session.run("MATCH (n) DETACH DELETE n")
        summary = await result_neo4j.consume()
        print(f"neo4j: deleted {summary.counters.nodes_deleted} nodes")

    # 2. Wipe Qdrant
    await _reset_qdrant_collections(qdrant_client, vector_store)

    print("Rebuilding projections from Postgres...")

    # 3. Fetch data and rebuild
    async with session_factory() as db_session:
        # Fetch active sources
        stmt = select(Source).where(Source.status == SourceStatus.active)
        source_result = await db_session.execute(stmt)
        sources = source_result.scalars().all()

        for source in sources:
            workspace_id = source.tenant_id
            if not workspace_id:
                print(
                    f"Skipping source {source.name} ({source.id}) - missing tenant_id/workspace_id"
                )
                continue

            print(f"Processing source: {source.name} (tenant/workspace: {workspace_id})")

            # Fetch active documents with deterministic ORDER BY (AP5).
            # Stable order: source_id first (constant within this loop iteration),
            # then document id for a total tie-break.  This guarantees the same
            # checkpoint epoch sequence across two independent rebuilds from the
            # same Postgres SoT, making the rebuild byte-reproducible.
            doc_stmt = (
                select(Document)
                .where(
                    Document.source_id == source.id,
                    Document.tombstoned_at.is_(None),
                )
                .order_by(Document.source_id, Document.id)
            )
            doc_result = await db_session.execute(doc_stmt)
            documents = doc_result.scalars().all()

            for doc in documents:
                print(f"  Rebuilding document: {doc.uri} (ext_id: {doc.external_id})")

                # Chunks are ordered by ord (already stable per AP5 audit).
                chunk_stmt = (
                    select(*_chunk_projection(include_embedding=has_legacy_embedding))
                    .where(Chunk.document_id == doc.id)
                    .order_by(Chunk.ord)
                )
                chunk_result = await db_session.execute(chunk_stmt)
                chunks = chunk_result.mappings().all()

                if not chunks:
                    continue

                # Prepare payloads typed as ChunkPayload for mypy.
                payloads: list[ChunkPayload] = []
                for chunk in chunks:
                    # AP5 --recompute-embeddings: always recompute from text.
                    # Default path (flag absent): reuse stored embedding when present.
                    stored_embedding = chunk.get("embedding")
                    if recompute_embeddings or not stored_embedding:
                        if recompute_embeddings:
                            print(
                                f"    Recomputing embedding for chunk {chunk.id} from SoT text..."
                            )
                        else:
                            print(f"    Generating embedding for chunk {chunk.id}...")
                        vectors = await embedding_provider.embed([chunk.text])
                        embedding = vectors[0]
                    else:
                        embedding = stored_embedding

                    payload: ChunkPayload = {
                        "ord": int(chunk.ord),
                        "text": str(chunk.text),
                        "embedding": list(embedding),
                        "symbol": chunk.symbol,
                        "metadata": dict(chunk.metadata or {}),
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

                # Fetch edges (typed list to satisfy mypy)
                edges: list[Edge] = []
                if entities:
                    ent_ids = [e.id for e in entities]
                    edge_stmt = select(Edge).where(Edge.source_entity_id.in_(ent_ids))
                    edge_result = await db_session.execute(edge_stmt)
                    edges = list(edge_result.scalars().all())

                # Wrap and upsert to graph store
                wrapped_entities = [EntityWrapper(ent, workspace_id) for ent in entities]
                wrapped_edges = [EdgeWrapper(edge, workspace_id) for edge in edges]

                # An empty graph projection is still a successfully processed
                # document version and must advance the Neo4j checkpoint.
                await graph_store.upsert_graph(
                    source_id=source.id,
                    document_id=doc.id,
                    entities=wrapped_entities,
                    edges=wrapped_edges,
                    workspace_id=workspace_id,
                    version=doc.doc_version,
                )

    print("Rebuild complete.  Running post-rebuild verification...")

    # 4. Post-rebuild verification (AP5)
    all_passed = await _run_verification(
        session_factory=session_factory,
        vector_store=vector_store,
        qdrant_client=qdrant_client,
        graph_store=graph_store,
        workspace_ids=workspace_ids,
    )

    # 5. Clean up connections
    await vector_store.close()
    await graph_store.close()
    await qdrant_client.close()
    await engine.dispose()

    # 6. Enforce RTO budget (AP5) — exits with code 2 if exceeded
    check_rto(start_time=start_time, budget_seconds=rto_seconds, exit_on_exceeded=True)

    if not all_passed:
        print("ERROR: Post-rebuild verification FAILED — see mismatches above.")
        sys.exit(3)

    print("All checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
