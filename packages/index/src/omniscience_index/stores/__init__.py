"""Backend-specific store adapters for Omniscience.

Currently hosts the Neo4j ``GraphStore`` implementation (issue #104 /
Phase 2a of epic #96).  Pgvector adapters live in
``omniscience_retrieval.adapters`` — the dependency arrow is always
``adapter -> protocol`` defined in
``omniscience_core.storage``.
"""

from __future__ import annotations

from omniscience_index.stores.neo4j_store import (
    Neo4jGraphStore,
    Neo4jStoreConfig,
)

__all__ = ["Neo4jGraphStore", "Neo4jStoreConfig"]
