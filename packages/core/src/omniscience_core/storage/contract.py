from __future__ import annotations

from typing import Protocol, runtime_checkable

from omniscience_core.storage.graph import GraphStore
from omniscience_core.storage.vector import VectorStore


@runtime_checkable
class StoreContract(GraphStore, VectorStore, Protocol):
    """Unified protocol combining GraphStore and VectorStore interfaces.

    Implemented by ``PostgresOnlyStore`` (Lite-mode) and ``FullStore``
    (composite of Qdrant + Neo4j for Full-mode).
    """
