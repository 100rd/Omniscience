"""Main API v1 router — aggregates all sub-routers under /api/v1."""

from __future__ import annotations

from fastapi import APIRouter

from omniscience_server.rest.documents import router as documents_router
from omniscience_server.rest.entities import router as entities_router
from omniscience_server.rest.freshness import router as freshness_router
from omniscience_server.rest.incident_timeline import router as incident_timeline_router
from omniscience_server.rest.incidents import router as incidents_router
from omniscience_server.rest.incidents_admin import router as incidents_admin_router
from omniscience_server.rest.ingestion_runs import router as ingestion_runs_router
from omniscience_server.rest.operator import router as operator_router
from omniscience_server.rest.otlp import router as otlp_router
from omniscience_server.rest.retention import router as retention_router
from omniscience_server.rest.search import router as search_router
from omniscience_server.rest.sources import router as sources_router
from omniscience_server.rest.stats import router as stats_router
from omniscience_server.rest.webhooks import router as webhooks_router

api_v1_router = APIRouter(prefix="/api/v1")

api_v1_router.include_router(search_router)
api_v1_router.include_router(sources_router)
api_v1_router.include_router(documents_router)
api_v1_router.include_router(entities_router)
api_v1_router.include_router(ingestion_runs_router)
api_v1_router.include_router(webhooks_router)
api_v1_router.include_router(freshness_router)
api_v1_router.include_router(stats_router)
api_v1_router.include_router(retention_router)
api_v1_router.include_router(otlp_router)
api_v1_router.include_router(operator_router)
api_v1_router.include_router(incidents_router)
api_v1_router.include_router(incident_timeline_router)
api_v1_router.include_router(incidents_admin_router)

__all__ = ["api_v1_router"]
