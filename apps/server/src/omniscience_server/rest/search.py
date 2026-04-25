"""POST /api/v1/search — search over the knowledge base.

Accepts a SearchRequest body, routes through the GraphRAG composer
(Neo4j + Qdrant, ADR-0005/0006) when the caller's token carries a
workspace, and returns a SearchResult.  Requires the ``search`` scope.

Tokens without a workspace receive 503 — the legacy pgvector
``RetrievalService`` was removed at the #105 cutover, and non-workspace
retrieval has no supported default backend.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from omniscience_core.auth.middleware import get_current_token, require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.workspace import get_workspace_id
from omniscience_core.db.models import ApiToken
from omniscience_retrieval.graph_rag import GraphRAGComposer
from omniscience_retrieval.models import SearchRequest, SearchResult

from omniscience_server.rest.rate_limit import rate_limit_dependency

log = structlog.get_logger(__name__)

router = APIRouter(tags=["search"])

# Module-level Depends singletons — avoids ruff B008
_search_scope_dep: Any = Depends(require_scope(Scope.search))
_rate_limit_dep: Any = Depends(rate_limit_dependency)
_current_token_dep: Any = Depends(get_current_token)


@router.post(
    "/search",
    response_model=SearchResult,
    summary="Hybrid semantic + keyword search",
    dependencies=[_search_scope_dep, _rate_limit_dep],
)
async def search(
    body: SearchRequest,
    request: Request,
    token: ApiToken = _current_token_dep,
) -> SearchResult:
    """Execute a search query against indexed knowledge.

    Body mirrors the MCP ``search`` tool input.  Response mirrors the MCP
    ``search`` tool output.

    Requires scope: ``search``.

    Workspace-scoped tokens route through the :class:`GraphRAGComposer`
    (Neo4j + Qdrant, ADR-0005/0006).  Tokens without a workspace fall
    back to ``app.state.retrieval_service`` when operators have wired
    a custom shim; by default that attribute is ``None`` and such
    requests return 503.
    """
    composer: GraphRAGComposer | None = getattr(request.app.state, "graph_rag_composer", None)
    retrieval_service = getattr(request.app.state, "retrieval_service", None)
    workspace_id = get_workspace_id(token)

    log.info(
        "search_request",
        query_len=len(body.query),
        top_k=body.top_k,
        strategy=body.retrieval_strategy,
        workspace_scoped=workspace_id is not None,
    )

    if composer is not None and workspace_id is not None:
        result: SearchResult = await composer.search(body, workspace_id=workspace_id)
    elif retrieval_service is not None:
        result = await retrieval_service.search(body)
    else:
        log.warning("retrieval_service_unavailable")
        raise HTTPException(
            status_code=503,
            detail={"code": "service_unavailable", "message": "Retrieval service not configured"},
        )

    log.info(
        "search_response",
        hits=len(result.hits),
        duration_ms=result.query_stats.duration_ms,
    )
    return result


__all__ = ["router"]
