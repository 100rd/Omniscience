"""Hybrid retrieval service: vector + BM25 + RRF merge."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, cast

from omniscience_core.db.models import Chunk, Document, Source
from omniscience_embeddings.base import EmbeddingProvider
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .filters import build_where_clauses, combine_clauses
from .models import (
    ChunkLineage,
    Citation,
    QueryStats,
    SearchHit,
    SearchRequest,
    SearchResult,
    SourceInfo,
)
from .ranking import reciprocal_rank_fusion
from .reranker import NoopReranker, Reranker
from .strategies.router import StrategyRouter

logger = logging.getLogger(__name__)

# Strategies that have dedicated implementations (v0.2).
# A WARNING is still emitted during the transition period so existing callers
# can update their log-monitoring expectations.
_NON_HYBRID_STRATEGIES = frozenset({"keyword", "structural", "auto"})

# Number of candidates forwarded to the re-ranker before slicing to top-k.
_RERANK_CANDIDATE_LIMIT = 50


class RetrievalService:
    """Executes search queries against the Omniscience index.

    Delegates to :class:`~omniscience_retrieval.strategies.router.StrategyRouter`
    which dispatches each request to the appropriate retrieval strategy:

    - ``"hybrid"``     — vector (pgvector HNSW) + BM25 (tsvector), merged via RRF
    - ``"keyword"``    — BM25-only, no embedding step
    - ``"structural"`` — graph-first: entity lookup + edge traversal
    - ``"auto"``       — heuristic classifier selects the best strategy

    The hybrid path is implemented directly on this class and passed as a
    callable to ``StrategyRouter`` to avoid circular imports.

    Optional re-ranking:
        When *reranker* is supplied (and is not a :class:`~.reranker.NoopReranker`),
        the service widens the initial candidate set to
        ``_RERANK_CANDIDATE_LIMIT`` results, scores each candidate against the
        query, and then returns the top-k by re-ranked score.  This improves
        precision at the cost of one extra embedding round-trip.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_provider: EmbeddingProvider,
        reranker: Reranker | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._embedding_provider = embedding_provider
        self._reranker: Reranker = reranker if reranker is not None else NoopReranker()
        self._router = StrategyRouter(
            session_factory=session_factory,
            embedding_provider=embedding_provider,
            hybrid_fn=self._hybrid_search,
        )

    @property
    def _reranking_active(self) -> bool:
        """True when an active (non-noop) reranker is configured."""
        return not isinstance(self._reranker, NoopReranker)

    async def search(self, request: SearchRequest) -> SearchResult:
        """Execute the appropriate retrieval strategy and return top-k results."""
        if request.retrieval_strategy in _NON_HYBRID_STRATEGIES:
            logger.warning(
                "retrieval_strategy=%r dispatching to dedicated implementation",
                request.retrieval_strategy,
            )

        result = await self._router.execute(request)

        # Re-rank if a non-noop reranker is configured.
        if self._reranking_active:
            result = await self._apply_reranker(request, result)

        return result

    # ------------------------------------------------------------------
    # Re-ranking
    # ------------------------------------------------------------------

    async def _apply_reranker(self, request: SearchRequest, result: SearchResult) -> SearchResult:
        """Re-score *result.hits* against *request.query* and return top-k."""
        candidates = result.hits[:_RERANK_CANDIDATE_LIMIT]
        if not candidates:
            return result

        texts = [hit.text for hit in candidates]
        scores = await self._reranker.rerank(request.query, texts)

        # Pair each hit with its new score and sort descending.
        rescored = sorted(
            zip(scores, candidates, strict=True),
            key=lambda pair: pair[0],
            reverse=True,
        )

        top_k_hits = [
            hit.model_copy(update={"score": score}) for score, hit in rescored[: request.top_k]
        ]

        logger.debug(
            "reranker applied: candidates=%d top_k=%d",
            len(candidates),
            len(top_k_hits),
        )
        return SearchResult(hits=top_k_hits, query_stats=result.query_stats)

    # ------------------------------------------------------------------
    # Hybrid search (vector + BM25 + RRF)
    # ------------------------------------------------------------------

    async def _hybrid_search(self, request: SearchRequest) -> SearchResult:
        """Run hybrid retrieval: pgvector HNSW + tsvector BM25, merged via RRF.

        When a reranker is active, the initial hit set is widened to
        ``_RERANK_CANDIDATE_LIMIT`` so the reranker has enough candidates to
        work with before the final top-k slice.
        """
        start = time.monotonic()
        query_vector = await self._embed_query(request.query)

        # When reranking is active, widen the candidate pool so the reranker
        # has enough hits to re-order meaningfully before the top-k slice.
        hit_limit = _RERANK_CANDIDATE_LIMIT if self._reranking_active else request.top_k

        async with self._session_factory() as session:
            oversample = hit_limit * 2

            vector_rows = await self._vector_search(session, query_vector, oversample)
            text_rows = await self._text_search(session, request.query, oversample)

            vector_matches = len(vector_rows)
            text_matches = len(text_rows)
            total_before = len({r[0] for r in vector_rows} | {r[0] for r in text_rows})

            merged = reciprocal_rank_fusion([vector_rows, text_rows])

            chunk_ids = [cid for cid, _ in merged]
            scores = {cid: score for cid, score in merged}

            rows = await self._fetch_enriched(session, request, chunk_ids)

        hits = self._build_hits(rows, scores, hit_limit)
        duration_ms = (time.monotonic() - start) * 1000.0

        return SearchResult(
            hits=hits,
            query_stats=QueryStats(
                total_matches_before_filters=total_before,
                vector_matches=vector_matches,
                text_matches=text_matches,
                duration_ms=duration_ms,
            ),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _embed_query(self, query: str) -> list[float]:
        vectors = await self._embedding_provider.embed([query])
        return vectors[0]

    async def _vector_search(
        self,
        session: AsyncSession,
        query_vector: list[float],
        limit: int,
    ) -> list[tuple[uuid.UUID, float]]:
        stmt = (
            select(Chunk.id, Chunk.embedding.cosine_distance(query_vector).label("dist"))
            .where(Chunk.embedding.is_not(None))
            .order_by(text("dist"))
            .limit(limit)
        )
        result = await session.execute(stmt)
        rows = result.all()
        # Convert distance to similarity: similarity = 1 - distance
        return [(row.id, 1.0 - float(row.dist)) for row in rows]

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
        """Fetch full chunk+document+source rows for the given chunk IDs."""
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
        return cast("list[Any]", result.all())

    def _build_hits(
        self,
        rows: list[Any],
        scores: dict[uuid.UUID, float],
        top_k: int,
    ) -> list[SearchHit]:
        """Convert enriched DB rows to SearchHit objects, sorted by RRF score."""
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
