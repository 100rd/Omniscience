"""Pydantic read/write schemas for Omniscience database models.

Each table has two schema variants:
- ``*Create`` — input for INSERT operations (no server-generated fields)
- ``*Read``   — output for SELECT operations (includes all fields)

Note: ORM models use ``doc_metadata`` / ``chunk_metadata`` / ``run_errors`` /
``entity_metadata`` / ``edge_metadata`` as Python attribute names to avoid
the SQLAlchemy reserved name ``metadata``.  The Pydantic schemas expose the
canonical field names (``metadata``, ``errors``) and use ``model_validate``
with ``from_attributes=True`` plus field aliases where names diverge.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omniscience_core.db.models import IngestionRunStatus, SourceStatus, SourceType

# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class SourceCreate(BaseModel):
    """Fields required / accepted when creating a new source."""

    type: SourceType
    name: str
    config: dict[str, Any] = Field(default_factory=dict)
    secrets_ref: str | None = None
    status: SourceStatus = SourceStatus.active
    freshness_sla_seconds: int | None = None
    tenant_id: uuid.UUID | None = None


class SourceRead(BaseModel):
    """Full source representation returned to callers."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: SourceType
    name: str
    config: dict[str, Any]
    secrets_ref: str | None
    status: SourceStatus
    last_sync_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    freshness_sla_seconds: int | None
    tenant_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class SourceUpdate(BaseModel):
    """Partial update payload — all fields optional."""

    name: str | None = None
    config: dict[str, Any] | None = None
    secrets_ref: str | None = None
    status: SourceStatus | None = None
    freshness_sla_seconds: int | None = None


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


class DocumentCreate(BaseModel):
    """Fields required when creating a new document row."""

    source_id: uuid.UUID
    external_id: str
    uri: str
    title: str | None = None
    content_hash: str
    doc_version: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentRead(BaseModel):
    """Full document representation.

    The ORM attribute is ``doc_metadata``; we expose it as ``metadata``
    via the ``alias`` / ``validation_alias`` mechanism.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    source_id: uuid.UUID
    external_id: str
    uri: str
    title: str | None
    content_hash: str
    doc_version: int
    metadata: dict[str, Any] = Field(alias="doc_metadata", default_factory=dict)
    indexed_at: datetime
    tombstoned_at: datetime | None


class DocumentUpdate(BaseModel):
    """Partial update payload used during incremental re-ingestion."""

    uri: str | None = None
    title: str | None = None
    content_hash: str | None = None
    doc_version: int | None = None
    metadata: dict[str, Any] | None = None
    tombstoned_at: datetime | None = None


# ---------------------------------------------------------------------------
# Ingestion runs
# ---------------------------------------------------------------------------


class IngestionRunCreate(BaseModel):
    """Fields to provide when starting a new ingestion run."""

    source_id: uuid.UUID
    status: IngestionRunStatus = IngestionRunStatus.running


class IngestionRunRead(BaseModel):
    """Full ingestion run representation.

    The ORM attribute is ``run_errors``; exposed as ``errors`` here.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    source_id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None
    status: IngestionRunStatus
    docs_new: int
    docs_updated: int
    docs_removed: int
    errors: dict[str, Any] = Field(alias="run_errors", default_factory=dict)


class IngestionRunUpdate(BaseModel):
    """Payload for closing out a run on finish or error."""

    finished_at: datetime | None = None
    status: IngestionRunStatus | None = None
    docs_new: int | None = None
    docs_updated: int | None = None
    docs_removed: int | None = None
    errors: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


class ChunkCreate(BaseModel):
    """Fields required when inserting a new chunk."""

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


class ChunkRead(BaseModel):
    """Full chunk representation (embedding omitted by default — large).

    The ORM attribute is ``chunk_metadata``; exposed as ``metadata`` here.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    document_id: uuid.UUID
    ord: int
    text: str
    symbol: str | None
    ingestion_run_id: uuid.UUID | None
    embedding_model: str
    embedding_provider: str
    parser_version: str
    chunker_strategy: str
    metadata: dict[str, Any] = Field(alias="chunk_metadata", default_factory=dict)


# ---------------------------------------------------------------------------
# API tokens
# ---------------------------------------------------------------------------


class ApiTokenCreate(BaseModel):
    """Fields provided when minting a new API token."""

    name: str
    hashed_token: str
    token_prefix: str
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None


class ApiTokenRead(BaseModel):
    """Token representation — never exposes ``hashed_token``."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    token_prefix: str
    scopes: list[str]
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    is_active: bool


# ---------------------------------------------------------------------------
# Entities (symbol graph nodes)
# ---------------------------------------------------------------------------


class EntityCreate(BaseModel):
    """Fields required when inserting a new entity."""

    source_id: uuid.UUID
    entity_type: str
    name: str
    display_name: str
    chunk_id: uuid.UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EntityRead(BaseModel):
    """Full entity representation returned to callers.

    The ORM attribute is ``entity_metadata``; exposed as ``metadata`` here.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    source_id: uuid.UUID
    entity_type: str
    name: str
    display_name: str
    chunk_id: uuid.UUID | None
    metadata: dict[str, Any] = Field(alias="entity_metadata", default_factory=dict)
    created_at: datetime


# ---------------------------------------------------------------------------
# Edges (symbol graph relationships)
# ---------------------------------------------------------------------------


class EdgeCreate(BaseModel):
    """Fields required when inserting a new edge."""

    source_entity_id: uuid.UUID
    target_entity_id: uuid.UUID
    edge_type: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class EdgeRead(BaseModel):
    """Full edge representation returned to callers.

    The ORM attribute is ``edge_metadata``; exposed as ``metadata`` here.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    source_entity_id: uuid.UUID
    target_entity_id: uuid.UUID
    edge_type: str
    metadata: dict[str, Any] = Field(alias="edge_metadata", default_factory=dict)
    created_at: datetime


__all__ = [
    "ApiTokenCreate",
    "ApiTokenRead",
    "ChunkCreate",
    "ChunkRead",
    "DocumentCreate",
    "DocumentRead",
    "DocumentUpdate",
    "EdgeCreate",
    "EdgeRead",
    "EntityCreate",
    "EntityRead",
    "IngestionRunCreate",
    "IngestionRunRead",
    "IngestionRunUpdate",
    "SourceCreate",
    "SourceRead",
    "SourceUpdate",
]
