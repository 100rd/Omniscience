"""Storage interface layer for Omniscience.

Defines the ``GraphStore`` and ``VectorStore`` protocols that decouple
retrieval and writer code from any specific backend.  As of v0.2 the
production backends are Neo4j (graph) and Qdrant (vector) per
ADR-0005 and ADR-0006.

See ``docs/decisions/0005-neo4j-as-graph-store.md`` and
``docs/decisions/0006-qdrant-as-vector-store.md`` for the design
rationale.

ACL invariant (issue #117 carry-forward)
----------------------------------------

Every read method on both protocols takes ``workspace_id`` as a
**keyword-only, required** parameter.  There is no "skip filtering"
sentinel and no default value.  Callers that cannot name a workspace
must fail upstream (REST/MCP layer).  This is enforced at the protocol
level precisely so a Neo4j or Qdrant adapter cannot accidentally widen
a read in the way the pre-#117 graph-query path did.
"""

from __future__ import annotations

from omniscience_core.storage.graph import (
    EdgeUpsert,
    EntityNodeView,
    EntityUpsert,
    GraphEdgeView,
    GraphResultView,
    GraphStore,
    GraphWriteResult,
)
from omniscience_core.storage.vector import (
    ChunkPayload,
    UpsertOutcome,
    VectorSearchResult,
    VectorStore,
)

__all__ = [
    "ChunkPayload",
    "EdgeUpsert",
    "EntityNodeView",
    "EntityUpsert",
    "GraphEdgeView",
    "GraphResultView",
    "GraphStore",
    "GraphWriteResult",
    "UpsertOutcome",
    "VectorSearchResult",
    "VectorStore",
]
