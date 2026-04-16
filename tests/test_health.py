"""Tests for the /health and /metrics endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from omniscience_server.routes.health import _aggregate_status


@pytest.mark.asyncio
async def test_health_returns_200(client: AsyncClient) -> None:
    """GET /health returns HTTP 200 when all checks are healthy or unchecked."""
    response = await client.get("/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_health_response_structure(client: AsyncClient) -> None:
    """GET /health body contains the expected top-level keys."""
    response = await client.get("/health")
    body = response.json()

    assert "status" in body
    assert "checks" in body
    assert "version" in body


@pytest.mark.asyncio
async def test_health_checks_keys(client: AsyncClient) -> None:
    """GET /health body includes postgres and nats dependency checks."""
    response = await client.get("/health")
    checks = response.json()["checks"]

    assert "postgres" in checks
    assert "nats" in checks


@pytest.mark.asyncio
async def test_health_version_from_settings(client: AsyncClient) -> None:
    """GET /health returns the version from Settings, not a hardcoded string."""
    response = await client.get("/health")
    assert response.json()["version"] == "0.1.0"


@pytest.mark.asyncio
async def test_health_status_valid_value(client: AsyncClient) -> None:
    """Overall status is one of the known states."""
    response = await client.get("/health")
    status = response.json()["status"]
    assert status in {"healthy", "degraded", "unhealthy"}


@pytest.mark.asyncio
async def test_health_returns_503_when_unhealthy(client: AsyncClient) -> None:
    """GET /health returns HTTP 503 when a dependency is unhealthy."""
    with patch(
        "omniscience_server.routes.health._check_postgres",
        new_callable=AsyncMock,
        return_value="unhealthy",
    ):
        response = await client.get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_health_returns_200_when_degraded(client: AsyncClient) -> None:
    """GET /health returns HTTP 200 when a dependency is degraded."""
    with patch(
        "omniscience_server.routes.health._check_nats",
        new_callable=AsyncMock,
        return_value="degraded",
    ):
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "degraded"


# --- _aggregate_status unit tests ---


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ({"a": "healthy", "b": "healthy"}, "healthy"),
        ({"a": "unchecked", "b": "unchecked"}, "healthy"),
        ({"a": "healthy", "b": "unchecked"}, "healthy"),
        ({"a": "degraded", "b": "healthy"}, "degraded"),
        ({"a": "healthy", "b": "degraded"}, "degraded"),
        ({"a": "unhealthy", "b": "healthy"}, "unhealthy"),
        ({"a": "unhealthy", "b": "degraded"}, "unhealthy"),
    ],
)
def test_aggregate_status(checks: dict[str, str], expected: str) -> None:  # type: ignore[type-arg]
    """_aggregate_status correctly prioritizes unhealthy > degraded > healthy."""
    assert _aggregate_status(checks) == expected  # type: ignore[arg-type]


# --- /metrics tests ---


@pytest.mark.asyncio
async def test_metrics_returns_200(client: AsyncClient) -> None:
    """GET /metrics returns HTTP 200."""
    response = await client.get("/metrics")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_metrics_content_type(client: AsyncClient) -> None:
    """GET /metrics returns Prometheus text exposition format."""
    response = await client.get("/metrics")
    assert "text/plain" in response.headers["content-type"]


@pytest.mark.asyncio
async def test_metrics_contains_request_counter(client: AsyncClient) -> None:
    """After a /health call the request counter metric is present in /metrics output."""
    await client.get("/health")
    response = await client.get("/metrics")
    assert b"omniscience_http_requests_total" in response.content
