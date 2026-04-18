"""Atomic index writer: upsert documents + chunks, tombstone, and purge.

All public methods run within a single database transaction so callers
never observe a half-written state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from omniscience_core.db.models import Chunk, Document, Edge, Entity
from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass
class ChunkData:
    """All fields needed to persist a single chunk."""

    ord: int
    text: str
    embedding: list[float]
    symbol: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding_model: str = ""
    embedding_provider: str = ""
    parser_version: str = ""
    chunker_strategy: str = ""


@dataclass
class UpsertResult:
    """Outcome of a single :meth:`IndexWriter.upsert_document` call."""

    action: Literal["created", "updated", "unchanged"]
    document_id: uuid.UUID
    chunks_written: int
    doc_version: int


class IndexWriter:
    """Write documents and their chunks atomically into the index store."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

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
        ingestion_run_id: uuid.UUID | None = None,
    ) -> UpsertResult:
        """Atomically create or update a document and its chunks.

        Decision table (evaluated inside one transaction):
        - No existing row           → insert document + chunks → action="created"
        - Existing, same hash       → no-op                    → action="unchanged"
        - Existing, different hash  → replace chunks, bump version → action="updated"
        """
        async with self._session_factory() as session, session.begin():
            existing = await self._find_document(session, source_id, external_id)

            if existing is None:
                doc = await self._insert_document(
                    session, source_id, external_id, uri, title, content_hash, metadata
                )
                await self._insert_chunks(session, doc.id, chunks, ingestion_run_id)
                return UpsertResult(
                    action="created",
                    document_id=doc.id,
                    chunks_written=len(chunks),
                    doc_version=doc.doc_version,
                )

            if existing.content_hash == content_hash:
                return UpsertResult(
                    action="unchanged",
                    document_id=existing.id,
                    chunks_written=0,
                    doc_version=existing.doc_version,
                )

            await self._delete_chunks(session, existing.id)
            await self._update_document(session, existing, uri, title, content_hash, metadata)
            await self._insert_chunks(session, existing.id, chunks, ingestion_run_id)
            return UpsertResult(
                action="updated",
                document_id=existing.id,
                chunks_written=len(chunks),
                doc_version=existing.doc_version,
            )

    async def tombstone(self, source_id: uuid.UUID, external_id: str) -> bool:
        """Set ``tombstoned_at`` on the document identified by *(source_id, external_id)*.

        Returns ``True`` when the document existed and was updated; ``False``
        when no matching active document was found.
        """
        async with self._session_factory() as session, session.begin():
            doc = await self._find_document(session, source_id, external_id)
            if doc is None:
                return False
            doc.tombstoned_at = datetime.now(UTC)
            return True

    async def purge_tombstones(self, older_than: timedelta) -> int:
        """Hard-delete tombstoned documents older than *older_than*.

        Returns the number of documents removed (their chunks cascade-delete
        automatically via the FK ``ON DELETE CASCADE`` constraint).
        """
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
    ) -> None:
        """Persist symbol graph entities and edges for a document.

        On re-ingestion of the same document the previous entities/edges for
        this source are deleted and replaced.  This keeps the graph consistent
        with the current document content.

        ``entities`` are :class:`~omniscience_parsers.code.graph.ExtractedEntity`
        instances; ``edges`` are
        :class:`~omniscience_parsers.code.graph.ExtractedEdge` instances.

        Cross-file edges (where the target entity does not yet exist) are
        stored with a NULL target — they are resolved lazily in a later
        graph-linking pass (not yet implemented; placeholder for v0.2).
        """
        async with self._session_factory() as session, session.begin():
            # Delete existing entities for this source (edges cascade via FK)
            await session.execute(
                delete(Entity).where(Entity.source_id == source_id)
            )

            if not entities:
                return

            # Insert entities and build a name → ORM id mapping
            name_to_orm_id: dict[str, uuid.UUID] = {}
            for ext_ent in entities:
                orm_id = ext_ent.id
                ent = Entity(
                    id=orm_id,
                    source_id=source_id,
                    entity_type=ext_ent.entity_type,
                    name=ext_ent.name,
                    display_name=ext_ent.display_name,
                    chunk_id=None,  # chunk linking deferred to v0.2
                    entity_metadata=ext_ent.metadata,
                    created_at=datetime.now(UTC),
                )
                session.add(ent)
                name_to_orm_id[ext_ent.name] = orm_id
                # Also index by display name for intra-file call resolution
                name_to_orm_id.setdefault(ext_ent.display_name, orm_id)

            await session.flush()

            # Insert edges — resolve target by name, drop unresolvable ones
            for ext_edge in edges:
                source_id_ent = ext_edge.source_entity_id
                target_name = ext_edge.target_name
                target_orm_id = name_to_orm_id.get(target_name)
                if target_orm_id is None:
                    # Cross-file target; skip for now (v0.2 will resolve these)
                    continue
                # Skip self-loops
                if source_id_ent == target_orm_id:
                    continue
                edge = Edge(
                    id=uuid.uuid4(),
                    source_entity_id=source_id_ent,
                    target_entity_id=target_orm_id,
                    edge_type=ext_edge.edge_type,
                    edge_metadata=ext_edge.metadata,
                    created_at=datetime.now(UTC),
                )
                session.add(edge)

            await session.flush()

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
        for chunk_data in chunks:
            chunk = Chunk(
                id=uuid.uuid4(),
                document_id=document_id,
                ord=chunk_data.ord,
                text=chunk_data.text,
                embedding=chunk_data.embedding or None,
                symbol=chunk_data.symbol,
                ingestion_run_id=ingestion_run_id,
                embedding_model=chunk_data.embedding_model,
                embedding_provider=chunk_data.embedding_provider,
                parser_version=chunk_data.parser_version,
                chunker_strategy=chunk_data.chunker_strategy,
                chunk_metadata=chunk_data.metadata,
            )
            session.add(chunk)
        await session.flush()


__all__ = ["ChunkData", "IndexWriter", "UpsertResult"]
