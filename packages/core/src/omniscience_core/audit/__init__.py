"""Replay audit-log package (issue #243).

Persistence layer for the "what did the agent see at time T" replay
primitive.  Each original tool call records a single row capturing the
inputs, the response, the bitemporal anchor T, and the deterministic
``state_fingerprint`` hash over the response payload.  Replay then
re-runs the same underlying tool against the bitemporal graph at the
recorded T and asserts the fingerprint matches.

Exports
-------

* :class:`AuditLog` — SQLAlchemy 2 model for ``audit_log`` table.
* :func:`compute_state_fingerprint` — canonical-JSON BLAKE2b-256 hex.
* :func:`record_tool_invocation` — write a row from a handler.
* :func:`get_audit_entry` — workspace-scoped fetch by id.

The audit-log row is **append-only** by contract.  No UPDATE / DELETE
path is exposed from the application code (the migration enforces no
unique constraints beyond the PK, and retention is managed out-of-band
per ADR-0009 tiering — see issue #138).

ACL invariant
-------------

Every read is keyed on ``(id, workspace_id)``.  A row created in
workspace A is invisible to workspace B by construction — the SQL
``WHERE`` clause is unconditional.  See ADR-0007 §ACL for the broader
fail-closed posture.
"""

from __future__ import annotations

from omniscience_core.audit.fingerprint import (
    FINGERPRINT_ALGORITHM,
    FINGERPRINT_HEX_LENGTH,
    compute_state_fingerprint,
)
from omniscience_core.audit.models import AuditLog
from omniscience_core.audit.service import (
    AuditLogNotFoundError,
    get_audit_entry,
    record_tool_invocation,
)

__all__ = [
    "FINGERPRINT_ALGORITHM",
    "FINGERPRINT_HEX_LENGTH",
    "AuditLog",
    "AuditLogNotFoundError",
    "compute_state_fingerprint",
    "get_audit_entry",
    "record_tool_invocation",
]
