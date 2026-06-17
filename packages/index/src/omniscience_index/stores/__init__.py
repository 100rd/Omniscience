"""Backend-specific store adapters for Omniscience.

Hosts the Neo4j ``GraphStore`` implementation (issue #104) and the
Qdrant ``VectorStore`` implementation (issue #106).  Pgvector adapters
live in ``omniscience_retrieval.adapters`` — the dependency arrow is
always ``adapter -> protocol`` defined in ``omniscience_core.storage``.

Concrete adapters are not re-exported from here to keep optional
third-party dependencies off the import path of callers that only
need the protocols.  Import the concrete adapter explicitly::

    from omniscience_index.stores.neo4j_store import Neo4jGraphStore
    from omniscience_index.stores.qdrant_store import QdrantVectorStore
"""

from __future__ import annotations

from omniscience_index.stores.neo4j_store import (
    Neo4jGraphStore,
    Neo4jStoreConfig,
)
from omniscience_index.stores.postgres_only_store import PostgresOnlyStore

__all__ = ["Neo4jGraphStore", "Neo4jStoreConfig", "PostgresOnlyStore"]
