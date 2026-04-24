"""Pgvector-backed adapter for the ``VectorStore`` protocol.

Wraps the existing ``IndexWriter`` (writes) and ``RetrievalService``
(searches) so all current call sites can depend on the protocol
rather than on the concrete pgvector classes.  Zero behavioural
change: every method delegates to code already in production.

See ``packages/core/src/omniscience_core/storage/vector.py`` for the
protocol contract and the ACL invariant.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import structlog
from omniscience_core.storage.vector import (
    ChunkPayload,
    UpsertOutcome,
)
from omniscience_embeddings.base import EmbeddingProvider
from omniscience_index.writer import ChunkData, IndexWriter, UpsertResult
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from omniscience_retrieval.models import SearchRequest, SearchResult
from omniscience_retrieval.reranker import Reranker
from omniscience_retrieval.search import RetrievalService

log = structlog.get_logger(__name__)


class PgVectorVectorStore:
    """Pgvector-backed ``VectorStore`` — the concrete Phase-1 adapter.

    Composition
    -----------
    - ``IndexWriter``        for chunk upserts and tombstoning.
    - ``RetrievalService``   for search queries.

    Both classes existed before #103; this adapter only re-exposes
    them through the protocol-shaped surface.

    Known gap (tracked for Phase 2)
    -------------------------------
    ``search`` today does not apply a workspace filter to the result
    set — the ACL invariant is enforced at the protocol level but the
    pgvector search path (``RetrievalService``) pre-dates #117's
    workspace-filter machinery.  The Qdrant adapter (issue #106) will
    enforce it natively.  Until then, ``workspace_id`` is accepted
    and logged so call-sites can be audited.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_provider: EmbeddingProvider,
        reranker: Reranker | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._writer = IndexWriter(session_factory)
        self._retrieval = RetrievalService(
            session_factory=session_factory,
            embedding_provider=embedding_provider,
            reranker=reranker,
        )

    @property
    def retrieval_service(self) -> RetrievalService:
        """Expose the underlying ``RetrievalService`` for composition.

        ``FederatedSearch`` wraps a concrete ``RetrievalService``, so
        callers that need the federation layer still reach into the
        adapter to compose.  This is a transitional handle; Phase 2
        adapters will surface a protocol-clean composition API.
        """
        return self._retrieval

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    async def upsert_chunks(
        self,
        *,
        source_id: uuid.UUID,
        external_id: str,
        uri: str,
        title: str | None,
        content_hash: str,
        metadata: dict[str, Any],
        chunks: list[ChunkPayload],
        ingestion_run_id: uuid.UUID | None = None,
    ) -> UpsertOutcome:
        """Delegate to ``IndexWriter.upsert_document`` unchanged."""
        chunk_data = [_payload_to_chunk_data(c) for c in chunks]
        result: UpsertResult = await self._writer.upsert_document(
            source_id=source_id,
            external_id=external_id,
            uri=uri,
            title=title,
            content_hash=content_hash,
            metadata=metadata,
            chunks=chunk_data,
            ingestion_run_id=ingestion_run_id,
        )
        return UpsertOutcome(
            action=result.action,
            document_id=result.document_id,
            chunks_written=result.chunks_written,
            doc_version=result.doc_version,
        )

    async def delete_by_document(
        self,
        *,
        source_id: uuid.UUID,
        external_id: str,
    ) -> bool:
        """Delegate to ``IndexWriter.tombstone`` unchanged."""
        return await self._writer.tombstone(source_id, external_id)

    async def delete_tombstoned(self, older_than: timedelta) -> int:
        """Delegate to ``IndexWriter.purge_tombstones`` unchanged."""
        return await self._writer.purge_tombstones(older_than)

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    async def search(
        self,
        *,
        request: SearchRequest,
        workspace_id: uuid.UUID,
    ) -> SearchResult:
        """Execute a retrieval query.

        Parameters
        ----------
        request:
            ``SearchRequest`` — forwarded to ``RetrievalService.search``.
        workspace_id:
            REQUIRED by the protocol's ACL invariant.  The current
            pgvector implementation does not yet filter the result
            set by workspace (documented gap; see class docstring).
            The value is logged so audit trails can identify
            unscoped reads until the follow-up lands.
        """
        log.debug(
            "pgvector_vector_search",
            workspace_id=str(workspace_id),
            strategy=request.retrieval_strategy,
            top_k=request.top_k,
        )
        return await self._retrieval.search(request)

    async def count(self, *, source_id: uuid.UUID) -> int:
        """Return the count of active (non-tombstoned) documents for a source."""
        from omniscience_core.db.models import Document

        session: AsyncSession
        async with self._session_factory() as session:
            stmt = (
                select(func.count())
                .select_from(Document)
                .where(Document.source_id == source_id)
                .where(Document.tombstoned_at.is_(None))
            )
            result = await session.execute(stmt)
            value = result.scalar_one()
            return int(value)


# ---------------------------------------------------------------------------
# Payload translator
# ---------------------------------------------------------------------------


def _payload_to_chunk_data(payload: ChunkPayload) -> ChunkData:
    """Translate a ``ChunkPayload`` TypedDict to an ``IndexWriter.ChunkData``.

    Applies the "required keys" policy documented on ``ChunkPayload``:
    ``ord``, ``text``, ``embedding`` are mandatory; others default.
    """
    try:
        ord_ = payload["ord"]
        text = payload["text"]
        embedding = payload["embedding"]
    except KeyError as exc:  # pragma: no cover — defensive only
        raise ValueError(f"chunk payload missing required key: {exc}") from exc

    return ChunkData(
        ord=ord_,
        text=text,
        embedding=embedding,
        symbol=payload.get("symbol"),
        metadata=dict(payload.get("metadata", {})),
        embedding_model=payload.get("embedding_model", ""),
        embedding_provider=payload.get("embedding_provider", ""),
        parser_version=payload.get("parser_version", ""),
        chunker_strategy=payload.get("chunker_strategy", ""),
    )


__all__ = ["PgVectorVectorStore"]
