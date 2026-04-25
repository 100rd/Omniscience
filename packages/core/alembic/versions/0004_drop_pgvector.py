"""Drop the pgvector column, HNSW index, and extension (Epic #96 cutover, issue #105).

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-24 00:00:00.000000

Context
-------

As of v0.2 Omniscience's vector store is Qdrant (ADR-0006) and its
graph store is Neo4j (ADR-0005).  Postgres is now operational
metadata only — sources, documents (row-level), ingestion_runs,
api_tokens, workspaces, chunks (text + lineage), entities, and
edges stay; the pgvector ``embedding`` column on ``chunks`` and
its HNSW index are removed here along with the ``vector``
extension itself.

Pre-v0.2 operators with data to migrate should run
``scripts/migrate_to_hybrid.py`` (retained as-is) **before**
applying this migration; the pgvector column is read by that
script.  Once this migration runs, the column and extension are
gone and the migration script is no longer a valid data path for
this database — only for fresh bring-up of new clusters.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # Drop the HNSW index first so it is no longer a dependency of the
    # column we are about to drop.  ``IF EXISTS`` lets us re-run the
    # migration idempotently (e.g. after a partial failure).
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")

    # Drop the vector column.  Cascade is unnecessary — no other object
    # depends on it once the HNSW index is gone.
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS embedding")

    # Drop the pgvector extension so the database no longer loads its
    # shared library on connection.  ``IF EXISTS`` again for idempotence.
    op.execute("DROP EXTENSION IF EXISTS vector")


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Restoring pgvector requires re-embedding every chunk — the vectors
    # are not recoverable from Postgres alone once we have dropped the
    # column.  The only supported "downgrade" path is a database restore
    # from a pre-0004 backup plus a rerun of the ingestion pipeline.
    #
    # We still re-create the extension, column, and HNSW index so that
    # an alembic ``downgrade`` does not leave the schema in an
    # inconsistent state, but callers MUST follow up with a re-ingest.

    # Embedding dimension kept in sync with 0001_initial.EMBEDDING_DIM.
    # If the embedding model changes, both constants must be updated
    # together; see ADR-0006 §Embedding model lifecycle.
    embedding_dim = 768

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(f"ALTER TABLE chunks ADD COLUMN embedding vector({embedding_dim})")
    op.execute(
        """
        CREATE INDEX ix_chunks_embedding_hnsw
        ON chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )
