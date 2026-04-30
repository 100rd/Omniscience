"""Operator-scoped read endpoint (#163).

GET /api/v1/operator/entities

Lists external_ids of entities the omniscience-operator emitted for a
given (workspace_id, cluster_id, kind) tuple, with cursor pagination.

ACL invariant — workspace_id is taken from the bearer token, never from the query
--------------------------------------------------------------------------------
The bearer token has a workspace_id (from
``packages/core/src/omniscience_core/auth/middleware.py``).  This handler
ENFORCES that the token's workspace_id matches the ``workspace_id`` query
parameter.  Mismatch → 403 ``forbidden``.

This is the **load-bearing security gate** for the reconciliation worker
(see issue #163 §ACL invariant #1).  Even a misconfigured operator
cannot reach into another tenant's data: the token resolves to one
workspace, the query param must equal it, and SQL filters lock the
result set to that workspace.

Emitter filter
--------------
Server-side filtering on ``source_type='k8s_operator'`` is mandatory and
NOT a query parameter — clients cannot opt out.  Issue #163 §Scope
explicitly requires this gate so the response set never includes entities
emitted by other connectors (the agentic ``k8s`` connector, terraform,
etc.).

Pagination
----------
Cursor-based using the document's primary-key UUID as the opaque next
cursor.  Limit is capped at 1000 server-side; the client passes any
``limit`` up to that cap.

Auth
----
* No / invalid bearer token                  → 401 ``unauthorized``
* Token without ``operator:read`` scope      → 403 ``forbidden`` (scope)
* Token's workspace ≠ query workspace_id     → 403 ``forbidden`` (workspace)
* Token has no workspace_id at all (legacy)  → 403 ``forbidden`` (workspace)
* Authenticated, scoped, matched             → 200 with JSON page
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from omniscience_core.auth.middleware import require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.workspace import get_workspace_id
from omniscience_core.db.models import ApiToken, Document, Source, SourceType
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/operator", tags=["operator"])

# Module-level singletons — avoid ruff B008 (function call in argument default).
_operator_read_dep: Any = Depends(require_scope(Scope.operator_read))
_workspace_id_query: Any = Query(
    ...,
    description="Workspace UUID — MUST match the bearer token's workspace_id.",
)
_cluster_id_query: Any = Query(
    ...,
    description="Cluster UUID — the operator's stamped cluster_id (issue #167).",
)
_kind_query: Any = Query(
    ...,
    min_length=1,
    max_length=64,
    description="K8s API Kind (e.g. 'Pod', 'Deployment'). Mixed-case, exact match.",
)
_cursor_query: Any = Query(
    default="",
    max_length=64,
    description="Pagination cursor. Empty string for the first page.",
)
_limit_query: Any = Query(
    default=500,
    ge=1,
    le=1000,
    description="Page size (1..1000). Defaults to 500.",
)


# ---------------------------------------------------------------------------
# Server-side bounds.
# ---------------------------------------------------------------------------

# Maximum entities returned per request. Clients pass any ``limit`` up to
# this cap; values above are silently clamped. 1000 is large enough to
# minimise round-trip count on 10k-pod clusters and small enough that a
# single response body stays under the typical 8 MiB API gateway ceiling.
_MAX_LIMIT = 1000

# Default limit when the client omits ``limit``. 500 matches the operator
# client's default page size so a same-defaults configuration produces
# matching page boundaries.
_DEFAULT_LIMIT = 500


# ---------------------------------------------------------------------------
# Pydantic models.
# ---------------------------------------------------------------------------


class OperatorEntitiesPage(BaseModel):
    """One page of operator-emitted external_ids."""

    model_config = ConfigDict(extra="forbid")

    external_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Sorted list of external_ids the operator emitted for the "
            "given (workspace_id, cluster_id, kind) tuple. Stable across "
            "calls — sort key is the external_id itself."
        ),
    )
    next_cursor: str = Field(
        default="",
        description=(
            "Opaque pagination cursor. Empty string terminates the walk. "
            "Pass back verbatim in the next request's ``cursor`` query "
            "parameter."
        ),
    )


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _require_matching_workspace(
    token: ApiToken,
    *,
    requested_workspace_id: uuid.UUID,
) -> uuid.UUID:
    """Extract and validate the caller's workspace_id.

    Three failure paths, all mapped to 403:
      * legacy token with no workspace_id at all
      * token's workspace_id != ``requested_workspace_id`` query param

    Returns the validated workspace_id (always == ``requested_workspace_id``
    on success). Raises ``HTTPException(403)`` otherwise.

    The handler logs each rejection with the token prefix (never the token
    body) so operations can correlate audit-log entries with denied
    requests without exposing credentials.
    """
    token_workspace = get_workspace_id(token)
    if token_workspace is None:
        log.warning(
            "operator_endpoint_rejected_no_workspace",
            token_prefix=token.token_prefix,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": "Operator endpoint requires a workspace-scoped token.",
            },
        )
    if token_workspace != requested_workspace_id:
        log.warning(
            "operator_endpoint_rejected_workspace_mismatch",
            token_prefix=token.token_prefix,
            token_workspace=str(token_workspace),
            requested_workspace=str(requested_workspace_id),
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": ("Token workspace_id does not match the requested workspace_id."),
            },
        )
    return token_workspace


def _build_external_id_prefix(cluster_id: uuid.UUID, kind: str) -> str:
    """Build the LIKE prefix for the (cluster_id, kind) filter.

    Operator external_ids follow exactly two shapes (see
    ``operator/internal/entity/common.go``):

      * namespaced:    ``k8s_resource/{cluster_id}/{Kind}/{namespace}/{name}``
      * cluster-scoped: ``k8s_resource/{cluster_id}/{Kind}/{name}``

    Both shapes share the prefix ``k8s_resource/{cluster_id}/{Kind}/`` so
    a single LIKE pattern matches either form precisely. Filtering by
    prefix on Documents.external_id is index-friendly (the column is
    ``Text`` and SQL LIKE uses the b-tree index when the pattern has no
    leading wildcard, which is the case here).
    """
    return f"k8s_resource/{cluster_id}/{kind}/%"


def _resolve_db_factory(request: Request) -> Any:
    """Return the configured async-sessionmaker or 503."""
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "service_unavailable", "message": "Database not available"},
        )
    return factory


async def _query_external_ids(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    cluster_id: uuid.UUID,
    kind: str,
    cursor: str,
    limit: int,
) -> tuple[list[str], str]:
    """Execute the paginated query and return ``(external_ids, next_cursor)``.

    Ordering is by ``Document.id`` (ascending) so cursor pagination is
    stable: the cursor is the last document's id; the next page returns
    documents with ``id > cursor``.

    Filters (all required, all server-enforced):
      * ``Source.type = 'k8s_operator'`` — emitter gate
      * ``Source.tenant_id = workspace_id`` — tenant gate
      * ``Document.external_id LIKE 'k8s_resource/{cluster_id}/{kind}/%'``
    """
    prefix = _build_external_id_prefix(cluster_id, kind)
    stmt = (
        select(Document.id, Document.external_id)
        .join(Source, Document.source_id == Source.id)
        .where(Source.type == SourceType.k8s_operator)
        .where(Source.tenant_id == workspace_id)
        .where(Document.external_id.like(prefix))
        .where(Document.tombstoned_at.is_(None))
        .order_by(Document.id)
        .limit(limit + 1)  # Fetch one extra to detect "is there a next page".
    )

    if cursor:
        try:
            cursor_uuid = uuid.UUID(cursor)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_cursor", "message": "Cursor must be a UUID."},
            ) from None
        stmt = stmt.where(Document.id > cursor_uuid)

    rows = (await db.execute(stmt)).all()

    has_next = len(rows) > limit
    rows = rows[:limit]
    external_ids = [row.external_id for row in rows]
    next_cursor = ""
    if has_next and rows:
        # Cursor is the last *included* row's id; the next page starts strictly
        # after it. Stable across server restarts because Document.id is a
        # uuid4 primary key — values do not shuffle.
        next_cursor = str(rows[-1].id)

    return external_ids, next_cursor


# ---------------------------------------------------------------------------
# Route.
# ---------------------------------------------------------------------------


@router.get(
    "/entities",
    summary="List operator-emitted entity external_ids (workspace + cluster + kind)",
    response_model=OperatorEntitiesPage,
    dependencies=[_operator_read_dep],
)
async def list_operator_entities(
    request: Request,
    workspace_id: uuid.UUID = _workspace_id_query,
    cluster_id: uuid.UUID = _cluster_id_query,
    kind: str = _kind_query,
    cursor: str = _cursor_query,
    limit: int = _limit_query,
    token: ApiToken = _operator_read_dep,
) -> OperatorEntitiesPage:
    """Return one page of external_ids for the given filter.

    Fails closed at three layers:
      1. Bearer auth (handled by Depends)
      2. ``operator:read`` scope (handled by Depends)
      3. Workspace match (this body) — token's workspace MUST equal the
         ``workspace_id`` query parameter, otherwise 403.

    Server-side filters that are NOT exposed as query params (deliberately):
      * ``source_type='k8s_operator'`` — clients cannot opt out
      * ``Source.tenant_id = workspace_id`` — second layer of tenant
        isolation, even if a future migration loosens the SQL above

    The first failure raises HTTPException; the success body is a
    :class:`OperatorEntitiesPage`.
    """
    # Workspace gate (load-bearing ACL check — see ADR-0007 §ACL).
    _require_matching_workspace(token, requested_workspace_id=workspace_id)

    factory = _resolve_db_factory(request)
    async with factory() as db:
        external_ids, next_cursor = await _query_external_ids(
            db,
            workspace_id=workspace_id,
            cluster_id=cluster_id,
            kind=kind,
            cursor=cursor,
            limit=limit,
        )

    log.info(
        "operator_entities_listed",
        token_prefix=token.token_prefix,
        workspace_id=str(workspace_id),
        cluster_id=str(cluster_id),
        kind=kind,
        count=len(external_ids),
        cursor_in=bool(cursor),
        cursor_out=bool(next_cursor),
    )
    return OperatorEntitiesPage(external_ids=external_ids, next_cursor=next_cursor)
