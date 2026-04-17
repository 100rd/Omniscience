"""Initial schema — sources, documents, chunks, ingestion_runs, api_tokens.

Revision ID: 0001
Revises:
Create Date: 2026-04-17 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Embedding dimension — change here + re-embed everything if you switch models.
EMBEDDING_DIM: int = 768


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Step 1: Enable pgvector extension
    # ------------------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ------------------------------------------------------------------
    # Step 2: Enumerations
    # ------------------------------------------------------------------
    source_type = postgresql.ENUM(
        "git",
        "fs",
        "confluence",
        "notion",
        "slack",
        "jira",
        "grafana",
        "k8s",
        "terraform",
        name="source_type",
    )
    source_type.create(op.get_bind(), checkfirst=True)

    source_status = postgresql.ENUM(
        "active",
        "paused",
        "error",
        name="source_status",
    )
    source_status.create(op.get_bind(), checkfirst=True)

    ingestion_run_status = postgresql.ENUM(
        "running",
        "ok",
        "partial",
        "error",
        name="ingestion_run_status",
    )
    ingestion_run_status.create(op.get_bind(), checkfirst=True)

    # ------------------------------------------------------------------
    # Step 3: sources
    # ------------------------------------------------------------------
    op.create_table(
        "sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "type",
            sa.Enum(
                "git",
                "fs",
                "confluence",
                "notion",
                "slack",
                "jira",
                "grafana",
                "k8s",
                "terraform",
                name="source_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("secrets_ref", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("active", "paused", "error", name="source_status", create_type=False),
            nullable=False,
            server_default="active",
        ),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("freshness_sla_seconds", sa.Integer(), nullable=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_sources_tenant_name"),
    )
    op.create_index("ix_sources_status", "sources", ["status"])

    # ------------------------------------------------------------------
    # Step 4: ingestion_runs (referenced by chunks FK)
    # ------------------------------------------------------------------
    op.create_table(
        "ingestion_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "ok",
                "partial",
                "error",
                name="ingestion_run_status",
                create_type=False,
            ),
            nullable=False,
            server_default="running",
        ),
        sa.Column("docs_new", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("docs_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("docs_removed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "errors",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_index("ix_ingestion_runs_source_id", "ingestion_runs", ["source_id"])

    # ------------------------------------------------------------------
    # Step 5: documents
    # ------------------------------------------------------------------
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("doc_version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "indexed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("tombstoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("source_id", "external_id", name="uq_documents_source_external"),
    )
    op.create_index("ix_documents_indexed_at", "documents", ["indexed_at"])
    op.create_index(
        "ix_documents_active",
        "documents",
        ["source_id"],
        postgresql_where=sa.text("tombstoned_at IS NULL"),
    )

    # ------------------------------------------------------------------
    # Step 6: chunks
    # ------------------------------------------------------------------
    op.create_table(
        "chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "text_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=False,
        ),
        sa.Column(
            "embedding",
            sa.Text(),  # placeholder; replaced below with vector type
            nullable=True,
        ),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column(
            "ingestion_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ingestion_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("embedding_provider", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("chunker_strategy", sa.Text(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    # Fix embedding column to use proper vector type
    op.execute("ALTER TABLE chunks DROP COLUMN embedding")
    op.execute(f"ALTER TABLE chunks ADD COLUMN embedding vector({EMBEDDING_DIM})")

    op.create_index("ix_chunks_document_ord", "chunks", ["document_id", "ord"])
    op.create_index(
        "ix_chunks_text_tsv",
        "chunks",
        ["text_tsv"],
        postgresql_using="gin",
    )
    # HNSW index for approximate nearest-neighbour vector search
    op.execute(
        """
        CREATE INDEX ix_chunks_embedding_hnsw
        ON chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )
    op.create_index(
        "ix_chunks_embedding_model_provider",
        "chunks",
        ["embedding_model", "embedding_provider"],
    )
    op.create_index("ix_chunks_parser_version", "chunks", ["parser_version"])

    # ------------------------------------------------------------------
    # Step 7: api_tokens
    # ------------------------------------------------------------------
    op.create_table(
        "api_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("hashed_token", sa.Text(), nullable=False),
        sa.Column("token_prefix", sa.Text(), nullable=False),
        sa.Column(
            "scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    op.drop_table("api_tokens")
    op.drop_table("chunks")
    op.drop_table("documents")
    op.drop_table("ingestion_runs")
    op.drop_table("sources")

    sa.Enum(name="ingestion_run_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="source_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="source_type").drop(op.get_bind(), checkfirst=True)

    op.execute("DROP EXTENSION IF EXISTS vector")
