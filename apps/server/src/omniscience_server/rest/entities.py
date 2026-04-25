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
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import unquote

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from omniscience_core.auth.middleware import get_current_token, require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.workspace import get_workspace_id
from omniscience_core.db.models import ApiToken
from omniscience_core.storage import GraphResultView, GraphStore

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
    token: ApiToken = _current_token_dep,
) -> dict[str, Any]:
    """Traverse the entity graph starting from the named entity.

    Returns the seed entity, related entities (up to ``max_depth`` hops),
    and the edges that connect them.  Each entity node includes the
    associated chunk text for context.

    Requires scope: ``search``
    Requires: token scoped to a workspace (fails closed with 403 otherwise).
    """
    # Path parameters are URL-encoded by FastAPI; decode forward-slashes etc.
    entity_name = unquote(name)

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

    try:
        result = await graph_store.find_related(
            entity_name=entity_name,
            workspace_id=workspace_id,
            max_depth=max_depth,
            edge_types=edge_types if edge_types else None,
        )
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith("entity_not_found:"):
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
    )

    return _result_to_dict(result)


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


__all__ = ["router"]
