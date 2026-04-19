"""Workspace scoping helpers for multi-tenant isolation.

Usage
-----
``get_workspace_id`` extracts the workspace UUID from an authenticated token.
``workspace_filter`` applies a WHERE clause to any SQLAlchemy SELECT so that
results are confined to the caller's workspace.

Both functions are intentionally simple and dependency-free so they can be
called from any layer (REST handler, background worker, CLI) without pulling
in FastAPI internals.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, null, or_

from omniscience_core.db.models import ApiToken


def get_workspace_id(token: ApiToken) -> uuid.UUID | None:
    """Return the workspace UUID associated with *token*, or ``None``.

    ``None`` means the token predates workspace scoping and should be treated
    as belonging to the global / default workspace (i.e. no filtering applied
    beyond what existed before multi-tenancy).

    Args:
        token: An authenticated :class:`~omniscience_core.db.models.ApiToken`
               ORM instance.

    Returns:
        The workspace UUID if the token has one set, otherwise ``None``.
    """
    return token.workspace_id


def workspace_filter(query: Select[Any], workspace_id: uuid.UUID | None) -> Select[Any]:
    """Narrow *query* to rows that belong to *workspace_id*.

    The filter strategy depends on the column present in the query's primary
    entity:

    * If the model has a ``workspace_id`` column, filter directly on it.
      ``NULL`` workspace_id rows (legacy data) are always included so that
      existing records remain visible after the migration.
    * If the model has a ``tenant_id`` column (older tables like ``sources``),
      cast *workspace_id* to the ``tenant_id`` match.  Again, ``NULL``
      tenant_id rows are included for backward compatibility.
    * If neither column is present, the query is returned unchanged — the
      table is not workspace-scoped (e.g. ``workspaces`` itself).

    When *workspace_id* is ``None`` (legacy token with no workspace) the query
    is returned unchanged so that all records remain accessible — this
    preserves backward compatibility for tokens created before this migration.

    Args:
        query:        Any SQLAlchemy 2 ``Select`` statement.
        workspace_id: UUID of the workspace to restrict results to, or
                      ``None`` to skip filtering entirely.

    Returns:
        The same (or augmented) ``Select`` statement.
    """
    if workspace_id is None:
        # Legacy token — no workspace restriction.
        return query

    # Determine which column to filter on by inspecting the from clauses.
    # get_final_froms() is the SA 2.x successor to the deprecated .froms attribute.
    entity_cols: dict[str, Any] = {}
    for from_clause in query.get_final_froms():
        if hasattr(from_clause, "c"):
            entity_cols = {col.key: col for col in from_clause.c}
            break

    if "workspace_id" in entity_cols:
        col = entity_cols["workspace_id"]
        return query.where(or_(col == workspace_id, col == null()))

    if "tenant_id" in entity_cols:
        col = entity_cols["tenant_id"]
        return query.where(or_(col == workspace_id, col == null()))

    # Table is not workspace-scoped — return as-is.
    return query


__all__ = ["get_workspace_id", "workspace_filter"]
