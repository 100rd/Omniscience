"""SQLAlchemy 2 declarative models for Omniscience.

All tables live in the ``public`` schema (Postgres default) for the MVP.
Multi-tenant namespacing is deferred to v0.2.

Note: SQLAlchemy reserves the attribute name ``metadata`` on declarative
classes (it refers to the ``MetaData`` object).  All ``metadata`` *columns*
are mapped under the Python attribute ``doc_metadata`` / ``chunk_metadata`` /
``run_errors`` / ``entity_metadata`` / ``edge_metadata``, while the
underlying DB column retains the schema-canonical name.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector  # type: ignore[import-untyped]
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class SourceType(enum.StrEnum):
    git = "git"
    fs = "fs"
    confluence = "confluence"
    notion = "notion"
    slack = "slack"
    jira = "jira"
    grafana = "grafana"
    k8s = "k8s"
    terraform = "terraform"


class SourceStatus(enum.StrEnum):
    active = "active"
    paused = "paused"
    error = "error"


class IngestionRunStatus(enum.StrEnum):
    running = "running"
    ok = "ok"
    partial = "partial"
    error = "error"


# ---------------------------------------------------------------------------
# Declarative base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class Source(Base):
    """Configured ingestion source (one row per connector instance)."""

    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[SourceType] = mapped_column(Enum(SourceType, name="source_type"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    secrets_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[SourceStatus] = mapped_column(
        Enum(SourceStatus, name="source_status"),
        nullable=False,
        default=SourceStatus.active,
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    freshness_sla_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    documents: Mapped[list[Document]] = relationship(
        "Document", back_populates="source", cascade="all, delete-orphan"
    )
    ingestion_runs: Mapped[list[IngestionRun]] = relationship(
        "IngestionRun", back_populates="source", cascade="all, delete-orphan"
    )
    entities: Mapped[list[Entity]] = relationship(
        "Entity", back_populates="source", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_sources_tenant_name"),
        Index("ix_sources_status", "status"),
    )


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


class Document(Base):
    """One row per source-native document (file, wiki page, issue, …)."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    doc_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    # DB column is "metadata"; Python attribute avoids SA reserved name conflict.
    doc_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    tombstoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    source: Mapped[Source] = relationship("Source", back_populates="documents")
    chunks: Mapped[list[Chunk]] = relationship(
        "Chunk", back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_documents_source_external"),
        Index("ix_documents_indexed_at", "indexed_at"),
        Index(
            "ix_documents_active",
            "source_id",
            postgresql_where=text("tombstoned_at IS NULL"),
        ),
    )


# ---------------------------------------------------------------------------
# Ingestion runs
# ---------------------------------------------------------------------------


class IngestionRun(Base):
    """Audit record of a single ingestion attempt for a source."""

    __tablename__ = "ingestion_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[IngestionRunStatus] = mapped_column(
        Enum(IngestionRunStatus, name="ingestion_run_status"),
        nullable=False,
        default=IngestionRunStatus.running,
    )
    docs_new: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    docs_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    docs_removed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # DB column is "errors"; renamed to avoid any potential SA conflicts.
    run_errors: Mapped[dict[str, Any]] = mapped_column(
        "errors", JSONB, nullable=False, default=dict
    )

    source: Mapped[Source] = relationship("Source", back_populates="ingestion_runs")
    chunks: Mapped[list[Chunk]] = relationship("Chunk", back_populates="ingestion_run")

    __table_args__ = (Index("ix_ingestion_runs_source_id", "source_id"),)


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


class Chunk(Base):
    """Chunked, embedded content unit used at retrieval time."""

    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    ord: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', text)", persisted=True),
        nullable=False,
    )
    embedding: Mapped[Any] = mapped_column(Vector(768), nullable=True)
    symbol: Mapped[str | None] = mapped_column(Text, nullable=True)
    ingestion_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ingestion_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_provider: Mapped[str] = mapped_column(Text, nullable=False)
    parser_version: Mapped[str] = mapped_column(Text, nullable=False)
    chunker_strategy: Mapped[str] = mapped_column(Text, nullable=False)
    # DB column is "metadata"; Python attribute avoids SA reserved name conflict.
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )

    document: Mapped[Document] = relationship("Document", back_populates="chunks")
    ingestion_run: Mapped[IngestionRun | None] = relationship(
        "IngestionRun", back_populates="chunks"
    )
    entities: Mapped[list[Entity]] = relationship("Entity", back_populates="chunk")

    __table_args__ = (
        Index("ix_chunks_document_ord", "document_id", "ord"),
        Index("ix_chunks_text_tsv", "text_tsv", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index(
            "ix_chunks_embedding_model_provider",
            "embedding_model",
            "embedding_provider",
        ),
        Index("ix_chunks_parser_version", "parser_version"),
    )


# ---------------------------------------------------------------------------
# API tokens
# ---------------------------------------------------------------------------


class ApiToken(Base):
    """Minimal API token model for single-user MVP."""

    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    hashed_token: Mapped[str] = mapped_column(Text, nullable=False)
    token_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


# ---------------------------------------------------------------------------
# Entities (symbol graph nodes)
# ---------------------------------------------------------------------------


class Entity(Base):
    """A named code entity extracted from a source document.

    Represents a node in the symbol graph.  Each entity has a fully-qualified
    name (FQN) such as ``mymodule.MyClass.my_method`` and a shorter display
    name (``my_method``).  The ``entity_type`` field categorises the symbol
    so graph queries can filter by kind (e.g. only classes, only functions).

    Valid ``entity_type`` values (open-ended, extensible):
      ``"function"``, ``"class"``, ``"module"``, ``"service"``, ``"resource"``
    """

    __tablename__ = "entities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    # FQN: e.g. "mymodule.MyClass.my_method"
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Short display name: e.g. "my_method"
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chunks.id", ondelete="SET NULL"),
        nullable=True,
    )
    # DB column is "metadata"; Python attribute avoids SA reserved name conflict.
    entity_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    source: Mapped[Source] = relationship("Source", back_populates="entities")
    chunk: Mapped[Chunk | None] = relationship("Chunk", back_populates="entities")
    outgoing_edges: Mapped[list[Edge]] = relationship(
        "Edge",
        foreign_keys="Edge.source_entity_id",
        back_populates="source_entity",
        cascade="all, delete-orphan",
    )
    incoming_edges: Mapped[list[Edge]] = relationship(
        "Edge",
        foreign_keys="Edge.target_entity_id",
        back_populates="target_entity",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_entities_source_type", "source_id", "entity_type"),
        Index("ix_entities_name", "name"),
    )


# ---------------------------------------------------------------------------
# Edges (symbol graph relationships)
# ---------------------------------------------------------------------------


class Edge(Base):
    """A directed relationship between two :class:`Entity` nodes.

    Represents an edge in the symbol graph.  The ``edge_type`` describes the
    nature of the relationship:

      ``"imports"``    — module A imports module/symbol B
      ``"calls"``      — function/method A calls function/method B
      ``"inherits"``   — class A inherits from class B
      ``"defines"``    — module A defines entity B
      ``"depends_on"`` — generic dependency (infra resources, services, etc.)
    """

    __tablename__ = "edges"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    edge_type: Mapped[str] = mapped_column(Text, nullable=False)
    # DB column is "metadata"; Python attribute avoids SA reserved name conflict.
    edge_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    source_entity: Mapped[Entity] = relationship(
        "Entity",
        foreign_keys=[source_entity_id],
        back_populates="outgoing_edges",
    )
    target_entity: Mapped[Entity] = relationship(
        "Entity",
        foreign_keys=[target_entity_id],
        back_populates="incoming_edges",
    )

    __table_args__ = (Index("ix_edges_edge_type", "edge_type"),)


__all__ = [
    "ApiToken",
    "Base",
    "Chunk",
    "Document",
    "Edge",
    "Entity",
    "IngestionRun",
    "IngestionRunStatus",
    "Source",
    "SourceStatus",
    "SourceType",
]
