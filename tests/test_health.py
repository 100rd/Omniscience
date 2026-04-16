"""Tests for the /health and /metrics endpoints."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_returns_200(client: AsyncClient) -> None:
    """GET /health returns HTTP 200."""
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
async def test_health_version(client: AsyncClient) -> None:
    """GET /health returns the expected version string."""
    response = await client.get("/health")
    assert response.json()["version"] == "0.1.0"


@pytest.mark.asyncio
async def test_health_status_valid_value(client: AsyncClient) -> None:
    """Overall status is one of the known states."""
    response = await client.get("/health")
    status = response.json()["status"]
    assert status in {"healthy", "degraded", "unhealthy"}


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
