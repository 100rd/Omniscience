"""Workspace-id resolution for the pgvector -> hybrid migration.

The pgvector schema does not carry a ``workspace_id`` column on
``sources`` / ``documents`` / ``chunks`` / ``entities`` / ``edges``
today (``Source.tenant_id`` is the de-facto multi-tenancy key — see
``omniscience_core.auth.workspace.workspace_filter`` for the canonical
lookup).

On migration we resolve each source's effective ``workspace_id``:

1.  If ``Source.tenant_id`` is set, use it verbatim.
2.  Otherwise, if the caller supplied ``--default-workspace-id UUID``,
    use that.
3.  Otherwise, raise :class:`MissingWorkspaceError` with the offending
    source's id + name.  No row is written without an explicit
    workspace binding — this is the migration-level ACL gate (#108).

The resolver is extracted so unit tests can cover the fall-through
matrix exhaustively without going through the runner.
"""

from __future__ import annotations

import uuid

from omniscience_core.db.models import Source


class MissingWorkspaceError(RuntimeError):
    """Raised when a pgvector source cannot be mapped to a workspace.

    Carries the offending ``source_id`` and ``source_name`` so callers
    can print a fix-it message naming the problem row.
    """

    def __init__(self, *, source_id: uuid.UUID, source_name: str) -> None:
        self.source_id = source_id
        self.source_name = source_name
        super().__init__(
            f"Source {source_id} ({source_name!r}) has no tenant_id; pass "
            "--default-workspace-id to bind legacy sources to a workspace, or "
            "backfill Source.tenant_id first."
        )


def resolve_source_workspace(
    source: Source,
    *,
    default_workspace_id: uuid.UUID | None,
) -> uuid.UUID:
    """Return the effective ``workspace_id`` for a pgvector source.

    Parameters
    ----------
    source:
        The ORM row for the source being migrated.
    default_workspace_id:
        Operator-provided fallback for sources whose ``tenant_id`` is
        null.  When ``None`` and ``source.tenant_id`` is also ``None``,
        this function raises :class:`MissingWorkspaceError`.

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
