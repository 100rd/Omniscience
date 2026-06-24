"""Entity graph traversal REST endpoint.

GET /api/v1/entities/{name}/related
    Query parameters:
        max_depth   (int, default 1)  — maximum BFS hops from seed
        edge_types  (list[str])       — comma-separated edge type filter

Requires the ``search`` scope.

Workspace scoping (issue #117)
------------------------------

The caller's ``workspace_id`` is resolved from the authenticated principal
and propagated to the ``GraphStore`` protocol (Neo4j as of v0.2).
A token with no workspace is rejected with
``403 forbidden`` — graph retrieval is fail-closed, never fail-open —
because the protocol layer requires an explicit workspace and we refuse
to invent one on behalf of an unscoped caller.

Dependency injection
--------------------

The handler reads ``request.app.state.graph_store`` — a
``GraphStore`` implementation wired at application startup (see
``omniscience_server.app.create_app``).  The protocol-level
``find_related`` call is semantically identical to the pre-#103
``GraphQueryService.get_related`` call it replaces.

AP1 — single-writer invariant
------------------------------

``merge_entities`` and ``unmerge_entity`` now write an :class:`OutboxEvent`
row to Postgres instead of calling the graph store directly.  The outbox
worker picks it up and routes it through the canonical ``entity.merge`` /
``entity.unmerge`` NATS subjects consumed by :class:`OutboxConsumerWorker`.
This ensures all Neo4j writes flow through the single outbox writer path.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import unquote

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from omniscience_core.auth.middleware import get_current_token, require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.workspace import get_workspace_id
from omniscience_core.db.models import ApiToken, OutboxEvent
from omniscience_core.queue.messages import EntityMergeEvent
from omniscience_core.storage import EntityNodeView, GraphResultView, GraphStore
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from omniscience_server.as_of import (
    DEGRADED_PRE_HISTORY,
    INVALID_TIMEZONE_CODE,
    INVALID_TIMEZONE_MESSAGE,
    enforce_utc,
    record_request_duration,
    resolve_effective_as_of,
)

log = structlog.get_logger(__name__)

router = APIRouter(tags=["entities"])

# Module-level Depends singletons — avoids ruff B008
_search_scope_dep: Any = Depends(require_scope(Scope.search))
_current_token_dep: Any = Depends(get_current_token)

# Query parameter annotations — avoids ruff B008 (no function calls in defaults)
_MaxDepthQuery = Annotated[
    int,
    Query(ge=1, le=10, description="Maximum BFS hops from seed"),
]
_EdgeTypesQuery = Annotated[
    list[str] | None,
    Query(description="Edge types to follow (repeat param for multiple values)"),
]
_AsOfQuery = Annotated[
    datetime | None,
    Query(
        description=(
            "Optional ISO-8601 timezone-aware UTC datetime "
            "(e.g. '2026-04-12T19:25:00Z'). When supplied, returns "
            "the entity / traversal as state was at this time per "
            "ADR-0008 §5 (open-closed [valid_from, valid_to) "
            "interval; valid_to=null means still-current). Naive "
            "or non-UTC datetimes are rejected with 400 "
            "INVALID_TIMEZONE."
        ),
    ),
]


def _validate_as_of(as_of: datetime | None) -> datetime | None:
    """Run the shared UTC enforcement, mapping errors to HTTP 400."""
    try:
        return enforce_utc(as_of)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": INVALID_TIMEZONE_CODE, "message": INVALID_TIMEZONE_MESSAGE},
        ) from exc


@router.get(
    "/entities/{name:path}/related",
    summary="Get related entities by graph traversal",
    dependencies=[_search_scope_dep],
)
async def get_related_entities(
    name: str,
    request: Request,
    max_depth: _MaxDepthQuery = 1,
    edge_types: _EdgeTypesQuery = None,
    as_of: _AsOfQuery = None,
    token: ApiToken = _current_token_dep,
) -> dict[str, Any]:
    """Traverse the entity graph starting from the named entity.

    Returns the seed entity, related entities (up to ``max_depth`` hops),
    and the edges that connect them.  Each entity node includes the
    associated chunk text for context.

    Requires scope: ``search``
    Requires: token scoped to a workspace (fails closed with 403 otherwise).

    ``as_of`` (issue #133, ADR-0008 §5) anchors the traversal to the
    bitemporal state at T.  Unknown-at-T returns 200 with a degraded
    payload + ``meta.degraded_response="as_of_before_recorded_history"``;
    unknown-without-as_of remains 404 (current contract).
    """
    # Path parameters are URL-encoded by FastAPI; decode forward-slashes etc.
    entity_name = unquote(name)
    normalised_as_of = _validate_as_of(as_of)

    graph_store: GraphStore | None = getattr(request.app.state, "graph_store", None)
    if graph_store is None:
        log.warning("graph_store_unavailable")
        raise HTTPException(
            status_code=503,
            detail={"code": "service_unavailable", "message": "Graph store not available"},
        )

    # Resolve the caller's workspace.  A token with no workspace cannot be
    # silently widened to the global graph — that is the exact bug in
    # issue #117.  Fail closed.
    workspace_id = get_workspace_id(token)
    if workspace_id is None:
        log.warning(
            "graph_request_rejected_no_workspace",
            token_prefix=token.token_prefix,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": "Graph retrieval requires a workspace-scoped token",
            },
        )

    with record_request_duration(
        surface="rest",
        tool="get_related_entities",
        as_of=normalised_as_of,
    ) as patcher:
        try:
            result = await graph_store.find_related(
                entity_name=entity_name,
                workspace_id=workspace_id,
                as_of=normalised_as_of,
                max_depth=max_depth,
                edge_types=edge_types if edge_types else None,
            )
        except ValueError as exc:
            msg = str(exc)
            if msg.startswith("entity_not_found:"):
                if normalised_as_of is not None:
                    # Pre-history hint instead of 404 when as_of supplied.
                    patcher.mark_pre_history()
                    return _empty_envelope(
                        entity_name=entity_name,
                        as_of=normalised_as_of,
                    )
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "entity_not_found",
                        "message": f"Entity '{entity_name}' not found",
                    },
                ) from exc
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request", "message": msg},
            ) from exc

        depth_reached = max((n.depth for n in result.related), default=0)
        log.info(
            "entity_graph_traversal",
            entity=entity_name,
            workspace_id=str(workspace_id),
            max_depth=max_depth,
            entities_found=1 + len(result.related),
            edges_traversed=len(result.edges),
            depth_reached=depth_reached,
            as_of=normalised_as_of.isoformat() if normalised_as_of else None,
        )

        payload = _result_to_dict(result)
        payload["effective_as_of"] = resolve_effective_as_of(normalised_as_of).isoformat()
        payload["meta"] = None
        return payload


@router.get(
    "/entities/{name:path}",
    summary="Get a single entity by name",
    dependencies=[_search_scope_dep],
)
async def get_entity(
    name: str,
    request: Request,
    as_of: _AsOfQuery = None,
    token: ApiToken = _current_token_dep,
) -> dict[str, Any]:
    """Resolve a single entity by name within the caller's workspace.

    Issue #133 §C — accepts ``as_of`` query parameter.  Returns the
    still-current state when ``as_of`` is omitted; otherwise the
    ``:EntityState`` row that satisfies ADR-0008 §5's predicate.
    Unknown / pre-history results return 200 with a null entity and
    ``meta.degraded_response="as_of_before_recorded_history"``.
    """
    entity_name = unquote(name)
    normalised_as_of = _validate_as_of(as_of)

    graph_store: GraphStore | None = getattr(request.app.state, "graph_store", None)
    if graph_store is None:
        log.warning("graph_store_unavailable")
        raise HTTPException(
            status_code=503,
            detail={"code": "service_unavailable", "message": "Graph store not available"},
        )

    workspace_id = get_workspace_id(token)
    if workspace_id is None:
        log.warning(
            "get_entity_rejected_no_workspace",
            token_prefix=token.token_prefix,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": "Graph retrieval requires a workspace-scoped token",
            },
        )

    with record_request_duration(
        surface="rest", tool="get_entity", as_of=normalised_as_of
    ) as patcher:
        node: EntityNodeView | None = await graph_store.get_entity(
            entity_name=entity_name,
            workspace_id=workspace_id,
            as_of=normalised_as_of,
        )
        if node is None:
            if normalised_as_of is not None:
                patcher.mark_pre_history()
                return {
                    "entity": None,
                    "effective_as_of": resolve_effective_as_of(normalised_as_of).isoformat(),
                    "meta": {"degraded_response": DEGRADED_PRE_HISTORY},
                }
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "entity_not_found",
                    "message": f"Entity '{entity_name}' not found",
                },
            )
        return {
            "entity": _entity_to_dict(node),
            "effective_as_of": resolve_effective_as_of(normalised_as_of).isoformat(),
            "meta": None,
        }


def _entity_to_dict(node: EntityNodeView) -> dict[str, Any]:
    """Serialise an :class:`EntityNodeView` for the REST wire format."""
    return {
        "name": node.name,
        "kind": node.kind,
        "source": node.source,
        "chunk_text": node.chunk_text,
        "valid_from": node.valid_from.isoformat() if node.valid_from else None,
        "valid_to": node.valid_to.isoformat() if node.valid_to else None,
        "recorded_at": node.recorded_at.isoformat() if node.recorded_at else None,
    }


def _empty_envelope(*, entity_name: str, as_of: datetime) -> dict[str, Any]:
    """Build the pre-history empty-result envelope for /related."""
    return {
        "seed": {
            "name": entity_name,
            "kind": None,
            "source": None,
            "chunk_text": None,
        },
        "related": [],
        "edges": [],
        "stats": {
            "entities_found": 0,
            "edges_traversed": 0,
            "depth_reached": 0,
        },
        "effective_as_of": resolve_effective_as_of(as_of).isoformat(),
        "meta": {"degraded_response": DEGRADED_PRE_HISTORY},
    }


def _result_to_dict(result: GraphResultView) -> dict[str, Any]:
    """Serialise a ``GraphResultView`` to the REST/MCP wire format.

    Matches the legacy ``GraphResult.to_dict`` output byte-for-byte so
    existing API consumers see no change after the #103 protocol
    migration.
    """
    depth_reached = max((n.depth for n in result.related), default=0)
    return {
        "seed": {
            "name": result.seed.name,
            "kind": result.seed.kind,
            "source": result.seed.source,
            "chunk_text": result.seed.chunk_text,
        },
        "related": [
            {
                "name": n.name,
                "kind": n.kind,
                "source": n.source,
                "chunk_text": n.chunk_text,
                "depth": n.depth,
                "edge_type": n.edge_type,
            }
            for n in result.related
        ],
        "edges": [
            {
                "from": e.from_entity,
                "to": e.to_entity,
                "type": e.edge_type,
            }
            for e in result.edges
        ],
        "stats": {
            "entities_found": 1 + len(result.related),
            "edges_traversed": len(result.edges),
            "depth_reached": depth_reached,
        },
    }


class MergeNodesRequest(BaseModel):
    source_id: uuid.UUID
    target_id: uuid.UUID


class UnmergeNodeRequest(BaseModel):
    merged_node_id: uuid.UUID


@router.post(
    "/entities/merge",
    summary="Merge source node into target node reversibly",
    dependencies=[_search_scope_dep],
)
async def merge_entities(
    req: MergeNodesRequest,
    request: Request,
    token: ApiToken = _current_token_dep,
) -> dict[str, Any]:
    """Queue a merge of source node into target node via the outbox.

    AP1 — writes an ``entity.merge`` OutboxEvent instead of writing to
    Neo4j directly.  The response is immediate (``queued: true``); the
    graph is updated within one outbox worker tick (~1 s default).

    Requires scope: ``admin/entities:write``
    """
    workspace_id = get_workspace_id(token)
    if workspace_id is None:
        raise HTTPException(status_code=403, detail="Workspace-scoped token required")
    if "admin/entities:write" not in token.scopes:
        raise HTTPException(status_code=403, detail="Scope admin/entities:write required")

    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "db_session_factory", None
    )
    if factory is None:
        raise HTTPException(status_code=503, detail="db_session_factory not wired")

    merge_payload = EntityMergeEvent(
        workspace_id=str(workspace_id),
        source_id=str(req.source_id),
        target_id=str(req.target_id),
        merged_node_id=None,
    )
    async with factory() as session, session.begin():
        session.add(
            OutboxEvent(
                id=uuid.uuid4(),
                event_type="entity.merge",
                entity_id=req.source_id,
                payload=merge_payload.model_dump(),
            )
        )
    log.info(
        "merge_entities_queued",
        workspace_id=str(workspace_id),
        source_id=str(req.source_id),
        target_id=str(req.target_id),
    )
    return {"success": True, "message": "Merge queued successfully", "queued": True}


@router.post(
    "/entities/unmerge",
    summary="Unmerge/split a previously merged node",
    dependencies=[_search_scope_dep],
)
async def unmerge_entity(
    req: UnmergeNodeRequest,
    request: Request,
    token: ApiToken = _current_token_dep,
) -> dict[str, Any]:
    """Queue an unmerge/split of a previously merged node via the outbox.

    AP1 — writes an ``entity.unmerge`` OutboxEvent instead of writing to
    Neo4j directly.  The response is immediate (``queued: true``); the
    graph is updated within one outbox worker tick (~1 s default).

    AP6 — requires ``admin/entities:write`` scope (parity with merge).

    Requires scope: ``admin/entities:write``
    """
    workspace_id = get_workspace_id(token)
    if workspace_id is None:
        raise HTTPException(status_code=403, detail="Workspace-scoped token required")
    # AP6: admin scope check (matches merge_entities)
    if "admin/entities:write" not in token.scopes:
        raise HTTPException(status_code=403, detail="Scope admin/entities:write required")

    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "db_session_factory", None
    )
    if factory is None:
        raise HTTPException(status_code=503, detail="db_session_factory not wired")

    unmerge_payload = EntityMergeEvent(
        workspace_id=str(workspace_id),
        source_id=None,
        target_id=None,
        merged_node_id=str(req.merged_node_id),
    )
    async with factory() as session, session.begin():
        session.add(
            OutboxEvent(
                id=uuid.uuid4(),
                event_type="entity.unmerge",
                entity_id=req.merged_node_id,
                payload=unmerge_payload.model_dump(),
            )
        )
    log.info(
        "unmerge_entity_queued",
        workspace_id=str(workspace_id),
        merged_node_id=str(req.merged_node_id),
    )
    return {"success": True, "message": "Unmerge queued successfully", "queued": True}


__all__ = ["router"]
