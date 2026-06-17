"""Tests for omniscience_core Settings."""

from __future__ import annotations

import pytest
from omniscience_core.config import Settings

# Environment variables that pydantic-settings would pick up and that could
# cause these default-assertion tests to read a real deployment value instead
# of the code default.  We clear them for every default-testing function so
# the assertions are deterministic regardless of the shell / CI environment.
_DEFAULT_TEST_ENV_VARS = (
    "LOG_LEVEL",
    "DATABASE_URL",
    "NATS_URL",
    "OLLAMA_URL",
    "OTLP_ENDPOINT",
    "APP_NAME",
    "APP_VERSION",
    "ENVIRONMENT",
    "EMBEDDING_PROVIDER",
)


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove env vars that shadow Settings defaults."""
    for var in _DEFAULT_TEST_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_defaults_load(clean_env: None) -> None:
    """Settings can be instantiated without any environment overrides."""
    s = Settings(_env_file=None)
    assert s.app_name == "omniscience"
    assert s.app_version == "0.2.0"
    assert s.environment == "development"
    assert s.log_level == "INFO"
    assert s.embedding_provider == "ollama"
    assert s.otlp_endpoint is None


def test_default_database_url(clean_env: None) -> None:
    """Default DATABASE_URL points to the local Docker Compose Postgres."""
    s = Settings(_env_file=None)
    assert "localhost" in s.database_url
    assert "omniscience" in s.database_url


def test_default_nats_url(clean_env: None) -> None:
    """Default NATS_URL points to local NATS."""
    s = Settings(_env_file=None)
    assert s.nats_url == "nats://localhost:4222"


def test_default_ollama_url(clean_env: None) -> None:
    """Default OLLAMA_URL points to local Ollama."""
    s = Settings(_env_file=None)
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
    s = Settings(_env_file=None)
    assert s.log_level == "ERROR"
    assert s.app_name == "test-svc"


def test_otlp_endpoint_none_by_default(clean_env: None) -> None:
    """OTLP endpoint is None unless explicitly set (keeps telemetry as no-op in dev)."""
    s = Settings(_env_file=None)
    assert s.otlp_endpoint is None


def test_background_workers_enabled_by_default(clean_env: None) -> None:
    """Discovery and reconcile workers default ON so the full profile is unchanged.

    The 'lite' deployment profile (issue #319) flips these to False to shed
    background load; the defaults must remain True so a stock ``docker compose
    up`` keeps the v0.2 worker posture verbatim.
    """
    s = Settings(_env_file=None)
    assert s.discovery_enabled is True
    assert s.reconcile_enabled is True


def test_background_workers_can_be_disabled_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lite profile disables background workers through env vars."""
    monkeypatch.setenv("DISCOVERY_ENABLED", "false")
    monkeypatch.setenv("RECONCILE_ENABLED", "false")
    s = Settings(_env_file=None)
    assert s.discovery_enabled is False
    assert s.reconcile_enabled is False
