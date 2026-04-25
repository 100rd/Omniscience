"""GraphRAG retrieval composition for Omniscience.

As of v0.2 the canonical retrieval path is the GraphRAG composer
(Neo4j + Qdrant); see ``docs/decisions/0005-neo4j-as-graph-store.md``
and ``docs/decisions/0006-qdrant-as-vector-store.md``.

Federation adds an optional fan-out layer: ``FederatedSearch`` wraps
any ``search(request)``-shaped object and queries one or more remote
Omniscience instances in parallel, merging and deduplicating the
combined result set.

Historical note
---------------

Prior to v0.2 this package also exposed:

- ``PgVectorGraphStore`` / ``PgVectorVectorStore`` pgvector adapters.
- ``RetrievalService``: the hybrid pgvector+BM25 retrieval service.
- ``QueryRewriter``, ``OllamaReranker``, ``NoopReranker``, and the
  ``strategies`` sub-package.

All of the above were removed at the #105 cutover when Neo4j + Qdrant
became the only supported backends.  See ``CHANGELOG.md`` §0.2.0 for
upgrade notes.
"""

from .federation import FederatedSearch
from .federation_config import FederatedInstance, FederationConfig
from .graph_query import EdgeResult, EntityNode, GraphQueryService, GraphResult
from .graph_rag import (
    ANCHOR_FILTER_KEY,
    CANDIDATE_EXPANSION_FACTOR,
    GRAPH_AFFINITY_BASE,
    MAX_ANCHOR_CANDIDATES,
    MAX_ANCHOR_DEPTH,
    MERGE_ALPHA,
    GraphRAGComposer,
)
from .models import (
    ChunkLineage,
    Citation,
    QueryStats,
    SearchHit,
    SearchRequest,
    SearchResult,
    SourceInfo,
)

__all__ = [
    "ANCHOR_FILTER_KEY",
    "CANDIDATE_EXPANSION_FACTOR",
    "GRAPH_AFFINITY_BASE",
    "MAX_ANCHOR_CANDIDATES",
    "MAX_ANCHOR_DEPTH",
    "MERGE_ALPHA",
    "ChunkLineage",
    "Citation",
    "EdgeResult",
    "EntityNode",
    "FederatedInstance",
    "FederatedSearch",
    "FederationConfig",
    "GraphQueryService",
    "GraphRAGComposer",
    "GraphResult",
    "QueryStats",
    "SearchHit",
    "SearchRequest",
    "SearchResult",
    "SourceInfo",
]
