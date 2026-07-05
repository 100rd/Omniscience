"""Index writer: upsert documents + chunks, tombstone, and purge.

**Consistency model (partial-failure semantics)**

The write path covers three heterogeneous stores.  Only the Postgres
transaction is atomic.  Qdrant and Neo4j writes run outside it:

1. ``Postgres`` — document + chunk rows inside ``session.begin()``.
   ACID-guaranteed: either fully committed or rolled back.

2. ``Qdrant`` — ``upsert_chunks`` is called *outside* the Postgres
   transaction. A Qdrant failure no longer rolls back Postgres.
   Instead, we rely on min-checkpoint replay: Postgres commits, and on
   retry, Qdrant skips already-processed versions.

3. ``Neo4j`` — ``upsert_graph`` is called by ``IngestionWorker`` *after*
   the ``upsert_document`` call returns.  It is entirely outside the
   Postgres transaction.  A failure here produces the ``neo4j_orphan``
   drift class.

**Compensation**

Drift is detected and repaired by the ``ReconcileWorker``
(``omniscience_server.reconcile_worker``), which periodically
cross-checks all three stores and:

* marks ``ingestion_run`` rows as ``partial`` for ``pg_missing_qdrant``,
* hard-deletes orphan Qdrant chunks (``qdrant_orphan``),
* records a Prometheus metric for ``neo4j_orphan`` (deferred to the
  retention worker for Neo4j cleanup).

See also ``docs/decisions/0010-reconcile-worker.md`` (if present).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from omniscience_core.db.models import Chunk, Document
from omniscience_core.storage.graph import GraphStore
from omniscience_core.storage.vector import ChunkPayload, VectorStore
from sqlalchemy import delete, insert, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass
class ChunkData:
    """All fields needed to persist a single chunk across stores.

    ``id`` is the single chunk identity shared by the Postgres ``chunks`` row
    and the Qdrant point.  Keeping one id across both stores lets the GraphRAG
    read path validate a vector hit against its Postgres chunk row.
    """

    ord: int
    text: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    embedding: list[float] = field(default_factory=list)
    symbol: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding_model: str = ""
    embedding_provider: str = ""
    parser_version: str = ""
    chunker_strategy: str = ""


@dataclass
class UpsertResult:
    """Outcome of an orchestrated upsert call."""

    action: Literal["created", "updated", "unchanged"]
    document_id: uuid.UUID
    chunks_written: int
    doc_version: int


class IndexWriter:
    """Write documents and their chunks into the hybrid store.

    See module docstring for the partial-failure semantics and the
    ``ReconcileWorker`` compensation mechanism.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        graph_store: GraphStore | None = None,
        vector_store: VectorStore | None = None,
        write_chunk_embedding: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._graph_store = graph_store
        self._vector_store = vector_store
        # Whether to persist the ``chunks.embedding`` pgvector column.
        #
        # As of migration 0004 (issue #105 cutover) the column is DROPPED for
        # the default qdrant vector backend — vectors live in Qdrant only, so
        # writing ``embedding`` here raises ``UndefinedColumnError`` and rolls
        # back the whole chunk write (0 chunks persisted).  It is re-added at
        # runtime *only* by ``PostgresOnlyStore.connect`` for the postgres
        # vector backend, which owns its own chunk-insert path.  Default False
        # keeps the ORM INSERT from ever naming the dropped column.
        self._write_chunk_embedding = write_chunk_embedding

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def upsert_document(
        self,
        source_id: uuid.UUID,
        external_id: str,
        uri: str,
        title: str | None,
        content_hash: str,
        metadata: dict[str, Any],
        chunks: list[ChunkData],
        workspace_id: uuid.UUID | None = None,
        ingestion_run_id: uuid.UUID | None = None,
        epoch: int | None = None,
        forced_replay: bool = False,
    ) -> UpsertResult:
        """Persist a document and its chunks across Postgres and Qdrant.

        **Failure semantics (min-checkpoint replay):**
        Postgres and Qdrant writes are *not* wrapped in a distributed
        transaction. Qdrant ``upsert_chunks`` is called *outside* the
        Postgres transaction. A Qdrant failure leaves Postgres committed,
        preventing duplicate writes. On replay, this method does not short-circuit;
        instead it delegates to Qdrant, which uses its own per-store
        checkpoint to skip or write.

        ``upsert_graph`` (Neo4j) is called by the caller after this
        method returns and is always outside the Postgres transaction.
        """
        async with self._session_factory() as session, session.begin():
            existing = await self._find_document(session, source_id, external_id)

            action: Literal["created", "updated", "unchanged"]
            if existing is not None and existing.content_hash == content_hash:
                doc = existing
                action = "unchanged"
            else:
                # 1. Postgres Metadata Write
                if existing is None:
                    doc = await self._insert_document(
                        session, source_id, external_id, uri, title, content_hash, metadata
                    )
                    action = "created"
                else:
                    await self._delete_chunks(session, existing.id)
                    await self._update_document(
                        session, existing, uri, title, content_hash, metadata
                    )
                    doc = existing
                    action = "updated"

                await self._insert_chunks(session, doc.id, chunks, ingestion_run_id)

        # 2. Qdrant Vector Write (if adapter provided)
        #    Executed outside session.begin() to support min-checkpoint replay.
        #    If Qdrant fails, Postgres is already committed. On retry, Qdrant
        #    uses its own store checkpoint to avoid duplicating work.
        if self._vector_store and workspace_id:
            vector_metadata = dict(metadata)
            vector_metadata["workspace_id"] = str(workspace_id)

            payloads: list[ChunkPayload] = [
                {
                    "chunk_id": str(c.id),
                    "ord": c.ord,
                    "text": c.text,
                    "embedding": c.embedding,
                    "symbol": c.symbol,
                    "metadata": c.metadata,
                    "embedding_model": c.embedding_model,
                    "embedding_provider": c.embedding_provider,
                    "parser_version": c.parser_version,
                    "chunker_strategy": c.chunker_strategy,
                }
                for c in chunks
            ]

            await self._vector_store.upsert_chunks(
                source_id=source_id,
                external_id=external_id,
                uri=uri,
                title=title,
                content_hash=content_hash,
                metadata=vector_metadata,
                chunks=payloads,
                ingestion_run_id=ingestion_run_id,
                version=doc.doc_version,
                epoch=epoch,
                forced_replay=forced_replay,
            )

        return UpsertResult(
            action=action,
            document_id=doc.id,
            chunks_written=len(chunks) if action != "unchanged" else 0,
            doc_version=doc.doc_version,
        )

    async def tombstone(
        self,
        source_id: uuid.UUID,
        external_id: str,
        workspace_id: uuid.UUID | None = None,
    ) -> bool:
        """Tombstone document in Postgres and Qdrant."""
        async with self._session_factory() as session, session.begin():
            doc = await self._find_document(session, source_id, external_id)
            if doc is None:
                return False
            doc.tombstoned_at = datetime.now(UTC)

            if self._vector_store and workspace_id:
                await self._vector_store.delete_by_document(
                    source_id=source_id,
                    external_id=external_id,
                    workspace_id=workspace_id,
                )
            return True

    async def purge_tombstones(self, older_than: timedelta) -> int:
        """Hard-delete tombstoned documents older than *older_than*.

        Deletion order: Qdrant points first, then Postgres rows.

        Rationale: if Qdrant fails, Postgres rows survive and the document
        remains visible to the next purge run — no data is lost.  The reverse
        order (PG first) would leave orphan Qdrant vectors with no Postgres
        anchor, causing ghost points to accumulate silently.  A surviving
        Qdrant-side orphan (our failure mode) is a temporary space waste;
        a dangling Postgres reference with no vector is an integrity violation
        that breaks the hybrid query path.

        When ``_vector_store`` is None the Qdrant step is skipped and only
        the Postgres DELETE is executed (backward-compatible behaviour).

        Returns the number of Postgres document rows removed (chunks
        cascade-delete automatically via FK ``ON DELETE CASCADE``).
        """
        if self._vector_store is not None:
            await self._vector_store.delete_tombstoned(older_than)

        cutoff = datetime.now(UTC) - older_than
        async with self._session_factory() as session, session.begin():
            stmt = (
                delete(Document)
                .where(Document.tombstoned_at != None)  # noqa: E711
                .where(Document.tombstoned_at <= cutoff)
            )
            cursor = cast("CursorResult[Any]", await session.execute(stmt))
            return cursor.rowcount

    async def upsert_graph(
        self,
        source_id: uuid.UUID,
        document_id: uuid.UUID,
        entities: list[Any],
        edges: list[Any],
        workspace_id: uuid.UUID | None = None,
        snapshot_at: datetime | None = None,
        version: int | None = None,
        epoch: int | None = None,
        forced_replay: bool = False,
    ) -> None:
        """Persist symbol graph into Neo4j (orchestrated).

        **Always outside the Postgres transaction.**  A failure here
        produces ``neo4j_orphan`` drift.  The ``ReconcileWorker``
        records the metric; cleanup is deferred to the retention worker.
        """
        if self._graph_store and workspace_id:
            # Tag entities with workspace_id for Neo4j ACL
            for ent in entities:
                if hasattr(ent, "metadata"):
                    ent.metadata["workspace_id"] = str(workspace_id)
                elif isinstance(ent, dict):
                    ent.setdefault("metadata", {})["workspace_id"] = str(workspace_id)

            await self._graph_store.upsert_graph(
                source_id=source_id,
                document_id=document_id,
                entities=entities,
                edges=edges,
                workspace_id=workspace_id,
                snapshot_at=snapshot_at,
                version=version,
                epoch=epoch,
                forced_replay=forced_replay,
            )

    # ------------------------------------------------------------------
    # Private helpers — each kept < 30 lines
    # ------------------------------------------------------------------

    async def _find_document(
        self, session: AsyncSession, source_id: uuid.UUID, external_id: str
    ) -> Document | None:
        stmt = select(Document).where(
            Document.source_id == source_id,
            Document.external_id == external_id,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _insert_document(
        self,
        session: AsyncSession,
        source_id: uuid.UUID,
        external_id: str,
        uri: str,
        title: str | None,
        content_hash: str,
        metadata: dict[str, Any],
    ) -> Document:
        doc = Document(
            id=uuid.uuid4(),
            source_id=source_id,
            external_id=external_id,
            uri=uri,
            title=title,
            content_hash=content_hash,
            doc_version=1,
            doc_metadata=metadata,
            indexed_at=datetime.now(UTC),
        )
        session.add(doc)
        await session.flush()
        return doc

    async def _update_document(
        self,
        session: AsyncSession,
        doc: Document,
        uri: str,
        title: str | None,
        content_hash: str,
        metadata: dict[str, Any],
    ) -> None:
        doc.uri = uri
        doc.title = title
        doc.content_hash = content_hash
        doc.doc_metadata = metadata
        doc.doc_version = doc.doc_version + 1
        doc.indexed_at = datetime.now(UTC)
        doc.tombstoned_at = None
        await session.flush()

    async def _delete_chunks(self, session: AsyncSession, document_id: uuid.UUID) -> None:
        await session.execute(delete(Chunk).where(Chunk.document_id == document_id))

    async def _insert_chunks(
        self,
        session: AsyncSession,
        document_id: uuid.UUID,
        chunks: list[ChunkData],
        ingestion_run_id: uuid.UUID | None,
    ) -> None:
        rows: list[dict[str, Any]] = []
        for chunk_data in chunks:
            row: dict[str, Any] = {
                "id": chunk_data.id,
                "document_id": document_id,
                "ord": chunk_data.ord,
                "text": chunk_data.text,
                "symbol": chunk_data.symbol,
                "ingestion_run_id": ingestion_run_id,
                "embedding_model": chunk_data.embedding_model,
                "embedding_provider": chunk_data.embedding_provider,
                "parser_version": chunk_data.parser_version,
                "chunker_strategy": chunk_data.chunker_strategy,
                "chunk_metadata": chunk_data.metadata,
            }
            # Only name the pgvector ``embedding`` column when the backend still
            # has it (postgres vector backend, which re-adds it at runtime).
            # A Core INSERT emits ONLY the keys present here, so under the
            # default qdrant backend — whose column migration 0004 DROPPED —
            # the column is never named and the write cannot raise
            # ``UndefinedColumnError``.  (A plain ORM ``session.add`` would still
            # emit ``embedding = NULL`` for the unset mapped attribute.)
            if self._write_chunk_embedding:
                row["embedding"] = chunk_data.embedding
            rows.append(row)
        if rows:
            await session.execute(insert(Chunk), rows)


__all__ = ["ChunkData", "IndexWriter", "UpsertResult"]
