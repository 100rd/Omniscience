"""Hybrid retrieval service (vector + BM25 + RRF) for Omniscience.

Implements staged hybrid retrieval: pgvector HNSW top-K, tsvector BM25,
reciprocal rank fusion, ACL filter, and freshness filter.
See docs/decisions/0004-retrieval-strategy-staged.md for the full design.

Federation adds an optional fan-out layer: ``FederatedSearch`` wraps the
local ``RetrievalService`` and queries one or more remote Omniscience
instances in parallel, merging and deduplicating the combined result set.

Query rewriting (v0.4+) provides optional heuristic expansion of search
queries before retrieval to improve recall in air-gapped deployments.
"""

from .federation import FederatedSearch
from .federation_config import FederatedInstance, FederationConfig
from .graph_query import EdgeResult, EntityNode, GraphQueryService, GraphResult
from .models import (
    ChunkLineage,
    Citation,
    QueryStats,
    SearchHit,
    SearchRequest,
    SearchResult,
    SourceInfo,
)
from .query_rewriter import QueryRewriter
from .reranker import NoopReranker, OllamaReranker, Reranker
from .search import RetrievalService

__all__ = [
    "ChunkLineage",
    "Citation",
    "EdgeResult",
    "EntityNode",
    "FederatedInstance",
    "FederatedSearch",
    "FederationConfig",
    "GraphQueryService",
    "GraphResult",
    "NoopReranker",
    "OllamaReranker",
    "QueryRewriter",
    "QueryStats",
    "Reranker",
    "RetrievalService",
    "SearchHit",
    "SearchRequest",
    "SearchResult",
    "SourceInfo",
]
