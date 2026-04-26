"""FastAPI application factory.

Create the ASGI application by calling ``create_app()``.  The factory:
  - reads Settings from the environment
  - configures structured logging
  - initialises OpenTelemetry
  - connects to Postgres (operational metadata), NATS JetStream,
    embedding provider, Neo4j (graph store), and Qdrant (vector store)
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
from omniscience_core.telemetry import init_telemetry
from omniscience_embeddings import create_embedding_provider
from omniscience_embeddings.base import EmbeddingProvider
from omniscience_index import IndexWriter
from omniscience_index.stores import Neo4jGraphStore, Neo4jStoreConfig
from omniscience_index.stores.qdrant_config import QdrantConfig
from omniscience_index.stores.qdrant_store import QdrantVectorStore
from omniscience_retrieval import GraphRAGComposer
from omniscience_retrieval.federation import FederatedSearch
from omniscience_retrieval.federation_config import FederationConfig
from omniscience_retrieval.models import SearchRequest, SearchResult
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import Response

from omniscience_server.freshness_worker import FreshnessWorker
from omniscience_server.ingestion.events import DocumentChangeEvent
from omniscience_server.ingestion.worker import IngestionWorker
from omniscience_server.mcp.mount import create_mcp_asgi_app
from omniscience_server.middleware import TelemetryMiddleware, TracingMiddleware
from omniscience_server.rest import api_v1_router, register_error_handlers
from omniscience_server.retention_worker import RetentionWorker
from omniscience_server.routes import health_router, tokens_router
from omniscience_server.scheduler import SchedulerWorker

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Backend values accepted by Settings.storage_*_backend after the #105
# cutover.  Any other value causes startup to abort with a clear error so
# ops that still have pre-v0.2 env vars get a loud failure, never a silent
# boot on an unintended backend.
# ---------------------------------------------------------------------------


_SUPPORTED_GRAPH_BACKENDS: frozenset[str] = frozenset({"neo4j"})
_SUPPORTED_VECTOR_BACKENDS: frozenset[str] = frozenset({"qdrant"})


class _UnwiredLegacyService:
    """Placeholder for ``GraphRAGComposer.legacy_service`` post-cutover.

    After #105 removed the pgvector adapters, the composer always
    dispatches to the Neo4j+Qdrant pipeline (``graphrag_active`` is
    always True with the supported backend set).  The ``legacy_service``
    branch is therefore unreachable at runtime; we still need to pass
    *something* that satisfies the ``_LegacySearchCallable`` protocol.

    Any accidental call (e.g. if the dispatch rules are refactored and
    a bug re-enables the legacy path) must fail loudly rather than
    silently degrade retrieval.
    """

    async def search(self, request: SearchRequest) -> SearchResult:
        raise RuntimeError(
            "legacy_retrieval_service_unwired: the pgvector retrieval path was "
            "removed at the #105 cutover; all search requests must route through "
            "the GraphRAG composer (Neo4j + Qdrant). See CHANGELOG.md §0.2.0."
        )


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

    # --- Postgres connection (operational metadata only: sources, ---
    # ingestion_runs, api_tokens, workspaces, locks).  Chunk text,
    # chunk embeddings, and the symbol graph all live outside Postgres
    # as of v0.2 (Qdrant + Neo4j).
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

    # --- Backend validation (Epic #96 cutover, issue #105) ---
    # Only 'neo4j' + 'qdrant' are accepted as of v0.2.  An unknown
    # value is rejected here, before any connections are opened, so
    # operators with leftover ``*_BACKEND=pgvector`` env vars see a
    # clear failure instead of a silently-degraded boot.
    graph_backend = str(settings.storage_graph_backend).lower()
    if graph_backend not in _SUPPORTED_GRAPH_BACKENDS:
        raise ValueError(
            f"unsupported_storage_graph_backend:{graph_backend!r} "
            f"(supported: {sorted(_SUPPORTED_GRAPH_BACKENDS)}). "
            "Pgvector was removed at the #105 cutover; see CHANGELOG.md §0.2.0."
        )
    vector_backend = str(settings.storage_vector_backend).lower()
    if vector_backend not in _SUPPORTED_VECTOR_BACKENDS:
        raise ValueError(
            f"unsupported_storage_vector_backend:{vector_backend!r} "
            f"(supported: {sorted(_SUPPORTED_VECTOR_BACKENDS)}). "
            "Pgvector was removed at the #105 cutover; see CHANGELOG.md §0.2.0."
        )

    # --- Graph store (Neo4j, ADR-0005) ---
    neo4j_config = Neo4jStoreConfig.from_settings(settings)
    neo4j_graph_store = Neo4jGraphStore(config=neo4j_config)
    await neo4j_graph_store.connect()
    app.state.graph_store = neo4j_graph_store
    app.state.neo4j_graph_store = neo4j_graph_store
    # ADR-0008 §8 phase 2 — bitemporal write-path rollout flag.  Read-only
    # in this PR (issue #130 lands the schema DDL); the writer that gates
    # on it lands in #131.  Defaults to "disabled" so PR #104's writer
    # behaviour is preserved verbatim until #131.
    app.state.graph_bitemporal_enabled = settings.graph_bitemporal == "enabled"

    # --- Vector store (Qdrant, ADR-0006) ---
    qdrant_store = await _build_qdrant_store(settings, embedding_provider)
    app.state.vector_store = qdrant_store
    app.state.qdrant_store = qdrant_store
    log.info(
        "storage_adapters_ready",
        graph_backend=graph_backend,
        vector_backend=vector_backend,
        collection=qdrant_store.collection_name,
    )

    # --- Legacy retrieval handle (unwired after #105) ---
    # The pgvector ``RetrievalService`` is no longer instantiated.
    # Callers that require a non-workspace-scoped legacy fallback
    # receive 503 from the REST/MCP layer.
    app.state.retrieval_service = None

    # --- Federation (optional) ---
    # Federation still operates over the ``RetrievalService`` shape so
    # that remote v0.1 peers remain reachable; the local leg wraps the
    # unwired placeholder, which the composer never invokes once the
    # Neo4j+Qdrant pair is active.
    if settings.federation_enabled:
        fed_config = FederationConfig.from_json(settings.federation_instances)
        fed_config = fed_config.model_copy(
            update={"timeout_seconds": float(settings.federation_timeout_seconds)}
        )
        federated = FederatedSearch(
            local_service=_UnwiredLegacyService(),
            config=fed_config,
        )
        app.state.federated_search = federated
        log.info(
            "federation_enabled",
            peers=len(fed_config.enabled_instances),
            timeout_s=fed_config.timeout_seconds,
        )
    else:
        federated = None
        app.state.federated_search = None

    # --- GraphRAG composer (issue #107) ---
    # Post-#105 the composer's type-based dispatch always lands on
    # the GraphRAG path; the ``legacy_service`` parameter is held for
    # backward compatibility of the constructor signature but is
    # never invoked at runtime.  The placeholder raises if it ever is.
    graph_rag_composer = GraphRAGComposer(
        graph_store=neo4j_graph_store,
        vector_store=qdrant_store,
        legacy_service=federated if federated is not None else _UnwiredLegacyService(),
    )
    app.state.graph_rag_composer = graph_rag_composer
    log.info(
        "graph_rag_composer_ready",
        graphrag_active=graph_rag_composer.graphrag_active,
    )

    # --- Ingestion worker ---
    # As of issue #126 the worker drives all three stores: Postgres
    # (operational metadata via ``IndexWriter``), Neo4j (entities + edges
    # via ``graph_store``), and Qdrant (chunk embeddings via
    # ``vector_store``).  ``workspace_id`` is resolved from
    # ``Source.tenant_id`` per document before any adapter call — the
    # ACL invariant from ADR-0005/0006 carries through the live path the
    # same way the migration runner enforces it for backfills.
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
        graph_store=neo4j_graph_store,
        vector_store=qdrant_store,
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

    # --- Retention worker (ADR-0009 §3, issue #135) ---
    retention_task: asyncio.Task[None] | None = None
    retention_worker: RetentionWorker | None = None
    if settings.retention_enabled:
        retention_worker = RetentionWorker(
            session_factory=session_factory,
            graph_store=neo4j_graph_store,
            vector_store=qdrant_store,
            settings=settings,
        )
        retention_task = asyncio.create_task(retention_worker.start())
        app.state.retention_worker = retention_worker
        log.info(
            "retention_worker_started",
            tick_seconds=settings.retention_tick_seconds,
            dry_run=settings.retention_dry_run,
            hot_days=settings.retention_hot_days,
            warm_days=settings.retention_warm_days,
        )
    else:
        app.state.retention_worker = None
        log.info("retention_worker_disabled")

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

    if retention_worker is not None and retention_task is not None:
        retention_worker.stop()
        retention_task.cancel()

    await worker.stop()
    worker_task.cancel()
    await embedding_provider.close()

    await qdrant_store.close()

    if federated is not None:
        await federated.close()

    await neo4j_graph_store.close()

    await engine.dispose()
    await nats_conn.disconnect()


async def _build_qdrant_store(
    settings: Settings, embedding_provider: EmbeddingProvider
) -> QdrantVectorStore:
    """Construct, connect, and bootstrap a ``QdrantVectorStore`` (#106).

    Reads connection settings from ``Settings`` (including the API key
    from ``QDRANT_API_KEY``) and returns a ready-to-use adapter with
    the collection and payload indexes already ensured on the cluster.
    """
    config = QdrantConfig(
        host=settings.qdrant_host,
        grpc_port=settings.qdrant_grpc_port,
        http_port=settings.qdrant_http_port,
        api_key=settings.qdrant_api_key,
        https=settings.qdrant_https,
        prefer_grpc=settings.qdrant_prefer_grpc,
        timeout_seconds=settings.qdrant_timeout_seconds,
    )
    store = QdrantVectorStore(config=config, embedding_provider=embedding_provider)
    await store.connect()
    return store


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

    # Middleware (applied in reverse registration order by Starlette).
    # Order matters: TelemetryMiddleware is registered FIRST so it sits
    # innermost (closest to the route), giving it access to the populated
    # ``request.state.api_token`` set by the auth dependency. The outer
    # TracingMiddleware records latency and binds log context.
    app.add_middleware(TelemetryMiddleware)
    app.add_middleware(TracingMiddleware)

    # Exception handlers for spec-compliant error responses
    register_error_handlers(app)

    # Routers
    app.include_router(health_router)
    app.include_router(tokens_router)
    app.include_router(api_v1_router)

    return app
