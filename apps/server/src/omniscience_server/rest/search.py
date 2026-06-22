"""POST /api/v1/search — search over the knowledge base.

Accepts a SearchRequest body, routes through the GraphRAG composer
(Neo4j + Qdrant, ADR-0005/0006) when the caller's token carries a
workspace, and returns a SearchResult.  Requires the ``search`` scope.

Tokens without a workspace receive 503 — non-workspace
retrieval has no supported default backend.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from omniscience_core.auth.middleware import get_current_token, require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.workspace import get_workspace_id
from omniscience_core.db.models import ApiToken
from omniscience_retrieval.graph_rag import GraphRAGComposer
from omniscience_retrieval.models import SearchRequest, SearchResult

from omniscience_server.as_of import (
    INVALID_TIMEZONE_CODE,
    INVALID_TIMEZONE_MESSAGE,
    enforce_utc,
    record_request_duration,
    resolve_effective_as_of,
)
from omniscience_server.rest.rate_limit import rate_limit_dependency

log = structlog.get_logger(__name__)

router = APIRouter(tags=["search"])

# Module-level Depends singletons — avoids ruff B008
_search_scope_dep: Any = Depends(require_scope(Scope.search))
_rate_limit_dep: Any = Depends(rate_limit_dependency)
_current_token_dep: Any = Depends(get_current_token)

#: ``as_of`` query-string parameter — issue #133.  Documented on the
#: route with the boundary-semantics call-out citing ADR-0008 §5.
_AsOfQuery = Annotated[
    datetime | None,
    Query(
        description=(
            "Optional ISO-8601 timezone-aware UTC datetime "
            "(e.g. '2026-04-12T19:25:00Z'). When supplied, returns "
            "the search result as state was at this time per "
            "ADR-0008 §5 (open-closed [valid_from, valid_to) "
            "interval; valid_to=null means still-current). Naive "
            "or non-UTC datetimes are rejected with 400 "
            "INVALID_TIMEZONE."
        ),
    ),
]


@router.post(
    "/search",
    response_model=SearchResult,
    summary="Hybrid semantic + keyword search",
    dependencies=[_search_scope_dep, _rate_limit_dep],
)
async def search(
    body: SearchRequest,
    request: Request,
    as_of: _AsOfQuery = None,
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

    ``as_of`` (issue #133, ADR-0008 §5) is accepted both as a
    query-string parameter and inside the request body; the
    query-string value wins when both are present.  Naive or non-UTC
    values raise 400 ``INVALID_TIMEZONE``.
    """
    try:
        normalised_as_of = enforce_utc(as_of) if as_of is not None else body.as_of
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": INVALID_TIMEZONE_CODE, "message": INVALID_TIMEZONE_MESSAGE},
        ) from exc

    composer: GraphRAGComposer | None = getattr(request.app.state, "graph_rag_composer", None)
    retrieval_service = getattr(request.app.state, "retrieval_service", None)
    workspace_id = get_workspace_id(token)

    log.info(
        "search_request",
        query_len=len(body.query),
        top_k=body.top_k,
        strategy=body.retrieval_strategy,
        workspace_scoped=workspace_id is not None,
        as_of=normalised_as_of.isoformat() if normalised_as_of else None,
    )

    with record_request_duration(surface="rest", tool="search", as_of=normalised_as_of) as patcher:
        if composer is not None and workspace_id is not None:
            result: SearchResult = await composer.search(
                body,
                workspace_id=workspace_id,
                as_of=normalised_as_of,
            )
        elif retrieval_service is not None:
            result = await retrieval_service.search(body)
            # Legacy path doesn't honour as_of; stamp envelope for parity.
            if result.effective_as_of is None:
                result = result.model_copy(
                    update={
                        "effective_as_of": resolve_effective_as_of(normalised_as_of),
                    }
                )
        else:
            log.warning("retrieval_service_unavailable")
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "service_unavailable",
                    "message": "Retrieval service not configured",
                },
            )

        if (
            normalised_as_of is not None
            and result.meta is not None
            and result.meta.get("degraded_response") == "as_of_before_recorded_history"
        ):
            patcher.mark_pre_history()

    log.info(
        "search_response",
        hits=len(result.hits),
        duration_ms=result.query_stats.duration_ms,
    )
    return result


__all__ = ["router"]
