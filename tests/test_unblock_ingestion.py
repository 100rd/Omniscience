"""Tests for Issue #85 — wire sync endpoint to NATS and runtime secrets resolution.

Coverage:
  - sync endpoint publishes DocumentChangeEvent to NATS (mock)
  - sync endpoint returns 202 when NATS unavailable
  - sync endpoint publishes to correct subject (ingest.changes.{source_type})
  - sync event has correct fields (source_id, source_type, action, external_id)
  - sync endpoint returns 404 when source not found
  - SecretsResolver: None/empty returns {}
  - SecretsResolver: env: prefix reads matching env vars
  - SecretsResolver: env: prefix with no matches returns {}
  - SecretsResolver: env: single var reads correct variable
  - SecretsResolver: env: single var missing raises ConfigError
  - SecretsResolver: env: single var with custom key name
  - SecretsResolver: file: valid JSON file
  - SecretsResolver: file: file not found raises ConfigError
  - SecretsResolver: file: invalid JSON raises ConfigError
  - SecretsResolver: file: non-dict JSON raises ConfigError
  - SecretsResolver: file: non-string value raises ConfigError
  - SecretsResolver: unrecognised format raises ConfigError
  - SecretsResolver: empty string returns {}
  - worker uses SecretsResolver (default instance created automatically)
  - worker accepts injected SecretsResolver
  - worker passes resolved secrets to pipeline
  - worker handles secrets resolution failure gracefully
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_core.config import Settings
from omniscience_core.db.models import Source, SourceStatus, SourceType
from omniscience_core.errors import ConfigError
from omniscience_core.secrets import SecretsResolver
from omniscience_server.app import create_app
from omniscience_server.ingestion.events import DocumentChangeEvent, ProcessResult
from omniscience_server.ingestion.worker import IngestionWorker

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )


# Shared workspace UUID so the mocked token's workspace_id matches the
# mocked source's tenant_id — the sync endpoint is workspace-scoped and
# returns 404 on a tenant mismatch (see rest/sources._get_source_or_404).
_WORKSPACE_ID: uuid.UUID = uuid.uuid4()


def _make_source(
    source_type: SourceType = SourceType.git,
) -> Source:
    src: Source = MagicMock(spec=Source)
    src.id = uuid.uuid4()
    src.type = source_type
    src.name = "test-repo"
    src.config = {}
    src.secrets_ref = None
    src.status = SourceStatus.active
    src.last_sync_at = None
    src.last_error = None
    src.tenant_id = _WORKSPACE_ID
    return src


def _make_db_session_for_sync(source: Source | None) -> AsyncMock:
    """Build a fake async DB session for the sync endpoint.

    The sync endpoint calls session.get() (for _get_source_or_404) and
    then session.add / flush / refresh / commit for the IngestionRun.
    """
    session = AsyncMock()

    # session.get() is used by _get_source_or_404
    session.get = AsyncMock(return_value=source)
    session.add = MagicMock()
    session.flush = AsyncMock()

    # refresh populates the run's id field
    async def _refresh(obj: Any) -> None:
        obj.id = uuid.uuid4()

    session.refresh = _refresh
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _make_token_session(token: Any) -> AsyncMock:
    """Build a fake session that handles both token lookup and source lookup."""
    session = AsyncMock()

    async def _execute(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.all.return_value = [token]
        result.scalars.return_value.first.return_value = token
        result.scalar_one.return_value = 0
        return result

    session.execute = _execute
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.delete = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _make_app_with_source(source: Source | None = None) -> FastAPI:
    """Build an app whose DB session returns *source* on .get() calls."""
    from omniscience_core.auth.tokens import generate_token, hash_token
    from omniscience_core.db.models import ApiToken

    # Build a write-scoped API token
    pt, prefix = generate_token("test")
    hashed = hash_token(pt)
    token: ApiToken = MagicMock(spec=ApiToken)
    token.id = uuid.uuid4()
    token.token_prefix = prefix
    token.hashed_token = hashed
    token.scopes = ["sources:write", "sources:read"]
    token.expires_at = None
    token.is_active = True
    token.last_used_at = None
    # Workspace-scoped token: must match the source's tenant_id so the
    # workspace-scoped sync endpoint resolves the source (not 404).
    token.workspace_id = _WORKSPACE_ID

    app = create_app(settings=_make_settings())

    token_session = _make_token_session(token)
    source_session = _make_db_session_for_sync(source)

    call_count = 0

    def _factory() -> AsyncMock:
        nonlocal call_count
        call_count += 1
        # First call is auth token lookup, subsequent calls are source/run operations
        if call_count == 1:
            return token_session
        return source_session

    app.state.db_session_factory = _factory
    return app, pt


async def _client_for(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


def _make_worker(
    events: list[DocumentChangeEvent] | None = None,
    secrets_resolver: SecretsResolver | None = None,
    source_secrets_ref: str | None = None,
) -> tuple[IngestionWorker, AsyncMock, AsyncMock]:
    """Build an IngestionWorker with mocked dependencies.

    Returns (worker, mock_pipeline_run, mock_connector_registry).
    """
    from omniscience_connectors.registry import ConnectorRegistry

    # Mock consumer that yields provided events then stops
    mock_consumer = AsyncMock()
    mock_consumer.stop = MagicMock()

    msgs = []
    for ev in events or []:
        msg = AsyncMock()
        msg.payload = ev
        msg.ack = AsyncMock()
        msg.nak = AsyncMock()
        msgs.append(msg)

    async def _aiter(_self: Any) -> Any:
        for m in msgs:
            yield m

    mock_consumer.__aiter__ = _aiter

    from pydantic import BaseModel as _BaseModel

    class _EmptyConfig(_BaseModel):
        pass

    connector = MagicMock()
    connector.config_schema = _EmptyConfig
    mock_registry: ConnectorRegistry = MagicMock(spec=ConnectorRegistry)
    mock_registry.get = MagicMock(return_value=connector)

    embedding_provider = MagicMock()
    embedding_provider.model_name = "test-model"
    embedding_provider.provider_name = "test-provider"

    index_writer = MagicMock()

    # Session factory: return a Source row with a non-null tenant_id so
    # workspace resolution succeeds.  These tests stub IngestionPipeline.run,
    # so the only DB interaction is the worker's source lookup.
    fake_source = MagicMock()
    fake_source.id = uuid.uuid4()
    fake_source.name = "unblock-test-source"
    fake_source.tenant_id = uuid.uuid4()
    fake_source.config = {}
    fake_source.secrets_ref = source_secrets_ref

    inner_session = AsyncMock()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none = MagicMock(return_value=fake_source)
    inner_session.execute = AsyncMock(return_value=scalar_result)

    factory_cm = MagicMock()
    factory_cm.__aenter__ = AsyncMock(return_value=inner_session)
    factory_cm.__aexit__ = AsyncMock(return_value=False)

    session_factory = MagicMock(return_value=factory_cm)

    worker = IngestionWorker(
        queue_consumer=mock_consumer,
        connector_registry=mock_registry,
        embedding_provider=embedding_provider,
        index_writer=index_writer,
        session_factory=session_factory,
        secrets_resolver=secrets_resolver,
    )

    mock_pipeline_run = AsyncMock(
        return_value=ProcessResult(
            source_id=uuid.uuid4(),
            external_id="test.py",
            action="created",
            duration_ms=10.0,
        )
    )
    return worker, mock_pipeline_run, mock_registry


# ===========================================================================
# PART 1: Sync endpoint → NATS wiring
# ===========================================================================


@pytest.mark.asyncio
async def test_sync_returns_202_with_run_id() -> None:
    """POST /sync returns 202 with a valid run_id UUID."""
    source = _make_source()
    app, token = _make_app_with_source(source)

    async with await _client_for(app) as client:
        resp = await client.post(
            f"/api/v1/sources/{source.id}/sync",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 202
    body = resp.json()
    assert "run_id" in body
    uuid.UUID(body["run_id"])  # must be a valid UUID


@pytest.mark.asyncio
async def test_sync_publishes_to_nats_when_available() -> None:
    """sync endpoint calls QueueProducer.publish when NATS is wired."""
    source = _make_source(SourceType.git)
    app, token = _make_app_with_source(source)

    mock_js = AsyncMock()
    mock_js.publish = AsyncMock()
    mock_nats = MagicMock()
    mock_nats.jetstream = mock_js
    app.state.nats = mock_nats

    with patch(
        "omniscience_server.rest.sources.QueueProducer.publish",
        new_callable=AsyncMock,
    ) as mock_publish:
        async with await _client_for(app) as client:
            resp = await client.post(
                f"/api/v1/sources/{source.id}/sync",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert resp.status_code == 202
    mock_publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_publishes_to_correct_subject() -> None:
    """sync endpoint publishes to ingest.changes.{source_type}."""
    source = _make_source(SourceType.fs)
    app, token = _make_app_with_source(source)

    mock_js = AsyncMock()
    mock_nats = MagicMock()
    mock_nats.jetstream = mock_js
    app.state.nats = mock_nats

    with patch(
        "omniscience_server.rest.sources.QueueProducer.publish",
        new_callable=AsyncMock,
    ) as mock_publish:
        async with await _client_for(app) as client:
            await client.post(
                f"/api/v1/sources/{source.id}/sync",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert mock_publish.call_count == 1
    kwargs = mock_publish.call_args.kwargs
    assert kwargs["subject"] == f"ingest.changes.{source.type}"


@pytest.mark.asyncio
async def test_sync_event_has_correct_fields() -> None:
    """Published DocumentChangeEvent carries source_id, source_type, action='updated'."""
    source = _make_source(SourceType.git)
    app, token = _make_app_with_source(source)

    mock_js = AsyncMock()
    mock_nats = MagicMock()
    mock_nats.jetstream = mock_js
    app.state.nats = mock_nats

    with patch(
        "omniscience_server.rest.sources.QueueProducer.publish",
        new_callable=AsyncMock,
    ) as mock_publish:
        async with await _client_for(app) as client:
            await client.post(
                f"/api/v1/sources/{source.id}/sync",
                headers={"Authorization": f"Bearer {token}"},
            )

    event: DocumentChangeEvent = mock_publish.call_args.kwargs["payload"]
    assert event.source_id == source.id
    assert event.source_type == str(source.type)
    assert event.action == "updated"
    assert event.external_id == "*"


@pytest.mark.asyncio
async def test_sync_still_202_when_nats_unavailable() -> None:
    """sync returns 202 even when NATS is not wired (nats=None)."""
    source = _make_source()
    app, token = _make_app_with_source(source)
    app.state.nats = None  # explicitly no NATS

    async with await _client_for(app) as client:
        resp = await client.post(
            f"/api/v1/sources/{source.id}/sync",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_sync_still_202_when_nats_has_no_jetstream() -> None:
    """sync returns 202 when NATS connection lacks a jetstream attribute."""
    source = _make_source()
    app, token = _make_app_with_source(source)

    mock_nats = MagicMock()
    mock_nats.jetstream = None
    app.state.nats = mock_nats

    async with await _client_for(app) as client:
        resp = await client.post(
            f"/api/v1/sources/{source.id}/sync",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_sync_returns_404_for_unknown_source() -> None:
    """sync returns 404 when source_id does not exist in DB."""
    app, token = _make_app_with_source(source=None)

    async with await _client_for(app) as client:
        resp = await client.post(
            f"/api/v1/sources/{uuid.uuid4()}/sync",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "source_not_found"


@pytest.mark.asyncio
async def test_sync_publish_failure_does_not_break_202() -> None:
    """When QueueProducer.publish raises, the endpoint still returns 202."""
    source = _make_source()
    app, token = _make_app_with_source(source)

    mock_js = AsyncMock()
    mock_nats = MagicMock()
    mock_nats.jetstream = mock_js
    app.state.nats = mock_nats

    with patch(
        "omniscience_server.rest.sources.QueueProducer.publish",
        new_callable=AsyncMock,
        side_effect=RuntimeError("NATS connection lost"),
    ):
        async with await _client_for(app) as client:
            resp = await client.post(
                f"/api/v1/sources/{source.id}/sync",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert resp.status_code == 202


# ===========================================================================
# PART 2: SecretsResolver unit tests
# ===========================================================================


class TestSecretsResolverNoneAndEmpty:
    def test_none_returns_empty_dict(self) -> None:
        resolver = SecretsResolver()
        assert resolver.resolve(None) == {}

    def test_empty_string_returns_empty_dict(self) -> None:
        resolver = SecretsResolver()
        assert resolver.resolve("") == {}

    def test_unrecognised_prefix_raises_config_error(self) -> None:
        resolver = SecretsResolver()
        with pytest.raises(ConfigError, match="Unrecognised secrets_ref format"):
            resolver.resolve("vault:secret/data/myapp")

    def test_plain_string_raises_config_error(self) -> None:
        resolver = SecretsResolver()
        with pytest.raises(ConfigError):
            resolver.resolve("justaplainstring")


class TestSecretsResolverEnvPrefix:
    def test_env_prefix_returns_matching_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MYAPP_TOKEN", "tok123")
        monkeypatch.setenv("MYAPP_ORG", "acme")
        monkeypatch.setenv("OTHER_VAR", "ignore")

        resolver = SecretsResolver()
        result = resolver.resolve("env:MYAPP_")

        assert result["TOKEN"] == "tok123"
        assert result["ORG"] == "acme"
        assert "OTHER_VAR" not in result
        assert "VAR" not in result

    def test_env_prefix_no_matches_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Clear any vars that start with ZZZNOMATCH_
        for key in list(os.environ.keys()):
            if key.startswith("ZZZNOMATCH_"):
                monkeypatch.delenv(key)

        resolver = SecretsResolver()
        result = resolver.resolve("env:ZZZNOMATCH_")
        assert result == {}

    def test_env_prefix_strips_prefix_from_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DB_HOST", "localhost")
        monkeypatch.setenv("DB_PORT", "5432")

        resolver = SecretsResolver()
        result = resolver.resolve("env:DB_")

        assert "HOST" in result
        assert "PORT" in result
        assert result["HOST"] == "localhost"

    def test_env_prefix_exact_prefix_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Variables not matching the prefix are excluded; matching ones are included."""
        monkeypatch.setenv("ABC_KEY", "val1")
        monkeypatch.setenv("ABC_TOKEN", "val2")
        # "ABCD_KEY" does NOT start with "ABC_" (4th char is 'D' != '_')
        monkeypatch.setenv("ABCD_KEY", "val3")

        resolver = SecretsResolver()
        result = resolver.resolve("env:ABC_")

        assert result["KEY"] == "val1"
        assert result["TOKEN"] == "val2"
        # ABCD_KEY doesn't start with ABC_, so it must not appear
        assert "D_KEY" not in result


class TestSecretsResolverEnvSingle:
    def test_env_single_reads_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_SECRET_TOKEN", "supersecret")

        resolver = SecretsResolver()
        result = resolver.resolve("env:MY_SECRET_TOKEN=token")

        assert result == {"token": "supersecret"}

    def test_env_single_custom_key_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_abc")

        resolver = SecretsResolver()
        result = resolver.resolve("env:GH_TOKEN=github_token")

        assert result == {"github_token": "ghp_abc"}

    def test_env_single_missing_var_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DEFINITELY_NOT_SET_XYZ123", raising=False)

        resolver = SecretsResolver()
        with pytest.raises(ConfigError, match="DEFINITELY_NOT_SET_XYZ123"):
            resolver.resolve("env:DEFINITELY_NOT_SET_XYZ123=key")

    def test_env_single_error_message_includes_var_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MISSING_VAR_ABC", raising=False)

        resolver = SecretsResolver()
        with pytest.raises(ConfigError) as exc_info:
            resolver.resolve("env:MISSING_VAR_ABC=mykey")

        assert "MISSING_VAR_ABC" in str(exc_info.value)

    def test_env_single_key_only_used_in_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RAW_VAR", "rawvalue")

        resolver = SecretsResolver()
        result = resolver.resolve("env:RAW_VAR=friendly_name")

        assert "friendly_name" in result
        assert "RAW_VAR" not in result


class TestSecretsResolverFile:
    def test_file_valid_json(self, tmp_path: Path) -> None:
        secrets_file = tmp_path / "secrets.json"
        secrets_file.write_text(
            json.dumps({"api_key": "key123", "token": "tok456"}), encoding="utf-8"
        )

        resolver = SecretsResolver()
        result = resolver.resolve(f"file:{secrets_file}")

        assert result == {"api_key": "key123", "token": "tok456"}

    def test_file_not_found_raises_config_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.json"

        resolver = SecretsResolver()
        with pytest.raises(ConfigError, match="Cannot read secrets file"):
            resolver.resolve(f"file:{missing}")

    def test_file_invalid_json_raises_config_error(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not valid json {{{", encoding="utf-8")

        resolver = SecretsResolver()
        with pytest.raises(ConfigError, match="not valid JSON"):
            resolver.resolve(f"file:{bad_file}")

    def test_file_json_array_raises_config_error(self, tmp_path: Path) -> None:
        array_file = tmp_path / "array.json"
        array_file.write_text(json.dumps(["a", "b", "c"]), encoding="utf-8")

        resolver = SecretsResolver()
        with pytest.raises(ConfigError, match="JSON object"):
            resolver.resolve(f"file:{array_file}")

    def test_file_non_string_value_raises_config_error(self, tmp_path: Path) -> None:
        bad_val = tmp_path / "nonstr.json"
        bad_val.write_text(json.dumps({"key": 42}), encoding="utf-8")

        resolver = SecretsResolver()
        with pytest.raises(ConfigError, match="must be a string"):
            resolver.resolve(f"file:{bad_val}")

    def test_file_empty_object_returns_empty_dict(self, tmp_path: Path) -> None:
        empty_file = tmp_path / "empty.json"
        empty_file.write_text("{}", encoding="utf-8")

        resolver = SecretsResolver()
        result = resolver.resolve(f"file:{empty_file}")
        assert result == {}

    def test_file_path_included_in_error_message(self, tmp_path: Path) -> None:
        missing = tmp_path / "gone.json"

        resolver = SecretsResolver()
        with pytest.raises(ConfigError) as exc_info:
            resolver.resolve(f"file:{missing}")

        assert str(missing) in str(exc_info.value)


# ===========================================================================
# PART 3: Worker secrets wiring
# ===========================================================================


@pytest.mark.asyncio
async def test_worker_creates_default_secrets_resolver() -> None:
    """IngestionWorker creates a SecretsResolver automatically when none provided."""
    worker, _, _ = _make_worker()
    assert isinstance(worker._secrets_resolver, SecretsResolver)


@pytest.mark.asyncio
async def test_worker_accepts_injected_resolver() -> None:
    """IngestionWorker uses the provided SecretsResolver instance."""
    custom_resolver = SecretsResolver()
    worker, _, _ = _make_worker(secrets_resolver=custom_resolver)
    assert worker._secrets_resolver is custom_resolver


@pytest.mark.asyncio
async def test_worker_passes_empty_secrets_when_no_secrets_ref() -> None:
    """process_document passes secrets={} when event has no secrets_ref."""
    source_id = uuid.uuid4()
    event = DocumentChangeEvent(
        source_id=source_id,
        source_type="git",
        external_id="README.md",
        uri="file://README.md",
        action="created",
    )
    worker, mock_run, _ = _make_worker(events=[event])

    with patch(
        "omniscience_server.ingestion.worker.IngestionPipeline.run_ref",
        new=mock_run,
    ):
        await worker.process_document(event)

    mock_run.assert_awaited_once()
    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs["secrets"] == {}


@pytest.mark.asyncio
async def test_worker_passes_resolved_secrets_to_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """process_document resolves source.secrets_ref and passes secrets to pipeline.run_ref.

    Secrets now come from Source.secrets_ref (server-side), not from the event payload.
    """
    monkeypatch.setenv("WORKER_TEST_KEY", "worker_secret_value")

    source_id = uuid.uuid4()
    event = DocumentChangeEvent(
        source_id=source_id,
        source_type="git",
        external_id="main.py",
        uri="file://main.py",
        action="updated",
    )

    worker, mock_run, _ = _make_worker(
        events=[event],
        source_secrets_ref="env:WORKER_TEST_KEY=api_key",
    )
    mock_run.return_value = ProcessResult(
        source_id=source_id,
        external_id="main.py",
        action="updated",
        duration_ms=5.0,
    )

    with patch(
        "omniscience_server.ingestion.worker.IngestionPipeline.run_ref",
        new=mock_run,
    ):
        await worker.process_document(event)

    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs["secrets"] == {"api_key": "worker_secret_value"}


@pytest.mark.asyncio
async def test_worker_uses_custom_resolver_for_secrets() -> None:
    """process_document delegates to the injected SecretsResolver."""
    mock_resolver = MagicMock(spec=SecretsResolver)
    mock_resolver.resolve = MagicMock(return_value={"injected_key": "injected_val"})

    source_id = uuid.uuid4()
    event = DocumentChangeEvent(
        source_id=source_id,
        source_type="git",
        external_id="app.py",
        uri="file://app.py",
        action="created",
    )

    worker, mock_run, _ = _make_worker(events=[event], secrets_resolver=mock_resolver)
    mock_run.return_value = ProcessResult(
        source_id=source_id,
        external_id="app.py",
        action="created",
        duration_ms=1.0,
    )

    with patch(
        "omniscience_server.ingestion.worker.IngestionPipeline.run_ref",
        new=mock_run,
    ):
        result = await worker.process_document(event)

    # Resolver is called with source.secrets_ref (None in this case, from fake_source).
    mock_resolver.resolve.assert_called_once_with(None)
    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs["secrets"] == {"injected_key": "injected_val"}
    assert result.action == "created"
