"""Create ``entity_emitter`` table for server-side dedup (issue #164).

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-30 00:00:00.000000

Context
-------

Issue #164 introduces server-side deduplication of operator vs k8s-agentic
emitters per ``(workspace_id, external_id)``.  Once the operator (#163) is
authoritative for the entities it emits, the agentic ``k8s`` connector
must not double-write the same resource.

This migration creates a single small table whose composite primary key
is ``(workspace_id, external_id)``.  Each row stores the *current
authoritative emitter* for that pair plus ``last_emit_at`` (used by the
TTL re-assignment rule).  The table is intentionally dimensioned for
constant-time lookup on every event: PK lookup only, no sequential
scan ever.

The state machine that drives reads/writes lives in
``apps/server/src/omniscience_server/ingestion/dedup.py`` — see that
module for the full transition rules.  The table itself is a dumb store.

ACL invariant
-------------

``workspace_id`` is the **first** column of the composite primary key.
Every read and every write touches the row scoped to a single workspace.
No cross-workspace lookup is possible by construction (the worker is
trusted not to forge ``workspace_id``; ingestion always derives it from
``Source.tenant_id``, never from the event payload — see ADR-0007 §ACL
and the worker's ``_resolve_workspace`` method).

Reversibility
-------------

``downgrade()`` drops the table.  Reversal is safe: the dedup module
falls back to "accept everything" when the table is missing (defensive
posture), and any in-flight authority decisions are simply re-established
on the next emit from any source.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    op.create_table(
        "entity_emitter",
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "external_id",
            sa.Text(),
            nullable=False,
        ),
        # Current authoritative emitter for this (workspace_id, external_id).
        # Free-form Text rather than an enum so adding a future emitter (e.g.
        # ``terraform``) does not require a schema migration; the application
        # layer validates the membership via the Emitter Literal in
        # ``ingestion/dedup.py``.
        sa.Column(
            "authority_emitter",
            sa.Text(),
            nullable=False,
        ),
        # Wall-clock time of the most recent emit observed for this row from
        # the *current* authoritative emitter. The TTL rule reads this column
        # to decide whether the operator counts as "still alive".
        sa.Column(
            "last_emit_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        # Time of the most recent authority transition (NULL on insert; set
        # on every operator->agentic or agentic->operator flip). Used for
        # operational dashboards and post-incident review; the dedup state
        # machine itself does not read this column.
        sa.Column(
            "authority_changed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "external_id",
            name="pk_entity_emitter",
        ),
    )
    # Optional secondary index on (authority_emitter, last_emit_at) for the
    # gauge sampler that reports per-emitter authority counts. Cheap to
    # maintain because writes touch one row at a time. Keeping it out of
    # the hot read path entirely — every dedup decision uses the PK only.
    op.create_index(
        "ix_entity_emitter_authority",
        "entity_emitter",
        ["authority_emitter"],
    )


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    op.drop_index("ix_entity_emitter_authority", table_name="entity_emitter")
    op.drop_table("entity_emitter")
