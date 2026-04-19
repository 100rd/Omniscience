"""Pydantic models mirroring the Omniscience REST API response shapes."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


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
    """Minimal source descriptor embedded in each search hit."""

    id: uuid.UUID
    name: str
    type: str


class SearchHit(BaseModel):
    """A single result returned by the retrieval service."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    score: float
    text: str
    source: SourceInfo
    citation: Citation
    lineage: ChunkLineage
    metadata: dict[str, Any] = Field(default_factory=dict)


class QueryStats(BaseModel):
    """Diagnostic counters for a completed search query."""

    total_matches_before_filters: int
    vector_matches: int
    text_matches: int
    duration_ms: float


class SearchResult(BaseModel):
    """Top-level response returned by the search endpoint."""

    hits: list[SearchHit]
    query_stats: QueryStats


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class Source(BaseModel):
    """A configured ingestion source."""

    id: uuid.UUID
    type: str
    name: str
    config: dict[str, Any] = Field(default_factory=dict)
    secrets_ref: str | None = None
    status: str
    last_sync_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    freshness_sla_seconds: int | None = None
    tenant_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


class Document(BaseModel):
    """Full document representation."""

    id: uuid.UUID
    source_id: uuid.UUID
    external_id: str
    uri: str
    title: str | None = None
    content_hash: str
    doc_version: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    indexed_at: datetime
    tombstoned_at: datetime | None = None


class Chunk(BaseModel):
    """A single text chunk associated with a document."""

    id: uuid.UUID
    document_id: uuid.UUID
    ord: int
    text: str
    symbol: str | None = None
    ingestion_run_id: uuid.UUID | None = None
    embedding_model: str
    embedding_provider: str
    parser_version: str
    chunker_strategy: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentWithChunks(BaseModel):
    """Document representation including all associated chunks."""

    document: Document
    chunks: list[Chunk]


# ---------------------------------------------------------------------------
# Ingestion runs
# ---------------------------------------------------------------------------


class IngestionRun(BaseModel):
    """An ingestion pipeline execution record."""

    id: uuid.UUID
    source_id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None = None
    status: str
    docs_new: int = 0
    docs_updated: int = 0
    docs_removed: int = 0
    errors: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


class ApiToken(BaseModel):
    """API token metadata (secret never returned after creation)."""

    id: uuid.UUID
    name: str
    token_prefix: str
    scopes: list[str]
    workspace_id: uuid.UUID | None = None
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    is_active: bool


class TokenCreateResponse(BaseModel):
    """Response after minting a new token — secret shown exactly once."""

    token: ApiToken
    secret: str


# ---------------------------------------------------------------------------
# Search request (for convenience when building requests)
# ---------------------------------------------------------------------------


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


__all__ = [
    "ApiToken",
    "Chunk",
    "ChunkLineage",
    "Citation",
    "Document",
    "DocumentWithChunks",
    "IngestionRun",
    "QueryStats",
    "SearchHit",
    "SearchRequest",
    "SearchResult",
    "Source",
    "SourceInfo",
    "TokenCreateResponse",
]
