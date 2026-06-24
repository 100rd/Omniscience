"""Add outbox_events table for reliable event publishing.

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-17 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "processed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_outbox_events_processed",
        "outbox_events",
        ["processed"],
        unique=False,
    )
    op.create_index(
        "ix_outbox_events_created_at",
        "outbox_events",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_created_at", table_name="outbox_events")
    op.drop_index("ix_outbox_events_processed", table_name="outbox_events")
    op.drop_table("outbox_events")
