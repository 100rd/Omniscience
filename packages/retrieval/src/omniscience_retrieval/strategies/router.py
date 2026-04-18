"""Strategy router: dispatches SearchRequests to the appropriate retrieval strategy.

The router owns all strategy instances and provides a unified ``execute`` entry
point.  It also handles the ``"auto"`` strategy by invoking the classifier.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from omniscience_retrieval.models import SearchRequest, SearchResult
from omniscience_retrieval.strategies.classifier import classify_query

if TYPE_CHECKING:
    from omniscience_embeddings.base import EmbeddingProvider
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


class StrategyRouter:
    """Routes search requests to the appropriate retrieval strategy.

    Strategies are lazily constructed on first use via *session_factory* and
    *embedding_provider* supplied at construction time.  The hybrid strategy is
    implemented by *hybrid_fn* (typically ``RetrievalService._hybrid_search``
    or equivalent) to avoid circular dependencies.

    Supported strategies:
      - ``"hybrid"`` — vector (HNSW) + BM25, merged via RRF (default)
      - ``"keyword"`` — BM25-only, no embedding
      - ``"structural"`` — graph-first: entity lookup + edge traversal
      - ``"auto"`` — heuristic classifier picks the best strategy
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_provider: EmbeddingProvider,
        hybrid_fn: HybridFn,
    ) -> None:
        from omniscience_retrieval.strategies.keyword import KeywordStrategy
        from omniscience_retrieval.strategies.structural import StructuralStrategy

        self._hybrid_fn = hybrid_fn
        self._keyword = KeywordStrategy(session_factory=session_factory)
        self._structural = StructuralStrategy(
            session_factory=session_factory,
            fallback_fn=hybrid_fn,
        )

    def select_strategy(self, request: SearchRequest) -> str:
        """For ``"auto"`` strategy, classify the query and return the chosen name.

        For all other strategies, return ``request.retrieval_strategy`` as-is.

        Args:
            request: The incoming search request.

        Returns:
            The resolved strategy name (one of ``"hybrid"``, ``"keyword"``,
            ``"structural"``).
        """
        if request.retrieval_strategy == "auto":
            chosen = classify_query(request.query)
            logger.debug("auto strategy classified %r as %r", request.query, chosen)
            return chosen
        return request.retrieval_strategy

    async def execute(self, request: SearchRequest) -> SearchResult:
        """Execute the selected strategy for *request*.

        Args:
            request: The search request.  ``retrieval_strategy`` may be
                ``"auto"``; the router resolves it before dispatch.

        Returns:
            A ``SearchResult`` from the chosen strategy.
        """
        strategy_name = self.select_strategy(request)

        if strategy_name == "keyword":
            return await self._keyword.execute(request)

        if strategy_name == "structural":
            return await self._structural.execute(request)

        # "hybrid" (default) or any future unrecognised value
        if strategy_name != "hybrid":
            logger.warning("unknown retrieval_strategy=%r; falling back to hybrid", strategy_name)
        return await self._hybrid_fn(request)


# ---------------------------------------------------------------------------
# Type alias
# ---------------------------------------------------------------------------

from collections.abc import Awaitable, Callable  # noqa: E402

HybridFn = Callable[[SearchRequest], Awaitable[SearchResult]]

__all__ = ["HybridFn", "StrategyRouter"]
