"""Add ``otel`` value to the ``source_type`` enum (issue #152).

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-24 00:00:00.000000

Context
-------

The OpenTelemetry receiver (#152) introduces a new push-based source
that does not pull files like the other connectors.  A per-workspace
``Source`` row of type ``otel`` carries the entities and edges
created from incoming OTLP/HTTP exports; this migration adds the new
enum value to the existing ``source_type`` Postgres enum so the
SQLAlchemy ``Enum(SourceType, name="source_type")`` column accepts
``"otel"`` without a CHECK violation.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------

revision: str = "0005"
down_revision: str | None = "0004"
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
    op.execute("ALTER TYPE source_type ADD VALUE IF NOT EXISTS 'otel'")


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Postgres does not support ``ALTER TYPE ... DROP VALUE``; the only
    # path is to recreate the enum.  We accept the additive-only nature
    # of this migration and leave downgrade as a no-op (consistent with
    # how additive enum migrations are handled in the rest of this tree).
    pass
