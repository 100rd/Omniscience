"""pgvector-backed adapters that implement the core storage protocols.

These adapters are the *only* concrete implementations of
``omniscience_core.storage.GraphStore`` and
``omniscience_core.storage.VectorStore`` for the current deployment.
Phase 2 (issues #104 + #106) will introduce ``Neo4jGraphStore`` and
``QdrantVectorStore`` in separate packages.

The adapters delegate to the existing ``IndexWriter``,
``RetrievalService``, and ``GraphQueryService`` classes so this
scaffold introduces zero behavioural change; it only repoints the
dependency arrow at the protocol contracts.
"""

from __future__ import annotations

from omniscience_retrieval.adapters.pgvector_graph import PgVectorGraphStore
from omniscience_retrieval.adapters.pgvector_vector import PgVectorVectorStore

__all__ = ["PgVectorGraphStore", "PgVectorVectorStore"]
