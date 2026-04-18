"""Omniscience index layer: atomic document + chunk upsert into PostgreSQL/pgvector."""

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
