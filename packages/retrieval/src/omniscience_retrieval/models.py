"""Request / response models for the retrieval service."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    """Parameters for a hybrid search query."""

    query: str
    top_k: int = Field(default=10, ge=1, le=500)
    sources: list[str] | None = None
    types: list[str] | None = None
    max_age_seconds: int | None = Field(default=None, ge=1)
    filters: dict[str, Any] | None = None
    include_tombstoned: bool = False
    retrieval_strategy: Literal["hybrid", "keyword", "structural", "auto"] = "hybrid"


class Citation(BaseModel):
    """Provenance information for a retrieved chunk."""

    uri: str
    title: str | None
    indexed_at: datetime
    doc_version: int


class ChunkLineage(BaseModel):
    """Ingestion lineage metadata for a retrieved chunk."""

    ingestion_run_id: uuid.UUID | None
    embedding_model: str
    embedding_provider: str
    parser_version: str
    chunker_strategy: str


class SourceInfo(BaseModel):
    """Minimal source descriptor embedded in each hit."""

    id: uuid.UUID
    name: str
    type: str


class SearchHit(BaseModel):
    """A single result returned by the retrieval service.

    The ``source_instance`` field is ``None`` for results from the local
    instance and set to the :attr:`~FederatedInstance.name` of the remote
    peer for hits that arrived via federation.
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    score: float
    text: str
    source: SourceInfo
    citation: Citation
    lineage: ChunkLineage
    metadata: dict[str, Any]
    source_instance: str | None = Field(
        default=None,
        description=(
            "Name of the Omniscience instance that produced this hit.  "
            "None for the local instance; set to the peer name for federated results."
        ),
    )


class QueryStats(BaseModel):
    """Diagnostic counters for a completed search query."""

    total_matches_before_filters: int
    vector_matches: int
    text_matches: int
    duration_ms: float


class SearchResult(BaseModel):
    """Top-level response returned by RetrievalService.search()."""

    hits: list[SearchHit]
    query_stats: QueryStats
