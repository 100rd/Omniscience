"""Main API v1 router — aggregates all sub-routers under /api/v1."""

from __future__ import annotations

from fastapi import APIRouter

from omniscience_server.rest.documents import router as documents_router
from omniscience_server.rest.freshness import router as freshness_router
from omniscience_server.rest.ingestion_runs import router as ingestion_runs_router
from omniscience_server.rest.search import router as search_router
from omniscience_server.rest.sources import router as sources_router
from omniscience_server.rest.webhooks import router as webhooks_router

api_v1_router = APIRouter(prefix="/api/v1")

api_v1_router.include_router(search_router)
api_v1_router.include_router(sources_router)
api_v1_router.include_router(documents_router)
api_v1_router.include_router(ingestion_runs_router)
api_v1_router.include_router(webhooks_router)
api_v1_router.include_router(freshness_router)

__all__ = ["api_v1_router"]
