"""Omniscience index layer.

Atomic document + chunk-metadata upsert into PostgreSQL (operational
metadata only as of v0.2), plus the Neo4j and Qdrant store adapters
used by the retrieval composer.
"""

from omniscience_index.hashing import compute_content_hash
from omniscience_index.linker import EntityLinker
from omniscience_index.matchers import (
    exact_name_match,
    normalize_entity_name,
    resource_name_match,
)
from omniscience_index.writer import ChunkData, IndexWriter, UpsertResult

__all__ = [
    "ChunkData",
    "EntityLinker",
    "IndexWriter",
    "UpsertResult",
    "compute_content_hash",
    "exact_name_match",
    "normalize_entity_name",
    "resource_name_match",
]
