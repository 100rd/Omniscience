"""System Component Status endpoint (GET /api/v1/admin/components).

Returns a per-component health and metrics snapshot for the Omniscience
infrastructure:  Postgres, Neo4j, Qdrant, NATS JetStream, and the
embedding provider.

Each component block is isolated with a try/except so one failing
dependency does not 500 the whole response.  The top-level ``status``
field is the worst of all component statuses.

Requires scope: ``admin``.
"""

from __future__ import annotations

from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, Request
from omniscience_core.auth.middleware import require_scope
from omniscience_core.auth.scopes import Scope
from pydantic import BaseModel
from sqlalchemy import text

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin-components"])

# Module-level Depends singleton — avoids ruff B008.
_admin_scope_dep: Any = Depends(require_scope(Scope.admin))

ComponentStatus = Literal["ok", "degraded", "error"]

_STATUS_RANK: dict[ComponentStatus, int] = {"ok": 0, "degraded": 1, "error": 2}


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class PostgresMetrics(BaseModel):
    size_bytes: int
    table_counts: dict[str, int]


class PostgresComponent(BaseModel):
    status: ComponentStatus
    metrics: PostgresMetrics | None
    error: str | None


class Neo4jMetrics(BaseModel):
    total_nodes: int
    total_relationships: int
    entity_nodes: int
    entity_state_nodes: int


class Neo4jComponent(BaseModel):
    status: ComponentStatus
    metrics: Neo4jMetrics | None
    error: str | None


class QdrantMetrics(BaseModel):
    collection_name: str
    vectors_count: int
    points_count: int
    collection_status: str


class QdrantComponent(BaseModel):
    status: ComponentStatus
    metrics: QdrantMetrics | None
    error: str | None


class NatsConsumerMetrics(BaseModel):
    name: str
    num_pending: int
    num_ack_pending: int
    num_redelivered: int


class NatsStreamMetrics(BaseModel):
    name: str
    messages: int
    bytes: int
    consumers: list[NatsConsumerMetrics]


class NatsMetrics(BaseModel):
    streams: list[NatsStreamMetrics]


class NatsComponent(BaseModel):
    status: ComponentStatus
    metrics: NatsMetrics | None
    error: str | None


class EmbeddingMetrics(BaseModel):
    provider: str
    model: str
    dim: int


class EmbeddingComponent(BaseModel):
    status: ComponentStatus
    metrics: EmbeddingMetrics | None
    error: str | None


class ComponentsResponse(BaseModel):
    status: ComponentStatus
    version: str
    postgres: PostgresComponent
    neo4j: Neo4jComponent
    qdrant: QdrantComponent
    nats: NatsComponent
    embedding: EmbeddingComponent


# ---------------------------------------------------------------------------
# Per-component collectors
# ---------------------------------------------------------------------------

_TABLE_NAMES = [
    "sources",
    "documents",
    "chunks",
    "entities",
    "edges",
    "api_tokens",
    "workspaces",
]


async def _collect_postgres(request: Request) -> PostgresComponent:
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        return PostgresComponent(
            status="error", metrics=None, error="db_session_factory not wired"
        )
    try:
        async with factory() as db:
            size_result = await db.execute(text("SELECT pg_database_size(current_database())"))
            size_bytes = int(size_result.scalar_one())
            table_counts: dict[str, int] = {}
            for table in _TABLE_NAMES:
                count_result = await db.execute(text(f"SELECT COUNT(*) FROM {table}"))  # noqa: S608
                table_counts[table] = int(count_result.scalar_one())
        return PostgresComponent(
            status="ok",
            metrics=PostgresMetrics(size_bytes=size_bytes, table_counts=table_counts),
            error=None,
        )
    except Exception as exc:
        log.warning("components_postgres_error", error=str(exc))
        return PostgresComponent(status="error", metrics=None, error=str(exc))


async def _collect_neo4j(request: Request) -> Neo4jComponent:
    graph_store = getattr(request.app.state, "graph_store", None)
    if graph_store is None:
        return Neo4jComponent(status="error", metrics=None, error="graph_store not wired")
    try:
        driver = graph_store._driver  # reuse the already-connected AsyncDriver
        database = graph_store._config.database
        async with driver.session(database=database) as session:
            total_nodes_row = await session.run("MATCH (n) RETURN count(n) AS total")
            total_nodes = int((await total_nodes_row.single())["total"])

            total_rels_row = await session.run("MATCH ()-[r]->() RETURN count(r) AS total")
            total_rels = int((await total_rels_row.single())["total"])

            entity_row = await session.run("MATCH (n:Entity) RETURN count(n) AS total")
            entity_count = int((await entity_row.single())["total"])

            estate_row = await session.run("MATCH (n:EntityState) RETURN count(n) AS total")
            estate_count = int((await estate_row.single())["total"])

        return Neo4jComponent(
            status="ok",
            metrics=Neo4jMetrics(
                total_nodes=total_nodes,
                total_relationships=total_rels,
                entity_nodes=entity_count,
                entity_state_nodes=estate_count,
            ),
            error=None,
        )
    except Exception as exc:
        log.warning("components_neo4j_error", error=str(exc))
        return Neo4jComponent(status="error", metrics=None, error=str(exc))


async def _collect_qdrant(request: Request) -> QdrantComponent:
    vector_store = getattr(request.app.state, "vector_store", None)
    if vector_store is None:
        return QdrantComponent(status="error", metrics=None, error="vector_store not wired")
    try:
        collection_name = vector_store.collection_name
        info = await vector_store._qc.get_collection(collection_name)
        vectors_count = int(info.vectors_count or 0)
        points_count = int(info.points_count or 0)
        collection_status = str(info.status.value) if info.status is not None else "unknown"
        return QdrantComponent(
            status="ok",
            metrics=QdrantMetrics(
                collection_name=collection_name,
                vectors_count=vectors_count,
                points_count=points_count,
                collection_status=collection_status,
            ),
            error=None,
        )
    except Exception as exc:
        log.warning("components_qdrant_error", error=str(exc))
        return QdrantComponent(status="error", metrics=None, error=str(exc))


async def _collect_nats(request: Request) -> NatsComponent:
    nats_conn = getattr(request.app.state, "nats", None)
    if nats_conn is None:
        return NatsComponent(status="error", metrics=None, error="nats not wired")
    try:
        js = nats_conn.jetstream
        stream_infos = await js.streams_info()
        streams: list[NatsStreamMetrics] = []
        for si in stream_infos:
            consumers: list[NatsConsumerMetrics] = []
            try:
                consumer_infos = await js.consumers_info(si.config.name)
                for ci in consumer_infos:
                    consumers.append(
                        NatsConsumerMetrics(
                            name=ci.name,
                            num_pending=int(ci.num_pending or 0),
                            num_ack_pending=int(ci.num_ack_pending or 0),
                            num_redelivered=int(ci.num_redelivered or 0),
                        )
                    )
            except Exception as consumer_exc:
                log.warning(
                    "components_nats_consumers_error",
                    stream=si.config.name,
                    error=str(consumer_exc),
                )
            streams.append(
                NatsStreamMetrics(
                    name=si.config.name,
                    messages=int(si.state.messages),
                    bytes=int(si.state.bytes),
                    consumers=consumers,
                )
            )
        return NatsComponent(status="ok", metrics=NatsMetrics(streams=streams), error=None)
    except Exception as exc:
        log.warning("components_nats_error", error=str(exc))
        return NatsComponent(status="error", metrics=None, error=str(exc))


def _collect_embedding(request: Request) -> EmbeddingComponent:
    provider = getattr(request.app.state, "embedding_provider", None)
    if provider is None:
        return EmbeddingComponent(
            status="error", metrics=None, error="embedding_provider not wired"
        )
    try:
        return EmbeddingComponent(
            status="ok",
            metrics=EmbeddingMetrics(
                provider=provider.provider_name,
                model=provider.model_name,
                dim=provider.dim,
            ),
            error=None,
        )
    except Exception as exc:
        log.warning("components_embedding_error", error=str(exc))
        return EmbeddingComponent(status="error", metrics=None, error=str(exc))


def _worst_status(*components: ComponentStatus) -> ComponentStatus:
    """Return the worst (highest-rank) status from a sequence of component statuses."""
    return max(components, key=lambda s: _STATUS_RANK[s])


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/components",
    response_model=ComponentsResponse,
    summary="Per-component infrastructure status",
    dependencies=[_admin_scope_dep],
)
async def get_components(request: Request) -> ComponentsResponse:
    """Return a health and metrics snapshot for each Omniscience infrastructure component.

    Each component is probed independently — a single failure is isolated
    to that component's block and never causes a 500 response.

    Requires scope: ``admin``
    """
    settings = getattr(request.app.state, "settings", None)
    version = str(getattr(settings, "app_version", "unknown")) if settings else "unknown"

    postgres = await _collect_postgres(request)
    neo4j = await _collect_neo4j(request)
    qdrant = await _collect_qdrant(request)
    nats = await _collect_nats(request)
    embedding = _collect_embedding(request)

    overall = _worst_status(
        postgres.status,
        neo4j.status,
        qdrant.status,
        nats.status,
        embedding.status,
    )

    log.info(
        "components_status",
        overall=overall,
        postgres=postgres.status,
        neo4j=neo4j.status,
        qdrant=qdrant.status,
        nats=nats.status,
        embedding=embedding.status,
    )

    return ComponentsResponse(
        status=overall,
        version=version,
        postgres=postgres,
        neo4j=neo4j,
        qdrant=qdrant,
        nats=nats,
        embedding=embedding,
    )


__all__ = ["router"]
