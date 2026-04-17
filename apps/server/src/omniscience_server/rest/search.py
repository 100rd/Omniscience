"""POST /api/v1/search — hybrid search over the knowledge base.

Accepts a SearchRequest body, delegates to RetrievalService, and returns
a SearchResult.  Requires the ``search`` scope.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from omniscience_core.auth.middleware import require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_retrieval.models import SearchRequest, SearchResult

from omniscience_server.rest.rate_limit import rate_limit_dependency

log = structlog.get_logger(__name__)

router = APIRouter(tags=["search"])

# Module-level Depends singletons — avoids ruff B008
_search_scope_dep: Any = Depends(require_scope(Scope.search))
_rate_limit_dep: Any = Depends(rate_limit_dependency)


@router.post(
    "/search",
    response_model=SearchResult,
    summary="Hybrid semantic + keyword search",
    dependencies=[_search_scope_dep, _rate_limit_dep],
)
async def search(
    body: SearchRequest,
    request: Request,
) -> SearchResult:
    """Execute a hybrid search query against indexed knowledge.

    Body mirrors the MCP ``search`` tool input.  Response mirrors the MCP
    ``search`` tool output.

    Requires scope: ``search``
    """
    retrieval_service = getattr(request.app.state, "retrieval_service", None)
    if retrieval_service is None:
        log.warning("retrieval_service_unavailable")
        raise HTTPException(
            status_code=503,
            detail={"code": "service_unavailable", "message": "Retrieval service not configured"},
        )

    log.info(
        "search_request",
        query_len=len(body.query),
        top_k=body.top_k,
        strategy=body.retrieval_strategy,
    )

    result: SearchResult = await retrieval_service.search(body)

    log.info(
        "search_response",
        hits=len(result.hits),
        duration_ms=result.query_stats.duration_ms,
    )
    return result


__all__ = ["router"]
