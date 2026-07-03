"""Workspace scoping helpers for multi-tenant isolation.

Usage
-----
``get_workspace_id`` extracts the workspace UUID from an authenticated token
and enforces fail-closed semantics: a token without a workspace_id raises
``PermissionError`` rather than granting access to all data.

``workspace_filter`` applies a strict equality WHERE clause to any SQLAlchemy
SELECT so that results are confined to the caller's workspace.  NULL-tenant
rows are *not* included — the ``0010_backfill_null_tenant`` migration must
run before this version is deployed so that all pre-multitenancy rows carry
the default workspace UUID (``00000000-0000-0000-0000-000000000001``).

Both functions are intentionally simple and dependency-free so they can be
called from any layer (REST handler, background worker, CLI) without pulling
in FastAPI internals.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select

from omniscience_core.db.models import ApiToken

# The canonical default workspace UUID, seeded by migration 0003_workspaces.
# Migration 0010_backfill_null_tenant assigns this to every row whose
# tenant_id/workspace_id was NULL before multi-tenancy was introduced.
DEFAULT_WORKSPACE_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def get_workspace_id(token: ApiToken) -> uuid.UUID:
    """Return the workspace UUID associated with *token*.

    Fail-closed: if the token has no ``workspace_id`` (pre-multitenancy
    token, i.e. ``None``), a ``PermissionError`` is raised with the message
    ``"forbidden:workspace_required"``.  Callers that previously relied on
    ``None`` meaning "no restriction" must be updated to set ``workspace_id``
    on the token before calling this helper.

    This matches the behaviour already in place at:
    - ``apps/server/src/omniscience_server/rest/operator.py`` (403 on None)
    - ``apps/server/src/omniscience_server/mcp/tools.py`` (RuntimeError on None)

    Args:
        token: An authenticated :class:`~omniscience_core.db.models.ApiToken`
               ORM instance.

    Returns:
        The workspace UUID of the token.

    Raises:
        PermissionError: When ``token.workspace_id`` is ``None``.
    """
    if token.workspace_id is None:
        raise PermissionError("forbidden:workspace_required")
    return token.workspace_id


def workspace_filter(query: Select[Any], workspace_id: uuid.UUID) -> Select[Any]:
    """Narrow *query* to rows that belong to *workspace_id*.

    The filter strategy depends on the column present in the query's primary
    entity:

    * If the model has a ``workspace_id`` column, filter with strict equality:
      ``WHERE workspace_id = :workspace_id``.
    * If the model has a ``tenant_id`` column (older tables like ``sources``),
      filter with strict equality: ``WHERE tenant_id = :workspace_id``.
    * If the table is one of the *transitively*-scoped tables (tenant
      isolation only exists via a join back to ``sources.tenant_id``, e.g.
      ``entities``/``edges``/``documents``/``chunks``/``ingestion_runs``),
      fail closed and raise ``PermissionError`` — see
      :data:`_TRANSITIVELY_SCOPED_TABLES`.
    * If neither column is present and the table is not one of the above,
      the query is returned unchanged — the table is genuinely
      tenant-agnostic (e.g. ``workspaces`` itself).

    **No NULL inclusion**: unlike the previous implementation this function
    never adds an ``OR col IS NULL`` clause.  Migration ``0010`` must backfill
    all NULL rows before this version is activated, so there are no legitimate
    legacy rows with NULL tenant_id/workspace_id remaining in production.

    Args:
        query:        Any SQLAlchemy 2 ``Select`` statement.
        workspace_id: UUID of the workspace to restrict results to.
                      Callers must not pass ``None`` — use
                      :func:`get_workspace_id` (which raises on legacy tokens)
                      to obtain a guaranteed non-None UUID.

    Returns:
        The same (or augmented) ``Select`` statement.

    Raises:
        PermissionError: When the query targets a table whose tenant
            isolation is only transitive (via a join to ``sources``) —
            silently returning such a query unfiltered would be a
            cross-tenant data leak. Callers must join to ``Source`` and
            filter on ``Source.tenant_id`` themselves.
    """
    # Determine which column to filter on by inspecting the from clauses.
    # get_final_froms() is the SA 2.x successor to the deprecated .froms attribute.
    entity_cols: dict[str, Any] = {}
    table_name: str | None = None
    for from_clause in query.get_final_froms():
        if hasattr(from_clause, "c"):
            entity_cols = {col.key: col for col in from_clause.c}
            table_name = getattr(from_clause, "name", None)
            break

    if "workspace_id" in entity_cols:
        col = entity_cols["workspace_id"]
        return query.where(col == workspace_id)

    if "tenant_id" in entity_cols:
        col = entity_cols["tenant_id"]
        return query.where(col == workspace_id)

    if table_name in _TRANSITIVELY_SCOPED_TABLES:
        raise PermissionError(
            f"requires_join_scoping: table {table_name!r} carries no direct "
            "tenant_id/workspace_id column; its isolation is only transitive "
            "(via source_id -> sources.tenant_id). workspace_filter() cannot "
            "safely scope it — join to Source and filter on "
            "Source.tenant_id explicitly instead."
        )

    # Table is not workspace-scoped — return as-is.
    return query


# Tables whose tenant isolation is only transitive, via a join back to
# sources.tenant_id (see migration 0010_backfill_null_tenant's table
# survey). workspace_filter() must fail closed for these rather than
# silently returning an unfiltered query.
_TRANSITIVELY_SCOPED_TABLES = frozenset(
    {"entities", "edges", "documents", "chunks", "ingestion_runs"}
)


__all__ = ["DEFAULT_WORKSPACE_ID", "get_workspace_id", "workspace_filter"]
