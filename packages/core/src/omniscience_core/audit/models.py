"""SQLAlchemy model for the replay audit-log table (issue #243).

One row per *audited* tool invocation.  The table is append-only by
contract; there is no application-level UPDATE or DELETE.  Retention
is managed out-of-band per ADR-0009 (issue #138) — the table is
expected to be the largest in the schema and is designed for
cold-tier archival from day one.

Schema rationale
----------------

* ``id`` — opaque UUID surfaced as the "audit_log_id" the admin UI
  pastes into the replay page.  Generated client-side so the row is
  insertable in one round-trip with no ``RETURNING`` clause.
* ``workspace_id`` — first column of every read; ACL invariant.
* ``tool_name`` — short string (``search`` / ``get_entity`` /
  ``get_related_entities`` / ``resolve_incident``).  Text rather than
  enum so adding a new auditable tool does not require a migration.
* ``arguments`` — JSONB; the wire arguments the caller passed.
  Includes ``as_of`` when supplied.
* ``response`` — JSONB; the exact payload the handler returned.
  Storing it lets the admin UI render the original alongside the
  re-played response without re-executing the tool.
* ``state_fingerprint`` — 64-char hex digest of ``response`` per
  :mod:`omniscience_core.audit.fingerprint`.  Indexed for "find me
  every audit row with the same fingerprint" auditor queries.
* ``fingerprint_algorithm`` — short algorithm label; lets a future
  v2 hash coexist with v1 rows in the same table.
* ``confidence`` — optional float; populated by ``resolve_incident``,
  ``NULL`` for tools that do not surface a confidence model yet
  (search/get_entity/get_related_entities as of v0.5).
* ``recorded_at`` — wall-clock when the row was written (server now).
* ``as_of`` — the bitemporal anchor T the tool was asked to read at.
  ``NULL`` means "still-current at recorded_at" — replay reuses
  ``recorded_at`` as T in that case.

Indexes
-------

* PK on ``id``.
* ``ix_audit_log_workspace_recorded_at`` — the dominant operational
  query ("show me the last N tool calls in this workspace").
* ``ix_audit_log_fingerprint`` — auditor queries.
* ``ix_audit_log_workspace_tool_recorded_at`` — per-tool funnel views.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    Index,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from omniscience_core.db.models import Base


class AuditLog(Base):
    """Append-only audit-log row capturing one tool invocation.

    See module docstring for the schema rationale.  Read access is
    via :func:`omniscience_core.audit.service.get_audit_entry`,
    which enforces the workspace ACL.
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    arguments: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    response: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    state_fingerprint: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    fingerprint_algorithm: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    confidence: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    as_of: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        Index(
            "ix_audit_log_workspace_recorded_at",
            "workspace_id",
            "recorded_at",
        ),
        Index(
            "ix_audit_log_fingerprint",
            "state_fingerprint",
        ),
        Index(
            "ix_audit_log_workspace_tool_recorded_at",
            "workspace_id",
            "tool_name",
            "recorded_at",
        ),
    )


__all__ = ["AuditLog"]
