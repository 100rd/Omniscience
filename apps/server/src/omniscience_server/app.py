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
from omniscience_connectors import K8sAgenticConnector
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

from omniscience_server.discovery_worker import DiscoveryWorker
from omniscience_server.freshness_worker import FreshnessWorker
from omniscience_server.ingestion.events import DocumentChangeEvent
from omniscience_server.ingestion.worker import IngestionWorker
from omniscience_server.mcp.mount import create_mcp_asgi_app
from omniscience_server.mcp.server import mcp_server
from omniscience_server.middleware import TelemetryMiddleware, TracingMiddleware
from omniscience_server.outbox_consumer import OutboxConsumerWorker
from omniscience_server.outbox_worker import OutboxWorker
from omniscience_server.reconcile_worker import ReconcileWorker
from omniscience_server.rest import api_v1_router, register_error_handlers
from omniscience_server.rest.otlp_ingester import OtlpIngester
from omniscience_server.retention_worker import RetentionWorker
from omniscience_server.routes import health_router, tokens_router
from omniscience_server.scheduler import SchedulerWorker

log = structlog.get_logger(__name__)

# The SourceType enum uses "k8s" for the legacy agentic connector; the connector
# registers as "k8s-agentic".  Add an alias so the ingestion worker can look up
# the connector by SourceType string without modifying existing source rows.
connector_registry._connectors.setdefault(
    "k8s",
    connector_registry._connectors.get("k8s-agentic") or K8sAgenticConnector(),
)


# ---------------------------------------------------------------------------
# Backend values accepted by Settings.storage_*_backend.
# Any other value causes startup to abort with a clear error so
# ops that have misconfigured env vars get a loud failure, never a silent
# boot on an unintended backend.
# ---------------------------------------------------------------------------


_SUPPORTED_GRAPH_BACKENDS: frozenset[str] = frozenset({"neo4j", "postgres"})
_SUPPORTED_VECTOR_BACKENDS: frozenset[str] = frozenset({"qdrant", "postgres"})


class _UnwiredLegacyService:
    """Placeholder for ``GraphRAGComposer.legacy_service``.

    The composer dispatches to the Neo4j+Qdrant/Postgres pipeline 
    (``graphrag_active`` is always True). The ``legacy_service``
    branch is therefore unreachable at runtime; we still need to pass
    *something* that satisfies the ``_LegacySearchCallable`` protocol.

    Any accidental call (e.g. if the dispatch rules are refactored and
    a bug re-enables the legacy path) must fail loudly rather than
    silently degrade retrieval.
    """

    async def search(self, request: SearchRequest) -> SearchResult:
        raise RuntimeError(
            "retrieval_service_unwired: all search requests must route through "
            "the GraphRAG composer."
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

    # --- Backend validation ---
    graph_backend = str(settings.storage_graph_backend).lower()
    if graph_backend not in _SUPPORTED_GRAPH_BACKENDS:
        raise ValueError(
            f"unsupported_storage_graph_backend:{graph_backend!r} "
            f"(supported: {sorted(_SUPPORTED_GRAPH_BACKENDS)})."
        )
    vector_backend = str(settings.storage_vector_backend).lower()
    if vector_backend not in _SUPPORTED_VECTOR_BACKENDS:
        raise ValueError(
            f"unsupported_storage_vector_backend:{vector_backend!r} "
            f"(supported: {sorted(_SUPPORTED_VECTOR_BACKENDS)})."
        )

    if graph_backend == "postgres" and vector_backend == "postgres":
        from omniscience_index.stores.postgres_only_store import PostgresOnlyStore

        postgres_only_store = PostgresOnlyStore(
            session_factory=session_factory,
            embedding_provider=embedding_provider,
        )
        await postgres_only_store.connect()
        app.state.graph_store = postgres_only_store
        app.state.vector_store = postgres_only_store
        app.state.postgres_only_store = postgres_only_store
        app.state.qdrant_store = postgres_only_store
        app.state.neo4j_graph_store = postgres_only_store
        app.state.graph_bitemporal_enabled = False
        log.info(
            "storage_adapters_ready",
            graph_backend=graph_backend,
            vector_backend=vector_backend,
            collection="postgres_only",
        )
    else:
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

    # --- GraphRAG composer ---
    # The composer's type-based dispatch always lands on
    # the GraphRAG path; the ``legacy_service`` parameter is held for
    # backward compatibility of the constructor signature but is
    graph_rag_composer = GraphRAGComposer(
        graph_store=neo4j_graph_store,
        vector_store=qdrant_store,
        legacy_service=federated if federated is not None else _UnwiredLegacyService(),
        session_factory=session_factory,
    )
    app.state.graph_rag_composer = graph_rag_composer
    log.info(
        "graph_rag_composer_ready",
        graphrag_active=graph_rag_composer.graphrag_active,
    )

    # --- Ingestion worker ---
    # As of issue #126 and Epic #96, the writer is orchestrated and handles
    # all three stores (Postgres, Neo4j, Qdrant) internally.
    index_writer = IndexWriter(
        session_factory=session_factory,
        graph_store=neo4j_graph_store,
        vector_store=qdrant_store,
    )
    # OTLP/HTTP trace receiver (#152) — wired here so the route in
    # ``rest/otlp.py`` can pull the per-workspace ingester off
    # ``app.state``.  Tenant identity is resolved from the bearer token
    # by the route; this ingester is workspace-agnostic on construction.
    app.state.otlp_ingester = OtlpIngester(session_factory=session_factory)
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

    # --- Outbox worker ---
    outbox_worker = OutboxWorker(
        session_factory=session_factory,
        nats_conn=nats_conn,
        interval_seconds=1.0,
    )
    outbox_task = asyncio.create_task(outbox_worker.start())
    app.state.outbox_worker = outbox_worker
    log.info("outbox_worker_started")

    # --- Outbox consumer worker ---
    outbox_consumer = OutboxConsumerWorker(
        nats_conn=nats_conn,
        graph_store=neo4j_graph_store,
        vector_store=qdrant_store,
        embedding_provider=embedding_provider,
        session_factory=session_factory,
    )
    outbox_consumer_task = asyncio.create_task(outbox_consumer.start())
    app.state.outbox_consumer = outbox_consumer
    log.info("outbox_consumer_worker_started")

    # --- Discovery worker (v0.4 Preview) ---
    discovery_worker = DiscoveryWorker(
        session_factory=session_factory,
        settings=settings,
    )
    discovery_task = asyncio.create_task(discovery_worker.start())
    app.state.discovery_worker = discovery_worker
    log.info("discovery_worker_started")

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

    # --- Reconcile worker ---
    reconcile_worker = ReconcileWorker(
        session_factory=session_factory,
        vector_store=qdrant_store,
        graph_store=neo4j_graph_store,
        settings=settings,
    )
    reconcile_task = asyncio.create_task(reconcile_worker.start())
    app.state.reconcile_worker = reconcile_worker
    log.info(
        "reconcile_worker_started",
        tick_seconds=getattr(settings, "reconcile_tick_seconds", 3600),
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

    # --- MCP streamable-http session manager (FastMCP) ---
    # The MCP ASGI sub-app is mounted at /mcp, so its own lifespan never runs
    # under the parent app. We must start the session-manager task group here,
    # otherwise every MCP request 500s with "Task group is not initialized".
    async with mcp_server.session_manager.run():
        log.info("mcp_session_manager_started")
        yield

    # --- Shutdown ---
    log.info("shutdown", app=settings.app_name)

    discovery_worker.stop()
    discovery_task.cancel()

    freshness_worker.stop()
    freshness_task.cancel()

    reconcile_worker.stop()
    reconcile_task.cancel()

    if scheduler is not None and scheduler_task is not None:
        await scheduler.stop()
        scheduler_task.cancel()

    if retention_worker is not None and retention_task is not None:
        retention_worker.stop()
        retention_task.cancel()

    outbox_consumer.stop()
    outbox_consumer_task.cancel()

    outbox_worker.stop()
    outbox_task.cancel()

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
