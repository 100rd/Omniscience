"""Backfill NULL tenant_id / workspace_id to the default workspace.

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-10 00:00:00.000000

Context
-------
Prior to multi-tenancy (issue #57 / migration 0003_workspaces), the
``sources`` and ``api_tokens`` tables were created without a workspace
scoping column.  Migration 0003 added ``api_tokens.workspace_id`` (nullable)
and introduced the ``sources.tenant_id`` column (also nullable), leaving
pre-existing rows with NULL in those fields.

The original ``workspace_filter`` helper included an ``OR col IS NULL``
clause so that those legacy rows would remain visible after the migration.
That clause is a cross-tenant vulnerability: any workspace-scoped token can
read NULL-tenant rows even though they conceptually belong to the
installation-level default workspace (``00000000-0000-0000-0000-000000000001``).

This migration eliminates the ambiguity by assigning the default workspace
UUID to every row that still carries NULL:

    UPDATE sources     SET tenant_id   = DEFAULT_WS WHERE tenant_id   IS NULL
    UPDATE api_tokens  SET workspace_id = DEFAULT_WS WHERE workspace_id IS NULL

After this migration the ``workspace_filter`` helper can drop the
``OR col IS NULL`` branch and use a plain equality filter, closing the
cross-tenant read vector entirely.

Tables surveyed
---------------
* ``sources``           — has ``tenant_id`` (nullable UUID).  Backfilled.
* ``api_tokens``        — has ``workspace_id`` (nullable UUID FK →
                          workspaces.id).  Backfilled.
* ``documents``         — no workspace column; isolated transitively via
                          ``source_id → sources.tenant_id``.  Not touched.
* ``chunks``            — no workspace column; isolated via
                          ``document_id → documents → sources``.  Not touched.
* ``ingestion_runs``    — no workspace column; isolated via
                          ``source_id → sources``.  Not touched.
* ``entities``          — no workspace column; isolated via
                          ``source_id → sources``.  Not touched.
* ``edges``             — no workspace column; isolated via
                          ``source_entity_id/target_entity_id → entities``
                          → sources``.  Not touched.
* ``entity_emitter``    — ``workspace_id`` is part of the composite PK
                          (NOT NULL), so there are no NULL rows.  Not touched.
* ``workspaces``        — the lookup table itself.  Not touched.

Downgrade strategy
------------------
Reverting NULL-assignment is ambiguous: we cannot distinguish rows that were
originally NULL (and therefore meaningfully belong to the default workspace)
from rows that were explicitly assigned the default workspace UUID by an
operator.  Setting them back to NULL would re-introduce the security hole.

The downgrade is therefore implemented as a **no-op** (consistent with the
enum-value migrations 0005/0006/0009 that also cannot be meaningfully
reverted).  If you need to roll back to the previous code version:

1. Deploy the previous code *first* (the old ``workspace_filter`` with the
   OR-NULL branch is backward-compatible with backfilled rows).
2. The data remains valid — default-workspace rows simply show as
   ``tenant_id = DEFAULT_WS`` instead of NULL; the old code's
   ``or_(col == ws, col == null())`` still returns the correct superset.

Pre-condition
-------------
The default workspace row (``id = 00000000-0000-0000-0000-000000000001``,
``name = 'default'``) must exist in the ``workspaces`` table before the
backfill runs.  This was inserted by migration 0003_workspaces and must not
be deleted manually.  The UPDATE to ``api_tokens`` respects the FK constraint
(``ON DELETE SET NULL``) — the workspace row is only deleted if an operator
explicitly removes it, which would reset the FK to NULL again; no special
handling is required here.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Canonical default workspace UUID — seeded by 0003_workspaces.
_DEFAULT_WORKSPACE_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Upgrade — backfill NULL rows to default workspace
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # Use text() so the UUID literal is rendered portably without having to
    # reflect the full table schema.  Both columns are UUID type in Postgres.
    default_ws = str(_DEFAULT_WORKSPACE_ID)

    # 1. sources.tenant_id ------------------------------------------------
    # Cast the bound string to uuid explicitly: both columns are Postgres
    # ``uuid`` and asyncpg will not implicitly coerce a VARCHAR parameter.
    op.execute(
        sa.text(
            "UPDATE sources SET tenant_id = CAST(:ws_id AS uuid) WHERE tenant_id IS NULL"
        ).bindparams(ws_id=default_ws)
    )

    # 2. api_tokens.workspace_id ------------------------------------------
    # The FK references workspaces(id); the default workspace row was
    # inserted by 0003_workspaces so this UPDATE will not violate referential
    # integrity as long as that row has not been manually deleted.
    op.execute(
        sa.text(
            "UPDATE api_tokens SET workspace_id = CAST(:ws_id AS uuid) WHERE workspace_id IS NULL"
        ).bindparams(ws_id=default_ws)
    )


# ---------------------------------------------------------------------------
# Downgrade — intentional no-op (see module docstring)
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Cannot safely distinguish originally-NULL rows from explicitly-set ones.
    # Rolling back the data would re-introduce the cross-tenant read vector.
    # Deploy the previous application version first; data stays valid.
    pass
