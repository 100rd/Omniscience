"""BM25-only (keyword) retrieval strategy.

Uses PostgreSQL tsvector full-text ranking without the vector/HNSW component.
Best suited for exact-name lookups: function names, error strings, service
identifiers, config keys.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from omniscience_core.db.models import Chunk, Document, Source
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from omniscience_retrieval.filters import build_where_clauses, combine_clauses
from omniscience_retrieval.models import (
    ChunkLineage,
    Citation,
    QueryStats,
    SearchHit,
    SearchRequest,
    SearchResult,
    SourceInfo,
)

logger = logging.getLogger(__name__)


class KeywordStrategy:
    """BM25-only retrieval: tsvector ranking, no vector component.

    Returns chunks ranked purely by ``ts_rank_cd`` against the query's
    ``plainto_tsquery`` representation.  Ideal for callers who need exact-name
    lookup and don't want vector similarity diluting the results.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def execute(self, request: SearchRequest) -> SearchResult:
        """Run BM25-only search and return ranked results."""
        start = time.monotonic()

        async with self._session_factory() as session:
            oversample = request.top_k * 2
            text_rows = await self._text_search(session, request.query, oversample)

            text_matches = len(text_rows)
            chunk_ids = [cid for cid, _ in text_rows]
            scores = {cid: score for cid, score in text_rows}

            rows = await self._fetch_enriched(session, request, chunk_ids)

        hits = self._build_hits(rows, scores, request.top_k)
        duration_ms = (time.monotonic() - start) * 1000.0

        return SearchResult(
            hits=hits,
            query_stats=QueryStats(
                total_matches_before_filters=text_matches,
                vector_matches=0,
                text_matches=text_matches,
                duration_ms=duration_ms,
            ),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _text_search(
        self,
        session: AsyncSession,
        query: str,
        limit: int,
    ) -> list[tuple[uuid.UUID, float]]:
        tsquery = func.plainto_tsquery("english", query)
        rank_expr = func.ts_rank_cd(Chunk.text_tsv, tsquery).label("rank")
        stmt = (
            select(Chunk.id, rank_expr)
            .where(Chunk.text_tsv.op("@@")(tsquery))
            .order_by(rank_expr.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        rows = result.all()
        return [(row.id, float(row.rank)) for row in rows]

    async def _fetch_enriched(
        self,
        session: AsyncSession,
        request: SearchRequest,
        chunk_ids: list[uuid.UUID],
    ) -> list[Any]:
        if not chunk_ids:
            return []

        where_clauses = build_where_clauses(request)
        combined = combine_clauses(where_clauses)

        stmt = (
            select(Chunk, Document, Source)
            .join(Document, Chunk.document_id == Document.id)
            .join(Source, Document.source_id == Source.id)
            .where(Chunk.id.in_(chunk_ids))
        )
        if combined is not None:
            stmt = stmt.where(combined)

        result = await session.execute(stmt)
        return list(result.all())

    def _build_hits(
        self,
        rows: list[Any],
        scores: dict[uuid.UUID, float],
        top_k: int,
    ) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for chunk, doc, source in rows:
            hit = SearchHit(
                chunk_id=chunk.id,
                document_id=doc.id,
                score=scores.get(chunk.id, 0.0),
                text=chunk.text,
                source=SourceInfo(
                    id=source.id,
                    name=source.name,
                    type=str(source.type),
                ),
                citation=Citation(
                    uri=doc.uri,
                    title=doc.title,
                    indexed_at=doc.indexed_at,
                    doc_version=doc.doc_version,
                ),
                lineage=ChunkLineage(
                    ingestion_run_id=chunk.ingestion_run_id,
                    embedding_model=chunk.embedding_model,
                    embedding_provider=chunk.embedding_provider,
                    parser_version=chunk.parser_version,
                    chunker_strategy=chunk.chunker_strategy,
                ),
                metadata=chunk.chunk_metadata,
            )
            hits.append(hit)

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]


__all__ = ["KeywordStrategy"]
