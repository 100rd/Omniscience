"""Add workspaces table and workspace_id FK on api_tokens.

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-17 00:00:00.000000
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Stable UUID for the default workspace — deterministic so re-runs are safe.
_DEFAULT_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. workspaces table
    # ------------------------------------------------------------------
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
    )
    op.create_index("ix_workspaces_name", "workspaces", ["name"])

    # ------------------------------------------------------------------
    # 2. Seed the default workspace
    # ------------------------------------------------------------------
    op.execute(
        sa.text(
            """
            INSERT INTO workspaces (id, name, display_name, settings, created_at, updated_at)
            VALUES (
                :id,
                'default',
                'Default Workspace',
                '{}'::jsonb,
                NOW(),
                NOW()
            )
            ON CONFLICT (name) DO NOTHING
            """
        ).bindparams(id=str(_DEFAULT_WORKSPACE_ID))
    )

    # ------------------------------------------------------------------
    # 3. Add workspace_id FK to api_tokens (nullable for backward compat)
    # ------------------------------------------------------------------
    op.add_column(
        "api_tokens",
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_api_tokens_workspace_id", "api_tokens", ["workspace_id"])


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    op.drop_index("ix_api_tokens_workspace_id", table_name="api_tokens")
    op.drop_column("api_tokens", "workspace_id")
    op.drop_index("ix_workspaces_name", table_name="workspaces")
    op.drop_table("workspaces")
