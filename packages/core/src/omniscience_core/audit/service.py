"""Audit-log service-layer helpers (issue #243).

Two functions:

* :func:`record_tool_invocation` — append-only write from a handler.
* :func:`get_audit_entry` — workspace-scoped read by ``audit_log_id``.

The service layer is intentionally thin — every operation is a single
SQL statement.  No transactions span the audit-log write and the
underlying tool's work because the audit-log row is by design a
*post-hoc* record of the response the caller actually saw; rolling
back the audit row when the caller's response succeeded would create
a phantom that the caller never observed.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from omniscience_core.audit.fingerprint import (
    FINGERPRINT_ALGORITHM,
    compute_state_fingerprint,
)
from omniscience_core.audit.models import AuditLog

log = structlog.get_logger(__name__)


class AuditLogNotFoundError(LookupError):
    """Raised when an audit-log row is missing or invisible to caller."""


async def record_tool_invocation(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    tool_name: str,
    arguments: dict[str, Any],
    response: dict[str, Any],
    as_of: datetime | None,
    confidence: float | None = None,
) -> AuditLog:
    """Append one audit-log row and return the persisted entity.

    Computes the state-fingerprint over ``response`` inside this call
    so the persisted hash and the hash returned to the caller in the
    replay envelope are guaranteed to be the same value (same input,
    same function, same process).

    Caller responsibilities
    -----------------------

    * Pass ``response`` AFTER it is fully populated — including any
      ``effective_as_of`` echo field.  The fingerprint binds to the
      bytes the caller actually saw.
    * Commit the surrounding ``AsyncSession`` — this helper does
      ``session.add()`` + ``session.flush()`` but never ``commit()``.
      The caller controls the transaction boundary so audit-log
      writes can be batched with other state changes when sensible.

    Failure mode
    ------------

    A SQLAlchemy error here propagates.  The handler is expected to
    catch it, log structurally, and either fail the request or swallow
    the audit failure (depending on whether audit is a hard
    requirement for the surface).  Replay-served reads MUST treat
    audit-write failure as fatal; ingestion-side calls MAY treat it
    as soft (operator decision).
    """
    fingerprint = compute_state_fingerprint(response)
    row = AuditLog(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        tool_name=tool_name,
        arguments=arguments,
        response=response,
        state_fingerprint=fingerprint,
        fingerprint_algorithm=FINGERPRINT_ALGORITHM,
        confidence=confidence,
        as_of=as_of,
    )
    session.add(row)
    await session.flush()
    log.info(
        "audit_log_recorded",
        audit_log_id=str(row.id),
        workspace_id=str(workspace_id),
        tool_name=tool_name,
        state_fingerprint=fingerprint,
        as_of=as_of.isoformat() if as_of else None,
    )
    return row


async def get_audit_entry(
    session: AsyncSession,
    *,
    audit_log_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> AuditLog:
    """Fetch one audit-log row, scoped to ``workspace_id``.

    Cross-workspace lookups raise :class:`AuditLogNotFoundError` — the
    row's existence is never leaked outside its owning workspace.
    """
    stmt = select(AuditLog).where(
        AuditLog.id == audit_log_id,
        AuditLog.workspace_id == workspace_id,
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        raise AuditLogNotFoundError(str(audit_log_id))
    return row


__all__ = [
    "AuditLogNotFoundError",
    "get_audit_entry",
    "record_tool_invocation",
]
