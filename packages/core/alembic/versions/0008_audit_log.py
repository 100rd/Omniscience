"""Create ``audit_log`` table for the replay primitive (issue #243).

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-22 00:00:00.000000

Context
-------

Issue #243 surfaces the existing bitemporal substrate (ADR-0008) as
a first-class governance-facing replay endpoint.  The endpoint needs
a persistence layer for the original tool calls so an auditor can
say "show me what the agent saw at audit_log_id=X" without the
caller having to remember the exact arguments + ``as_of`` they
passed.

This migration creates a single append-only ``audit_log`` table.
Every original tool call (search / get_entity / get_related_entities
/ resolve_incident) writes one row; the replay handler reads the row,
re-runs the tool against the bitemporal graph at the recorded T, and
compares the stored ``state_fingerprint`` with the freshly-computed
one.

ACL invariant
-------------

``workspace_id`` is the first column of every secondary index and is
unconditional in every application read (see
``omniscience_core.audit.service.get_audit_entry``).  Cross-workspace
lookups are not just rejected — they are impossible to express
through the service layer.

Reversibility
-------------

``downgrade()`` drops the table and its indexes.  Reversal loses the
audit history; recovery requires a fresh re-ingestion + new agent
runs.  Production downgrades should snapshot ``audit_log`` to object
storage first; the migration itself does not enforce that because
the snapshot tooling lives in the retention worker (#138), not here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "tool_name",
            sa.Text(),
            nullable=False,
        ),
        # The wire arguments the caller passed.  JSONB so per-tool
        # filter queries ("audits where arguments->>'query' ~ 'oom'")
        # are cheap.
        sa.Column(
            "arguments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # The exact response the handler returned.  Stored verbatim so
        # the admin UI can show "original vs replayed" diffs without
        # re-executing the underlying tool a second time.
        sa.Column(
            "response",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # 64-char lowercase hex digest per
        # ``omniscience_core.audit.fingerprint``.
        sa.Column(
            "state_fingerprint",
            sa.Text(),
            nullable=False,
        ),
        # Algorithm tag so a future v2 hash can coexist with v1 rows
        # in the same table without a schema migration.
        sa.Column(
            "fingerprint_algorithm",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "confidence",
            sa.Float(),
            nullable=True,
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        # The bitemporal anchor the tool was asked to read at.  NULL
        # means "still-current at recorded_at" — replay reuses
        # ``recorded_at`` as T in that case.
        sa.Column(
            "as_of",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
    )
    # Operational query: "last N invocations in workspace X."
    op.create_index(
        "ix_audit_log_workspace_recorded_at",
        "audit_log",
        ["workspace_id", "recorded_at"],
    )
    # Auditor query: "find every row with this fingerprint."
    op.create_index(
        "ix_audit_log_fingerprint",
        "audit_log",
        ["state_fingerprint"],
    )
    # Per-tool funnel ("last N resolve_incident calls in workspace X").
    op.create_index(
        "ix_audit_log_workspace_tool_recorded_at",
        "audit_log",
        ["workspace_id", "tool_name", "recorded_at"],
    )


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    op.drop_index(
        "ix_audit_log_workspace_tool_recorded_at",
        table_name="audit_log",
    )
    op.drop_index("ix_audit_log_fingerprint", table_name="audit_log")
    op.drop_index("ix_audit_log_workspace_recorded_at", table_name="audit_log")
    op.drop_table("audit_log")
