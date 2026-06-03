"""Tests for GET /api/v1/admin/components (issue: system status page).

Coverage:
* 401 without a token.
* 403 with a token lacking admin scope.
* 200 happy path — response shape contains all five component blocks.
* Graceful-degrade — one store's driver raises; that component is "error"
  but the response is still HTTP 200 and other components remain "ok".
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_core.auth.tokens import generate_token, hash_token
from omniscience_core.config import Settings
from omniscience_core.db.models import ApiToken
from omniscience_server.app import create_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(scopes: list[str]) -> tuple[ApiToken, str]:
    """Create a real hashed token with the given scopes."""
    plaintext, prefix = generate_token("test")
    hashed = hash_token(plaintext)
    tok: ApiToken = MagicMock(spec=ApiToken)
    tok.id = uuid.uuid4()
    tok.name = "test-token"
    tok.token_prefix = prefix
    tok.hashed_token = hashed
    tok.scopes = scopes
    tok.workspace_id = None
    tok.expires_at = None
    tok.is_active = True
    tok.last_used_at = None
    return tok, plaintext


def _make_db_session(token: ApiToken) -> AsyncMock:
    """Return a fake async DB session that resolves *token* on bearer lookup."""
    session = AsyncMock()

    async def _execute(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.all.return_value = [token]
        result.scalars.return_value.first.return_value = token
        result.scalar_one.return_value = 1
        return result

    session.execute = _execute
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _build_app(token: ApiToken) -> FastAPI:
    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)
    app.state.db_session_factory = MagicMock(return_value=_make_db_session(token))
    return app


def _make_healthy_stores(app: FastAPI) -> None:
    """Wire app.state with mock stores that simulate fully-healthy components."""
    # Neo4j mock — driver.session() returns an async context manager
    mock_graph_store = MagicMock()
    mock_graph_store._config = MagicMock()
    mock_graph_store._config.database = "neo4j"

    async def _neo4j_session_cm(*args: Any, **kwargs: Any) -> Any:
        session = AsyncMock()
        result_mock = AsyncMock()
        result_mock.single = AsyncMock(return_value={"total": 42})
        session.run = AsyncMock(return_value=result_mock)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        return session

    mock_graph_store._driver = MagicMock()
    mock_graph_store._driver.session = MagicMock(side_effect=_neo4j_session_cm)
    app.state.graph_store = mock_graph_store

    # Qdrant mock
    mock_vector_store = MagicMock()
    mock_vector_store.collection_name = "test_collection"
    mock_collection_info = MagicMock()
    mock_collection_info.vectors_count = 100
    mock_collection_info.points_count = 100
    mock_collection_info.status = MagicMock()
    mock_collection_info.status.value = "green"
    mock_vector_store._qc = AsyncMock()
    mock_vector_store._qc.get_collection = AsyncMock(return_value=mock_collection_info)
    app.state.vector_store = mock_vector_store

    # NATS mock
    mock_stream_state = MagicMock()
    mock_stream_state.messages = 10
    mock_stream_state.bytes = 1024

    mock_stream_config = MagicMock()
    mock_stream_config.name = "INGEST_CHANGES"

    mock_stream_info = MagicMock()
    mock_stream_info.state = mock_stream_state
    mock_stream_info.config = mock_stream_config

    mock_consumer_info = MagicMock()
    mock_consumer_info.name = "omniscience-ingestion-worker"
    mock_consumer_info.num_pending = 5
    mock_consumer_info.num_ack_pending = 0
    mock_consumer_info.num_redelivered = 0

    mock_js = AsyncMock()
    mock_js.streams_info = AsyncMock(return_value=[mock_stream_info])
    mock_js.consumers_info = AsyncMock(return_value=[mock_consumer_info])

    mock_nats = MagicMock()
    mock_nats.jetstream = mock_js
    app.state.nats = mock_nats

    # Embedding provider mock
    mock_embedding = MagicMock()
    mock_embedding.provider_name = "ollama"
    mock_embedding.model_name = "nomic-embed-text"
    mock_embedding.dim = 768
    app.state.embedding_provider = mock_embedding


# ---------------------------------------------------------------------------
# 401 — no token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_components_401_without_token() -> None:
    """GET /api/v1/admin/components returns 401 when no token is provided."""
    token, _ = _make_token(["admin"])
    app = _build_app(token)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/admin/components")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 403 — wrong scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_components_403_without_admin_scope() -> None:
    """GET /api/v1/admin/components returns 403 for a token without admin scope."""
    token, plaintext = _make_token(["stats:read"])
    app = _build_app(token)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/admin/components",
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 200 happy path — shape validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_components_200_shape() -> None:
    """Healthy request returns 200 with all five component blocks present."""
    token, plaintext = _make_token(["admin"])
    app = _build_app(token)

    from omniscience_server.rest.components import (
        EmbeddingComponent,
        EmbeddingMetrics,
        NatsComponent,
        NatsMetrics,
        NatsStreamMetrics,
        Neo4jComponent,
        Neo4jMetrics,
        PostgresComponent,
        PostgresMetrics,
        QdrantComponent,
        QdrantMetrics,
    )

    ok_postgres = PostgresComponent(
        status="ok",
        metrics=PostgresMetrics(
            size_bytes=1_000_000,
            table_counts={
                "sources": 3,
                "documents": 100,
                "chunks": 500,
                "entities": 200,
                "edges": 150,
                "api_tokens": 5,
                "workspaces": 1,
            },
        ),
        error=None,
    )
    ok_neo4j = Neo4jComponent(
        status="ok",
        metrics=Neo4jMetrics(
            total_nodes=242,
            total_relationships=150,
            entity_nodes=200,
            entity_state_nodes=42,
        ),
        error=None,
    )
    ok_qdrant = QdrantComponent(
        status="ok",
        metrics=QdrantMetrics(
            collection_name="test_col",
            vectors_count=500,
            points_count=500,
            collection_status="green",
        ),
        error=None,
    )
    ok_nats = NatsComponent(
        status="ok",
        metrics=NatsMetrics(
            streams=[
                NatsStreamMetrics(name="INGEST_CHANGES", messages=10, bytes=1024, consumers=[])
            ]
        ),
        error=None,
    )
    ok_embedding = EmbeddingComponent(
        status="ok",
        metrics=EmbeddingMetrics(provider="ollama", model="nomic-embed-text", dim=768),
        error=None,
    )

    with (
        patch(
            "omniscience_server.rest.components._collect_postgres",
            AsyncMock(return_value=ok_postgres),
        ),
        patch(
            "omniscience_server.rest.components._collect_neo4j",
            AsyncMock(return_value=ok_neo4j),
        ),
        patch(
            "omniscience_server.rest.components._collect_qdrant",
            AsyncMock(return_value=ok_qdrant),
        ),
        patch(
            "omniscience_server.rest.components._collect_nats",
            AsyncMock(return_value=ok_nats),
        ),
        patch(
            "omniscience_server.rest.components._collect_embedding",
            MagicMock(return_value=ok_embedding),
        ),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/admin/components",
                headers={"Authorization": f"Bearer {plaintext}"},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "version" in data

    # All five top-level component keys must be present
    for key in ("postgres", "neo4j", "qdrant", "nats", "embedding"):
        assert key in data, f"missing component key: {key}"
        assert data[key]["status"] == "ok"
        assert data[key]["metrics"] is not None

    # Postgres metrics shape
    pg = data["postgres"]["metrics"]
    assert "size_bytes" in pg
    assert "table_counts" in pg
    assert "sources" in pg["table_counts"]

    # Neo4j metrics shape
    n4 = data["neo4j"]["metrics"]
    assert "total_nodes" in n4
    assert "total_relationships" in n4
    assert "entity_nodes" in n4
    assert "entity_state_nodes" in n4

    # Qdrant metrics shape
    qd = data["qdrant"]["metrics"]
    assert "collection_name" in qd
    assert "vectors_count" in qd
    assert "points_count" in qd

    # NATS metrics shape
    nats_data = data["nats"]["metrics"]
    assert "streams" in nats_data
    assert isinstance(nats_data["streams"], list)

    # Embedding metrics shape
    emb = data["embedding"]["metrics"]
    assert "provider" in emb
    assert "model" in emb
    assert "dim" in emb


# ---------------------------------------------------------------------------
# Graceful degrade — neo4j driver raises, collector returns "error" component
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_components_graceful_degrade_one_store_error() -> None:
    """When the neo4j driver raises, the neo4j block is 'error' but response is 200.

    The other components must remain 'ok', and the top-level status degrades
    to 'error' (worst-of aggregation) because one component failed.
    """
    token, plaintext = _make_token(["admin"])
    app = _build_app(token)

    from omniscience_server.rest.components import (
        EmbeddingComponent,
        EmbeddingMetrics,
        NatsComponent,
        NatsMetrics,
        PostgresComponent,
        PostgresMetrics,
        QdrantComponent,
        QdrantMetrics,
    )

    ok_postgres = PostgresComponent(
        status="ok",
        metrics=PostgresMetrics(
            size_bytes=1_000_000,
            table_counts={
                t: 0
                for t in [
                    "sources",
                    "documents",
                    "chunks",
                    "entities",
                    "edges",
                    "api_tokens",
                    "workspaces",
                ]
            },
        ),
        error=None,
    )
    ok_qdrant = QdrantComponent(
        status="ok",
        metrics=QdrantMetrics(
            collection_name="test_col",
            vectors_count=0,
            points_count=0,
            collection_status="green",
        ),
        error=None,
    )
    ok_nats = NatsComponent(
        status="ok",
        metrics=NatsMetrics(streams=[]),
        error=None,
    )
    ok_embedding = EmbeddingComponent(
        status="ok",
        metrics=EmbeddingMetrics(provider="ollama", model="nomic-embed-text", dim=768),
        error=None,
    )

    # Wire a broken graph_store whose driver.session() raises so that
    # _collect_neo4j catches the exception internally and returns
    # Neo4jComponent(status="error").
    mock_broken_graph_store = MagicMock()
    mock_broken_graph_store._config = MagicMock()
    mock_broken_graph_store._config.database = "neo4j"
    broken_driver = MagicMock()
    broken_driver.session = MagicMock(side_effect=RuntimeError("neo4j connection refused"))
    mock_broken_graph_store._driver = broken_driver
    app.state.graph_store = mock_broken_graph_store

    with (
        patch(
            "omniscience_server.rest.components._collect_postgres",
            AsyncMock(return_value=ok_postgres),
        ),
        patch(
            "omniscience_server.rest.components._collect_qdrant",
            AsyncMock(return_value=ok_qdrant),
        ),
        patch(
            "omniscience_server.rest.components._collect_nats",
            AsyncMock(return_value=ok_nats),
        ),
        patch(
            "omniscience_server.rest.components._collect_embedding",
            MagicMock(return_value=ok_embedding),
        ),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/admin/components",
                headers={"Authorization": f"Bearer {plaintext}"},
            )

    assert resp.status_code == 200
    data = resp.json()
    # Top-level degrades to "error" because neo4j failed
    assert data["status"] == "error"
    # Neo4j component reports error
    assert data["neo4j"]["status"] == "error"
    assert data["neo4j"]["error"] is not None
    assert data["neo4j"]["metrics"] is None
    # All other components remain ok
    assert data["postgres"]["status"] == "ok"
    assert data["qdrant"]["status"] == "ok"
    assert data["nats"]["status"] == "ok"
    assert data["embedding"]["status"] == "ok"
