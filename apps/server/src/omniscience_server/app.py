"""FastAPI application factory.

Create the ASGI application by calling ``create_app()``.  The factory:
  - reads Settings from the environment
  - configures structured logging
  - initialises OpenTelemetry
  - connects to Postgres, NATS JetStream, embedding provider
  - starts the ingestion worker (consumes document change events)
  - mounts the Prometheus metrics ASGI app at /metrics
  - mounts the MCP ASGI app at /mcp (streamable-http transport)
  - adds TracingMiddleware
  - registers all route groups
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlparse, urlunparse

import structlog
from fastapi import FastAPI
from omniscience_connectors import default_registry as connector_registry
from omniscience_core.config import Settings
from omniscience_core.db import create_async_engine, create_session_factory
from omniscience_core.logging import configure_logging
from omniscience_core.queue import NatsConnection, ensure_streams
from omniscience_core.queue.consumer import QueueConsumer
from omniscience_core.telemetry import init_telemetry
from omniscience_embeddings import create_embedding_provider
from omniscience_index import IndexWriter
from omniscience_retrieval import RetrievalService
from omniscience_retrieval.federation import FederatedSearch
from omniscience_retrieval.federation_config import FederationConfig
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import Response

from omniscience_server.ingestion.events import DocumentChangeEvent
from omniscience_server.ingestion.worker import IngestionWorker
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

    # --- Embedding provider ---
    embedding_provider = create_embedding_provider(settings)
    app.state.embedding_provider = embedding_provider
    log.info(
        "embedding_provider_ready",
        provider=embedding_provider.provider_name,
        model=embedding_provider.model_name,
        dim=embedding_provider.dim,
    )

    # --- Retrieval service ---
    local_retrieval = RetrievalService(
        session_factory=session_factory,
        embedding_provider=embedding_provider,
    )

    # --- Federation (optional) ---
    if settings.federation_enabled:
        fed_config = FederationConfig.from_json(settings.federation_instances)
        fed_config = fed_config.model_copy(
            update={"timeout_seconds": float(settings.federation_timeout_seconds)}
        )
        retrieval_service: RetrievalService | FederatedSearch = FederatedSearch(
            local_service=local_retrieval,
            config=fed_config,
        )
        app.state.federated_search = retrieval_service
        log.info(
            "federation_enabled",
            peers=len(fed_config.enabled_instances),
            timeout_s=fed_config.timeout_seconds,
        )
    else:
        retrieval_service = local_retrieval
        app.state.federated_search = None

    app.state.retrieval_service = retrieval_service
    log.info("retrieval_service_ready", federated=settings.federation_enabled)

    # --- Ingestion worker ---
    index_writer = IndexWriter(session_factory)
    consumer: QueueConsumer[DocumentChangeEvent] = QueueConsumer(
        js=nats_conn.jetstream,
        stream="INGEST_CHANGES",
        subject="ingest.changes.*",
        durable="omniscience-ingestion-worker",
        payload_type=DocumentChangeEvent,
        dlq_subject="ingest.dlq.ingestion",
    )
    worker = IngestionWorker(
        queue_consumer=consumer,
        connector_registry=connector_registry,
        embedding_provider=embedding_provider,
        index_writer=index_writer,
        session_factory=session_factory,
    )
    worker_task = asyncio.create_task(worker.start())
    app.state.ingestion_worker = worker
    log.info("ingestion_worker_started")

    yield

    # --- Shutdown ---
    log.info("shutdown", app=settings.app_name)
    await worker.stop()
    worker_task.cancel()
    await embedding_provider.close()

    # Close the federation HTTP client if federation is active.
    if settings.federation_enabled and isinstance(retrieval_service, FederatedSearch):
        await retrieval_service.close()

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
