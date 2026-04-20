"""Entity graph traversal REST endpoint.

GET /api/v1/entities/{name}/related
    Query parameters:
        max_depth   (int, default 1)  — maximum BFS hops from seed
        edge_types  (list[str])       — comma-separated edge type filter

Requires the ``search`` scope.
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import unquote

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from omniscience_core.auth.middleware import require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_retrieval.graph_query import GraphQueryService

log = structlog.get_logger(__name__)

router = APIRouter(tags=["entities"])

# Module-level Depends singleton — avoids ruff B008
_search_scope_dep: Any = Depends(require_scope(Scope.search))

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
) -> dict[str, Any]:
    """Traverse the entity graph starting from the named entity.

    Returns the seed entity, related entities (up to ``max_depth`` hops),
    and the edges that connect them.  Each entity node includes the
    associated chunk text for context.

    Requires scope: ``search``
    """
    # Path parameters are URL-encoded by FastAPI; decode forward-slashes etc.
    entity_name = unquote(name)

    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        log.warning("db_session_factory_unavailable")
        raise HTTPException(
            status_code=503,
            detail={"code": "service_unavailable", "message": "Database not available"},
        )

    service = GraphQueryService(factory)

    try:
        result = await service.get_related(
            entity_name=entity_name,
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

    log.info(
        "entity_graph_traversal",
        entity=entity_name,
        max_depth=max_depth,
        entities_found=result.stats["entities_found"],
        edges_traversed=result.stats["edges_traversed"],
    )

    return result.to_dict()


__all__ = ["router"]
