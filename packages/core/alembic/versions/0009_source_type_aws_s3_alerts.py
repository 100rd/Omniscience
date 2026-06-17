"""Add ``s3``, ``aws``, ``alerts`` values to the ``source_type`` enum.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-07 00:00:00.000000

Context
-------

``SourceType`` (``packages/core/.../db/models.py``) defines ``s3``, ``aws``, and
``alerts`` members, but no migration ever added them to the Postgres
``source_type`` enum.  Creating a Source of those types failed at the DB layer
with ``InvalidTextRepresentationError: invalid input value for enum
source_type: "aws"`` (caught while wiring the AWS connector end-to-end, ADR-0014).

Additive-only — same shape as ``0005_source_type_otel`` and
``0006_source_type_k8s_operator``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_VALUES = ("s3", "aws", "alerts")


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ``ALTER TYPE ... ADD VALUE`` cannot run inside a transaction in
    # Postgres < 12; pin AUTOCOMMIT for the duration of this DDL so the
    # migration applies cleanly on every supported server version.
    op.execute("COMMIT")
    for value in _NEW_VALUES:
        op.execute(f"ALTER TYPE source_type ADD VALUE IF NOT EXISTS '{value}'")


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Postgres does not support ``ALTER TYPE ... DROP VALUE``; downgrade is
    # a no-op (consistent with 0005/0006 and the rest of the tree).
    pass
