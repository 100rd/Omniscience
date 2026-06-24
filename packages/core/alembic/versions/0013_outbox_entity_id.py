"""Add entity_id partition key to outbox_events.

Revision ID: 0013_outbox_entity_id
Revises: 0013_add_entity_version
Create Date: 2026-06-24 00:00:00.000000

AP1 — single-writer invariant: adds a nullable ``entity_id`` UUID column
to ``outbox_events`` so per-entity selective replay and ordered polling are
possible.  A composite index on ``(entity_id, created_at)`` supports the
common pattern of "fetch all unprocessed events for entity X in creation
order" without a full-table scan.

Edge events use the ``source_entity_id`` as the partition key; merge/unmerge
events use the acted-on entity id.  NULL is allowed — the version-guard in
the store adapters is the ordering backstop for events that carry no entity
affinity (e.g. bulk document imports where entity identity is resolved later).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers
revision: str = "0013_outbox_entity_id"
down_revision: str | None = "0013_add_entity_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column(
            "entity_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_outbox_events_entity_created",
        "outbox_events",
        ["entity_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_entity_created", table_name="outbox_events")
    op.drop_column("outbox_events", "entity_id")
