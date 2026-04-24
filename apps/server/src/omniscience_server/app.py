"""FastAPI application factory.

Create the ASGI application by calling ``create_app()``.  The factory:
  - reads Settings from the environment
  - configures structured logging
  - initialises OpenTelemetry
  - connects to Postgres, NATS JetStream, embedding provider
  - starts the ingestion worker (consumes document change events)
  - starts the freshness worker (periodic SLO evaluation + Prometheus metrics)
  - starts the scheduler worker (periodic stale-source re-sync triggers)
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
from omniscience_core.storage.graph import GraphStore
from omniscience_core.telemetry import init_telemetry
from omniscience_embeddings import create_embedding_provider
from omniscience_index import IndexWriter
from omniscience_index.stores import Neo4jGraphStore, Neo4jStoreConfig
from omniscience_retrieval import (
    PgVectorGraphStore,
    PgVectorVectorStore,
    RetrievalService,
)
from omniscience_retrieval.federation import FederatedSearch
from omniscience_retrieval.federation_config import FederationConfig
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import Response

from omniscience_server.freshness_worker import FreshnessWorker
from omniscience_server.ingestion.events import DocumentChangeEvent
from omniscience_server.ingestion.worker import IngestionWorker
from omniscience_server.mcp.mount import create_mcp_asgi_app
from omniscience_server.middleware import TracingMiddleware
from omniscience_server.rest import api_v1_router, register_error_handlers
from omniscience_server.routes import health_router, tokens_router
from omniscience_server.scheduler import SchedulerWorker

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

    # --- Storage adapters (GraphStore / VectorStore protocols, issue #103) ---
    # These wrap the pgvector writer + retriever behind the new backend-
    # neutral protocols defined in ``omniscience_core.storage``.  Phase 2a
    # (issue #104) selects the Neo4j graph backend behind the
    # ``STORAGE_GRAPH_BACKEND`` feature flag; vector backend selection is
    # separate (issue #106).
    graph_backend = str(settings.storage_graph_backend).lower()
    graph_store: GraphStore
    neo4j_graph_store: Neo4jGraphStore | None = None
    if graph_backend == "neo4j":
        neo4j_config = Neo4jStoreConfig.from_settings(settings)
        neo4j_graph_store = Neo4jGraphStore(config=neo4j_config)
        await neo4j_graph_store.connect()
        graph_store = neo4j_graph_store
    elif graph_backend == "pgvector":
        graph_store = PgVectorGraphStore(session_factory=session_factory)
    else:
        raise ValueError(
            f"unknown_storage_graph_backend:{graph_backend} (expected 'pgvector' or 'neo4j')"
        )
    vector_store = PgVectorVectorStore(
        session_factory=session_factory,
        embedding_provider=embedding_provider,
    )
    app.state.graph_store = graph_store
    app.state.vector_store = vector_store
    app.state.neo4j_graph_store = neo4j_graph_store
    log.info("storage_adapters_ready", graph_backend=graph_backend)

    # --- Retrieval service ---
    # Keep the direct ``RetrievalService`` handle on app.state for the
    # (still-in-flight) federation composition path.  New consumers
    # should prefer ``app.state.vector_store``.
    local_retrieval = vector_store.retrieval_service

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

    # --- Freshness worker ---
    freshness_worker = FreshnessWorker(
        session_factory=session_factory,
        interval_seconds=getattr(settings, "freshness_check_interval_seconds", 60.0),
    )
    freshness_task = asyncio.create_task(freshness_worker.start())
    app.state.freshness_worker = freshness_worker
    log.info(
        "freshness_worker_started",
        interval_seconds=freshness_worker._interval,
    )

    # --- Scheduler worker ---
    scheduler_task: asyncio.Task[None] | None = None
    scheduler: SchedulerWorker | None = None
    if settings.scheduler_enabled:
        scheduler = SchedulerWorker(
            session_factory=session_factory,
            nats_conn=nats_conn,
            interval=settings.scheduler_interval_seconds,
        )
        scheduler_task = asyncio.create_task(scheduler.start())
        app.state.scheduler = scheduler
        log.info(
            "scheduler_worker_started",
            interval_seconds=settings.scheduler_interval_seconds,
        )
    else:
        app.state.scheduler = None
        log.info("scheduler_worker_disabled")

    yield

    # --- Shutdown ---
    log.info("shutdown", app=settings.app_name)

    freshness_worker.stop()
    freshness_task.cancel()

    if scheduler is not None and scheduler_task is not None:
        await scheduler.stop()
        scheduler_task.cancel()

    await worker.stop()
    worker_task.cancel()
    await embedding_provider.close()

    # Close the federation HTTP client if federation is active.
    if settings.federation_enabled and isinstance(retrieval_service, FederatedSearch):
        await retrieval_service.close()

    if neo4j_graph_store is not None:
        await neo4j_graph_store.close()

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
