"""Workspace-id resolution shared by ingestion and migration paths.

The pgvector schema does not carry a ``workspace_id`` column on
``sources`` directly — ``Source.tenant_id`` is the multi-tenancy key
(see ``omniscience_core.auth.workspace.workspace_filter``).

Both the ingestion worker (issue #126) and the legacy pgvector
migration runner (issue #108) need to derive a non-null
``workspace_id`` BEFORE invoking any Neo4j / Qdrant adapter — the
adapters fail closed on a missing ACL marker.

Rationale for living here (not in ``migration/``):
    The migration package is a one-shot tool kept for legacy installs.
    The ingestion worker uses the resolver on every document.  Reaching
    into a private ``migration.workspace`` from production code would
    couple the live path to the legacy module.  Lifting the helper to
    ``omniscience_index.workspace`` is the smallest change that keeps
    the migration runner working (it re-exports from this module) and
    gives the worker a stable import target.
"""

from __future__ import annotations

import uuid

from omniscience_core.db.models import Source


class MissingWorkspaceError(RuntimeError):
    """Raised when a source cannot be mapped to a workspace.

    Carries the offending ``source_id`` and ``source_name`` so callers
    can produce a fix-it message naming the problem row.  The ingestion
    worker catches this, logs ``workspace_resolution_failed``, and
    surfaces an error result so the broker NAKs the message (and after
    ``max_deliver`` redeliveries the message lands in the DLQ).
    """

    def __init__(self, *, source_id: uuid.UUID, source_name: str) -> None:
        self.source_id = source_id
        self.source_name = source_name
        super().__init__(
            f"Source {source_id} ({source_name!r}) has no tenant_id; backfill "
            "Source.tenant_id (or, for migration only, pass --default-workspace-id)."
        )


def resolve_source_workspace(
    source: Source,
    *,
    default_workspace_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Return the effective ``workspace_id`` for a source row.

    Resolution order:

    1. ``source.tenant_id`` when set — the canonical workspace marker.
    2. ``default_workspace_id`` when provided — the migration-time
       fallback used by ``scripts/migrate_to_hybrid.py``.  Production
       ingestion MUST NOT pass a default; tenant-less sources are a
       data-quality bug that the worker should refuse to write past.
    3. Raise :class:`MissingWorkspaceError`.

    Parameters
    ----------
    source:
        The ORM row for the source being processed.
    default_workspace_id:
        Operator-provided fallback for sources whose ``tenant_id`` is
        null.  Defaults to ``None`` (the production posture: fail
        closed).  Only the legacy migration tool sets this.

    Raises
    ------
    MissingWorkspaceError
        When neither the source nor the operator provides a workspace.
    """
    if source.tenant_id is not None:
        return source.tenant_id
    if default_workspace_id is not None:
        return default_workspace_id
    raise MissingWorkspaceError(source_id=source.id, source_name=source.name)


__all__ = ["MissingWorkspaceError", "resolve_source_workspace"]
