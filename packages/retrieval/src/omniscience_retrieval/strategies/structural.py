"""Graph-first structural retrieval strategy.

Interprets the query as an entity lookup followed by graph traversal through
the symbol graph (entities + edges tables).  Returns chunks for the seed
entity and all directly connected entities (one hop).

Falls back to hybrid retrieval when:
  - no entity matches the query
  - the graph is empty for the matched entity
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from omniscience_core.db.models import Chunk, Document, Edge, Entity, Source
from sqlalchemy import func, or_, select
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

# Maximum number of seed entities matched from the query term.
_MAX_SEED_ENTITIES = 10

# Maximum graph depth for traversal (currently 1 hop).
_GRAPH_HOPS = 1


class StructuralStrategy:
    """Graph-first retrieval: entity lookup + one-hop edge traversal.

    Query interpretation:
      1. Extract search term(s) from the query (strip relationship keywords).
      2. Find seed entities whose ``name`` or ``display_name`` fuzzy-matches.
      3. Collect chunk_ids from seed entities + their immediate neighbours.
      4. Fetch and rank those chunks.

    If no graph matches are found, logs a warning and delegates to hybrid
    retrieval via the *fallback_fn* callable provided at construction time.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        fallback_fn: HybridFallbackFn,
    ) -> None:
        self._session_factory = session_factory
        self._fallback_fn = fallback_fn

    async def execute(self, request: SearchRequest) -> SearchResult:
        """Run structural retrieval and return results."""
        start = time.monotonic()
        term = _extract_subject(request.query)

        async with self._session_factory() as session:
            seed_entities = await self._find_seed_entities(session, term)

            if not seed_entities:
                logger.info("structural: no entities matched %r; falling back to hybrid", term)
                return await self._fallback_fn(request)

            entity_ids = [e.id for e in seed_entities]

            # Collect connected entity IDs (one hop, both directions)
            neighbour_ids = await self._collect_neighbours(session, entity_ids)
            all_entity_ids = list({*entity_ids, *neighbour_ids})

            # Gather chunk_ids from all entities in scope
            chunk_ids = await self._entity_chunk_ids(session, all_entity_ids)

            if not chunk_ids:
                logger.info(
                    "structural: entities found but no chunks attached; falling back to hybrid"
                )
                return await self._fallback_fn(request)

            # Rank chunks: seeds first, then neighbours, with position score
            scores = _positional_scores(entity_ids, seed_entities, chunk_ids)

            rows = await self._fetch_enriched(session, request, list(chunk_ids))

        hits = self._build_hits(rows, scores, request.top_k)
        duration_ms = (time.monotonic() - start) * 1000.0

        graph_matches = len(chunk_ids)
        return SearchResult(
            hits=hits,
            query_stats=QueryStats(
                total_matches_before_filters=graph_matches,
                vector_matches=0,
                text_matches=graph_matches,
                duration_ms=duration_ms,
            ),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _find_seed_entities(
        self,
        session: AsyncSession,
        term: str,
    ) -> list[Entity]:
        """Return entities whose name or display_name contains *term*."""
        pattern = f"%{term}%"
        stmt = (
            select(Entity)
            .where(
                or_(
                    func.lower(Entity.name).like(func.lower(pattern)),
                    func.lower(Entity.display_name).like(func.lower(pattern)),
                )
            )
            .limit(_MAX_SEED_ENTITIES)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def _collect_neighbours(
        self,
        session: AsyncSession,
        entity_ids: list[uuid.UUID],
    ) -> list[uuid.UUID]:
        """Return IDs of entities reachable via one hop from *entity_ids*."""
        if not entity_ids:
            return []

        stmt = select(Edge.source_entity_id, Edge.target_entity_id).where(
            or_(
                Edge.source_entity_id.in_(entity_ids),
                Edge.target_entity_id.in_(entity_ids),
            )
        )
        result = await session.execute(stmt)
        neighbour_ids: list[uuid.UUID] = []
        for row in result.all():
            src_id = row.source_entity_id
            tgt_id = row.target_entity_id
            if src_id not in entity_ids:
                neighbour_ids.append(src_id)
            if tgt_id not in entity_ids:
                neighbour_ids.append(tgt_id)
        return neighbour_ids

    async def _entity_chunk_ids(
        self,
        session: AsyncSession,
        entity_ids: list[uuid.UUID],
    ) -> set[uuid.UUID]:
        """Return chunk_ids for the given entities (excludes entities with null chunk)."""
        if not entity_ids:
            return set()

        stmt = select(Entity.chunk_id).where(
            Entity.id.in_(entity_ids),
            Entity.chunk_id.is_not(None),
        )
        result = await session.execute(stmt)
        return {row.chunk_id for row in result.all() if row.chunk_id is not None}

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
                score=scores.get(chunk.id, 0.5),
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


# ---------------------------------------------------------------------------
# Type alias for the hybrid fallback callable
# ---------------------------------------------------------------------------

from collections.abc import Awaitable, Callable  # noqa: E402

HybridFallbackFn = Callable[[SearchRequest], Awaitable[SearchResult]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_subject(query: str) -> str:
    """Strip leading relationship keywords and return the core subject term.

    Examples:
      "what depends on AuthService"  → "AuthService"
      "imports from utils"           → "utils"
      "calls parse_token"            → "parse_token"
      "authenticate_user"            → "authenticate_user"
    """
    # Strip leading relationship verb phrases
    _prefixes = (
        "what depends on ",
        "what imports ",
        "what calls ",
        "who calls ",
        "who imports ",
        "who uses ",
        "depends on ",
        "depend on ",
        "dependencies of ",
        "dependency of ",
        "imported by ",
        "called by ",
        "referenced by ",
        "references to ",
        "calls ",
        "imports ",
        "uses ",
        "inherits from ",
        "extends ",
    )
    lower = query.lower()
    for prefix in _prefixes:
        if lower.startswith(prefix):
            return query[len(prefix) :].strip()

    # If no prefix, return the whole query (last token as heuristic for
    # "depends on X" patterns where prefix was already stripped above)
    tokens = query.strip().split()
    return tokens[-1] if tokens else query


def _positional_scores(
    seed_ids: list[uuid.UUID],
    seed_entities: list[Entity],
    chunk_ids: set[uuid.UUID],
) -> dict[uuid.UUID, float]:
    """Assign scores: seed entity chunks get 1.0, neighbour chunks get 0.7."""
    seed_chunk_ids: set[uuid.UUID] = {e.chunk_id for e in seed_entities if e.chunk_id is not None}
    return {cid: (1.0 if cid in seed_chunk_ids else 0.7) for cid in chunk_ids}


__all__ = ["HybridFallbackFn", "StructuralStrategy"]
