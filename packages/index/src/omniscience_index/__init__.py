"""Omniscience index layer: atomic document + chunk upsert into PostgreSQL/pgvector."""

from omniscience_index.hashing import compute_content_hash
from omniscience_index.writer import ChunkData, IndexWriter, UpsertResult

__all__ = [
    "ChunkData",
    "IndexWriter",
    "UpsertResult",
    "compute_content_hash",
]
