"""Tests for omniscience_core Settings."""

from __future__ import annotations

import pydantic
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
    "GRAPH_BITEMPORAL",
    "OMNISCIENCE_SECRET_KEY",
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


# ---------------------------------------------------------------------------
# GRAPH_BITEMPORAL rollout flag (ADR-0008 §8, issue #317)
# ---------------------------------------------------------------------------


def test_graph_bitemporal_enabled_by_default(clean_env: None) -> None:
    """ADR-0008 §8 is fully implemented; the write path is on by default (#317).

    The bitemporal triple (``valid_from`` / ``valid_to`` / ``recorded_at``)
    is the canonical write path: removals end-date instead of hard-deleting
    and ``as_of`` reads traverse the version chain.  Existing flag-off
    deployments can still opt out via ``GRAPH_BITEMPORAL=disabled``.
    """
    s = Settings(_env_file=None)
    assert s.graph_bitemporal == "enabled"


def test_graph_bitemporal_off_override_still_honoured(clean_env: None) -> None:
    """Back-compat: an explicit ``disabled`` keeps PR #104's legacy writer."""
    s = Settings(_env_file=None, graph_bitemporal="disabled")
    assert s.graph_bitemporal == "disabled"


def test_graph_bitemporal_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag is overridable from the environment (operator opt-out path)."""
    monkeypatch.setenv("GRAPH_BITEMPORAL", "disabled")
    s = Settings(_env_file=None)
    assert s.graph_bitemporal == "disabled"


# ---------------------------------------------------------------------------
# secret_key fail-fast on known README/docs placeholder values
#
# The quickstart docs (README.md, docs/integrations/*.md, .mcp/pulsemcp.json)
# tell operators to `cat > .env` with OMNISCIENCE_SECRET_KEY set to a
# `change-me-...` placeholder before swapping in a real value. Nothing
# currently rejects the placeholder if it survives into a real deployment,
# so the app boots with a guessable, publicly-documented secret key. Settings
# must refuse to construct when secret_key is one of these known placeholders.
# ---------------------------------------------------------------------------

_KNOWN_PLACEHOLDER_SECRET_KEYS = (
    # README.md, docs/integrations/claude-code.md, docs/integrations/multiqlti.md
    "change-me-32-char-secret-key-here",
    # .mcp/pulsemcp.json
    "change-me-32-chars",
)


def test_secret_key_none_by_default(clean_env: None) -> None:
    """secret_key is optional for local/dev use and defaults to unset."""
    s = Settings(_env_file=None)
    assert s.secret_key is None


@pytest.mark.parametrize("placeholder", _KNOWN_PLACEHOLDER_SECRET_KEYS)
def test_secret_key_rejects_known_placeholder(
    monkeypatch: pytest.MonkeyPatch, placeholder: str
) -> None:
    """Booting with a documented placeholder secret must fail fast."""
    monkeypatch.setenv("OMNISCIENCE_SECRET_KEY", placeholder)
    with pytest.raises(pydantic.ValidationError):
        Settings(_env_file=None)


def test_secret_key_env_var_is_omniscience_secret_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settings reads the exact env var name the quickstart docs instruct."""
    real_value = "9f3a7c1e5b8d2046af90c3e7b1d5f8a2"
    monkeypatch.setenv("OMNISCIENCE_SECRET_KEY", real_value)
    s = Settings(_env_file=None)
    assert s.secret_key == real_value


def test_secret_key_accepts_non_placeholder_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real, non-blocklisted secret key boots normally."""
    monkeypatch.setenv("OMNISCIENCE_SECRET_KEY", "9f3a7c1e5b8d2046af90c3e7b1d5f8a2")
    s = Settings(_env_file=None)
    assert s.secret_key == "9f3a7c1e5b8d2046af90c3e7b1d5f8a2"
