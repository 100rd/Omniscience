"""Shared pytest fixtures for the Omniscience test suite."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_core.config import Settings
from omniscience_server.app import create_app


@pytest.fixture()
def test_settings() -> Settings:
    """Return a Settings instance with safe test defaults."""
    return Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )


@pytest.fixture()
def app(test_settings: Settings) -> FastAPI:
    """Return a FastAPI app configured for testing."""
    return create_app(settings=test_settings)


@pytest_asyncio.fixture()
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Async HTTP client bound to the test app via ASGI transport."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
