"""Persist the MCP v1 read-token profile and rotation provenance.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Expand tokens with nullable profile and rotation provenance columns."""
    op.add_column("api_tokens", sa.Column("profile_id", sa.Text(), nullable=True))
    op.add_column(
        "api_tokens",
        sa.Column("rotated_from_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_api_tokens_rotated_from_id",
        "api_tokens",
        "api_tokens",
        ["rotated_from_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_api_tokens_profile_id", "api_tokens", ["profile_id"])
    op.create_index(
        "ix_api_tokens_rotated_from_id",
        "api_tokens",
        ["rotated_from_id"],
        unique=True,
    )


def downgrade() -> None:
    """Remove MCP profile metadata without affecting legacy token columns."""
    op.drop_index("ix_api_tokens_rotated_from_id", table_name="api_tokens")
    op.drop_index("ix_api_tokens_profile_id", table_name="api_tokens")
    op.drop_constraint("fk_api_tokens_rotated_from_id", "api_tokens", type_="foreignkey")
    op.drop_column("api_tokens", "rotated_from_id")
    op.drop_column("api_tokens", "profile_id")
