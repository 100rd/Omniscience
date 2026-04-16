"""Tests for omniscience_core Settings."""

from __future__ import annotations

import pytest
from omniscience_core.config import Settings


def test_defaults_load() -> None:
    """Settings can be instantiated without any environment overrides."""
    s = Settings()
    assert s.app_name == "omniscience"
    assert s.app_version == "0.1.0"
    assert s.environment == "development"
    assert s.log_level == "INFO"
    assert s.embedding_provider == "ollama"
    assert s.otlp_endpoint is None


def test_default_database_url() -> None:
    """Default DATABASE_URL points to the local Docker Compose Postgres."""
    s = Settings()
    assert "localhost" in s.database_url
    assert "omniscience" in s.database_url


def test_default_nats_url() -> None:
    """Default NATS_URL points to local NATS."""
    s = Settings()
    assert s.nats_url == "nats://localhost:4222"


def test_default_ollama_url() -> None:
    """Default OLLAMA_URL points to local Ollama."""
    s = Settings()
    assert s.ollama_url == "http://localhost:11434"


def test_override_via_kwargs() -> None:
    """Settings values can be overridden by passing keyword arguments."""
    s = Settings(log_level="DEBUG", environment="staging", otlp_endpoint="http://otel:4317")
    assert s.log_level == "DEBUG"
    assert s.environment == "staging"
    assert s.otlp_endpoint == "http://otel:4317"


def test_override_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings values are loaded from environment variables."""
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    monkeypatch.setenv("APP_NAME", "test-svc")
    s = Settings()
    assert s.log_level == "ERROR"
    assert s.app_name == "test-svc"


def test_otlp_endpoint_none_by_default() -> None:
    """OTLP endpoint is None unless explicitly set (keeps telemetry as no-op in dev)."""
    s = Settings()
    assert s.otlp_endpoint is None
