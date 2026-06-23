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
    s3 = "s3"
    aws = "aws"
    alerts = "alerts"
    otel = "otel"
    # k8s_operator — in-cluster operator emitter (ADR-0007). Distinct from
    # the agentic ``k8s`` connector during the parallel-deprecation window
    # (epic #98); allows server-side dedup (#164) and the operator-scoped
    # read endpoint (#163) to disambiguate.
    k8s_operator = "k8s_operator"


class SourceStatus(enum.StrEnum):
    active = "active"
    paused = "paused"
    error = "error"


class SyncMode(enum.StrEnum):
    """Controls whether the scheduler auto-triggers sync for a source.

    ``pull`` — default; the in-stack scheduler re-syncs the source on TTL
               expiry by publishing a trigger to NATS (pull/discovery model).

    ``push`` — the source is driven by an external process (e.g. a launchd
               script or seed tool).  The scheduler skips it entirely to avoid
               attempted in-cluster discovery from an off-cluster host.
               Freshness monitoring is still active — a silent push process is
               a valuable operational signal.
    """

    pull = "pull"
    push = "push"


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
# Workspaces
# ---------------------------------------------------------------------------


class Workspace(Base):
    """Multi-tenant workspace — top-level isolation boundary.

    Every resource (sources, tokens, etc.) can be scoped to a workspace.
    The ``default`` workspace is created automatically on first migration
    and acts as the implicit workspace for tokens that pre-date multi-tenancy.
    """

    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    api_tokens: Mapped[list[ApiToken]] = relationship(
        "ApiToken", back_populates="workspace", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_workspaces_name", "name"),)


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
    sync_mode: Mapped[SyncMode] = mapped_column(
        Enum(SyncMode, name="sync_mode"),
        nullable=False,
        default=SyncMode.pull,
        server_default="pull",
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
        Index("ix_sources_sync_mode", "sync_mode"),
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


from sqlalchemy.types import UserDefinedType


class SqlVector(UserDefinedType):
    """Custom SQLAlchemy type for pgvector's vector type."""

    def get_col_spec(self, **kw):
        return "vector"

    def bind_processor(self, dialect):
        def process(value):
            if value is None:
                return None
            return f"[{','.join(map(str, value))}]"

        return process

    def result_processor(self, dialect, coltype):
        def process(value):
            if value is None:
                return None
            if isinstance(value, str):
                return [float(x) for x in value.strip("[]").split(",") if x]
            return list(value)

        return process


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
    embedding: Mapped[list[float] | None] = mapped_column(SqlVector, nullable=True)

    document: Mapped[Document] = relationship("Document", back_populates="chunks")
    ingestion_run: Mapped[IngestionRun | None] = relationship(
        "IngestionRun", back_populates="chunks"
    )
    entities: Mapped[list[Entity]] = relationship("Entity", back_populates="chunk")

    __table_args__ = (
        Index("ix_chunks_document_ord", "document_id", "ord"),
        Index("ix_chunks_text_tsv", "text_tsv", postgresql_using="gin"),
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
    """API token scoped to an optional workspace."""

    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    hashed_token: Mapped[str] = mapped_column(Text, nullable=False)
    token_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    workspace: Mapped[Workspace | None] = relationship("Workspace", back_populates="api_tokens")

    __table_args__ = (Index("ix_api_tokens_workspace_id", "workspace_id"),)


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
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
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
      ``"candidate-edge"`` — probabilistic relationship between entities via ML clustering
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
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
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


# ---------------------------------------------------------------------------
# Entity emitter (server-side dedup state — issue #164)
# ---------------------------------------------------------------------------


class EntityEmitter(Base):
    """Tracks the authoritative emitter for a ``(workspace_id, external_id)`` pair.

    Used by the ingestion-worker dedup module
    (:mod:`omniscience_server.ingestion.dedup`) to decide whether an
    incoming event from the agentic ``k8s`` connector should be accepted
    or dropped because the in-cluster operator (#163) is already
    authoritative for that resource.

    The composite primary key ``(workspace_id, external_id)`` is the
    *only* index touched on the hot path.  Each event triggers a single
    PK lookup; concurrency is handled via ``INSERT ... ON CONFLICT
    DO UPDATE`` with a guarded ``WHERE`` clause so the state-machine
    transitions are atomic.

    ACL invariant — ``workspace_id`` is the first column of the PK and
    every read scopes by it.  No cross-workspace lookup is structurally
    possible (see ADR-0007 §ACL).
    """

    __tablename__ = "entity_emitter"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    external_id: Mapped[str] = mapped_column(
        Text,
        primary_key=True,
        nullable=False,
    )
    authority_emitter: Mapped[str] = mapped_column(Text, nullable=False)
    last_emit_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    authority_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (Index("ix_entity_emitter_authority", "authority_emitter"),)


class OutboxEvent(Base):
    """Outbox table for reliable event publishing (Outbox pattern)."""

    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_outbox_events_processed", "processed"),
        Index("ix_outbox_events_created_at", "created_at"),
    )


__all__ = [
    "ApiToken",
    "Base",
    "Chunk",
    "Document",
    "Edge",
    "Entity",
    "EntityEmitter",
    "IngestionRun",
    "IngestionRunStatus",
    "OutboxEvent",
    "Source",
    "SourceStatus",
    "SourceType",
    "SyncMode",
    "Workspace",
]
