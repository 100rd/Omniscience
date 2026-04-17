"""FastAPI application factory.

Create the ASGI application by calling ``create_app()``.  The factory:
  - reads Settings from the environment
  - configures structured logging
  - initialises OpenTelemetry
  - connects to NATS JetStream and ensures streams are provisioned
  - initialises the ingestion worker (placeholder — not consuming yet)
  - mounts the Prometheus metrics ASGI app at /metrics
  - mounts the MCP ASGI app at /mcp (streamable-http transport)
  - adds TracingMiddleware
  - registers all route groups
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlparse, urlunparse

import structlog
from fastapi import FastAPI
from omniscience_core.config import Settings
from omniscience_core.db import create_async_engine, create_session_factory
from omniscience_core.logging import configure_logging
from omniscience_core.queue import NatsConnection, ensure_streams
from omniscience_core.telemetry import init_telemetry
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import Response

from omniscience_server.mcp.mount import create_mcp_asgi_app
from omniscience_server.middleware import TracingMiddleware
from omniscience_server.rest import api_v1_router, register_error_handlers
from omniscience_server.routes import health_router, tokens_router

log = structlog.get_logger(__name__)


def _redact_url(url: str) -> str:
    """Strip credentials from a URL for safe logging."""
    parsed = urlparse(url)
    if parsed.password:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        redacted = parsed._replace(netloc=f"{parsed.username}:***@{host}")
        return urlunparse(redacted)
    return url


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: startup tasks -> yield -> shutdown tasks."""
    settings: Settings = app.state.settings

    configure_logging(settings.log_level)
    init_telemetry(settings)

    log.info(
        "startup",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
    )

    # --- Postgres connection ---
    engine = create_async_engine(settings)
    session_factory = create_session_factory(engine)
    app.state.db_engine = engine
    app.state.db_session_factory = session_factory
    log.info("postgres_connected", url=_redact_url(settings.database_url))

    # --- NATS JetStream connection ---
    nats_conn = NatsConnection()
    await nats_conn.connect(settings)
    await ensure_streams(nats_conn.jetstream)
    app.state.nats = nats_conn

    # --- Ingestion worker (placeholder — not consuming yet) ---
    # TODO(issue-6): Wire real connector registry, embedding provider, index writer,
    # and session factory here, then call ``asyncio.create_task(worker.start())``.
    # The worker is intentionally not started until all dependencies are available.
    log.info("ingestion_worker_placeholder", status="not_started")
    app.state.ingestion_worker = None

    yield

    # --- Shutdown ---
    log.info("shutdown", app=settings.app_name)
    await engine.dispose()
    await nats_conn.disconnect()


async def _metrics_endpoint(request: Request) -> Response:
    """Serve Prometheus metrics in the standard exposition format.

    Mounted as a plain ASGI route so that Prometheus scrapes are not counted
    in the request-latency histograms tracked by TracingMiddleware.
    """
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct and configure the FastAPI application.

    Args:
        settings: Optional Settings instance.  When ``None`` a new instance
                  is created from the environment (normal production path).

    Returns:
        A fully configured FastAPI application ready for ``uvicorn.run()``.
    """
    resolved = settings or Settings()

    # Disable Swagger UI in production; enable in dev/test
    is_dev = str(resolved.environment).lower() in ("development", "dev", "test")

    app = FastAPI(
        title="Omniscience",
        description="Self-hosted knowledge retrieval service with MCP-first API",
        version=resolved.app_version,
        lifespan=_lifespan,
        # OpenAPI served at /api/v1/openapi.json; UI only in dev
        docs_url="/api/docs" if is_dev else None,
        redoc_url="/api/redoc" if is_dev else None,
        openapi_url="/api/v1/openapi.json",
    )
    app.state.settings = resolved

    # Metrics endpoint - mounted before middleware so scrapes don't hit TracingMiddleware
    app.add_route("/metrics", _metrics_endpoint, include_in_schema=False)

    # MCP streamable-http endpoint
    app.mount("/mcp", create_mcp_asgi_app(app))

    # Middleware (applied in reverse registration order by Starlette)
    app.add_middleware(TracingMiddleware)

    # Exception handlers for spec-compliant error responses
    register_error_handlers(app)

    # Routers
    app.include_router(health_router)
    app.include_router(tokens_router)
    app.include_router(api_v1_router)

    return app
