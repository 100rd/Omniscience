"""Add ``k8s_operator`` value to the ``source_type`` enum (issue #163).

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-30 00:00:00.000000

Context
-------

The reconciliation worker (#163) and its companion read API endpoint
need to distinguish operator-emitted entities from the agentic ``k8s``
connector during the parallel-deprecation window (epic #98).  This
migration adds the new enum value so the SQLAlchemy
``Enum(SourceType, name="source_type")`` column accepts
``"k8s_operator"`` without a CHECK violation.

Additive-only — same shape as ``0005_source_type_otel`` (issue #152).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ``ALTER TYPE ... ADD VALUE`` cannot run inside a transaction in
    # Postgres < 12; pin AUTOCOMMIT for the duration of this DDL so the
    # migration applies cleanly on every supported server version.
    op.execute("COMMIT")
    op.execute("ALTER TYPE source_type ADD VALUE IF NOT EXISTS 'k8s_operator'")


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Postgres does not support ``ALTER TYPE ... DROP VALUE``; downgrade is
    # a no-op (consistent with 0005 and the rest of the tree).
    pass
