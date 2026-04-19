"""Hybrid retrieval service (vector + BM25 + RRF) for Omniscience.

Implements staged hybrid retrieval: pgvector HNSW top-K, tsvector BM25,
reciprocal rank fusion, ACL filter, and freshness filter.
See docs/decisions/0004-retrieval-strategy-staged.md for the full design.

Federation adds an optional fan-out layer: ``FederatedSearch`` wraps the
local ``RetrievalService`` and queries one or more remote Omniscience
instances in parallel, merging and deduplicating the combined result set.
"""

from .federation import FederatedSearch
from .federation_config import FederatedInstance, FederationConfig
from .models import (
    ChunkLineage,
    Citation,
    QueryStats,
    SearchHit,
    SearchRequest,
    SearchResult,
    SourceInfo,
)
from .reranker import NoopReranker, OllamaReranker, Reranker
from .search import RetrievalService

__all__ = [
    "ChunkLineage",
    "Citation",
    "FederatedInstance",
    "FederatedSearch",
    "FederationConfig",
    "NoopReranker",
    "OllamaReranker",
    "QueryStats",
    "Reranker",
    "RetrievalService",
    "SearchHit",
    "SearchRequest",
    "SearchResult",
    "SourceInfo",
]
