"""Add sync_mode column to sources (push/pull model separation).

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-10 00:00:00.000000

Context
-------
Omniscience supports two source synchronisation models:

* **pull** (default) — the in-stack ``SchedulerWorker`` triggers a re-sync
  automatically when a source TTL expires.  Works for sources that have a
  discoverable in-cluster endpoint (git, s3, confluence, …).

* **push** — an external process (launchd script, seed tool, operator) is
  exclusively responsible for pushing data.  The scheduler must *not*
  auto-trigger these sources because the discovery worker would attempt to
  reach an in-cluster URL (e.g. ``kubernetes.default.svc``) from an
  off-cluster host, producing a cascade of connection errors.

The distinction is captured in the new ``sync_mode`` column:

    ALTER TABLE sources
      ADD COLUMN sync_mode sync_mode NOT NULL DEFAULT 'pull';

All pre-existing rows default to ``'pull'`` (backward-compatible — existing
pull sources continue to be auto-triggered as before).

Push sources created by seed scripts or external agents should set
``sync_mode = 'push'``.  The ``SchedulerWorker`` skips these sources entirely
when selecting candidates for automatic trigger.

Freshness monitoring is intentionally **not** disabled for push sources:
a push process that silently stops updating ``last_sync_at`` should trigger
a staleness alert so operators are notified of pipeline outage.  The fix for
push sources emitting spurious freshness warnings is to have the seed scripts
update ``last_sync_at`` on every run, not to silence the checker.

Enum
----
A new Postgres ENUM ``sync_mode`` is created with values ``'pull'`` and
``'push'``.  Using an enum (rather than a boolean ``externally_managed``)
keeps the door open for future modes such as ``'webhook'`` or ``'polling'``
without a destructive schema change.

Downgrade strategy
------------------
Dropping an ENUM type requires that no column references it.  The downgrade:

1. Drops the ``ix_sources_sync_mode`` index.
2. Drops the ``sync_mode`` column.
3. Drops the ``sync_mode`` ENUM type.

Data loss: the ``sync_mode`` values are permanently lost on downgrade.
Re-running ``upgrade`` after a downgrade sets all rows back to ``'pull'``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ENUM_NAME = "sync_mode"
_ENUM_VALUES = ("pull", "push")
_TABLE = "sources"
_COLUMN = "sync_mode"
_INDEX = "ix_sources_sync_mode"

# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # 1. Create the ENUM type.
    sync_mode_enum = sa.Enum(*_ENUM_VALUES, name=_ENUM_NAME)
    sync_mode_enum.create(op.get_bind(), checkfirst=True)

    # 2. Add the column with a server-side default of 'pull' so that the
    #    single DDL statement both adds the column and fills existing rows.
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.Enum(*_ENUM_VALUES, name=_ENUM_NAME, create_type=False),
            nullable=False,
            server_default="pull",
        ),
    )

    # 3. Add a lightweight index to support scheduler queries that filter
    #    on sync_mode = 'pull'.  The cardinality is low (2 values) but the
    #    table can be large, and a partial index on 'pull' would need
    #    updating if new modes are added; a plain btree is safe and small.
    op.create_index(_INDEX, _TABLE, [_COLUMN])


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # 1. Drop the index first (cannot drop column while index exists).
    op.drop_index(_INDEX, table_name=_TABLE)

    # 2. Drop the column.
    op.drop_column(_TABLE, _COLUMN)

    # 3. Drop the ENUM type (safe now that no column references it).
    sync_mode_enum = sa.Enum(*_ENUM_VALUES, name=_ENUM_NAME)
    sync_mode_enum.drop(op.get_bind(), checkfirst=True)
