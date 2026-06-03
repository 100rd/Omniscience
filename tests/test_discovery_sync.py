"""Tests for the discovery-based sync path in the ingestion worker.

Spec: docs/specs/discovery-sync-worker.md

Test matrix:
  1. Sync event → discover yields N refs → N pipeline.run_ref calls, N docs indexed.
  2. Sync event → connector.discover raises NotImplementedError → falls back to single-doc.
  3. Non-sync event → pipeline receives validated config (not None) — regression for gap #1.
  4. Source.config fails validation → ProcessResult(action="error"), no index writes.
  5. Pipeline: run_ref passes ref.metadata intact to connector.fetch (not a rebuilt bare ref).
  6. Pipeline: existing run() still works (backwards compat).
  7. Bounded concurrency: semaphore limits concurrent fetches to _DISCOVERY_CONCURRENCY.
  8. Secrets: source.secrets_ref="env:OMNI_K8S_QBIQ_" resolves to {token, ca_cert_b64}.
  9. ACL: workspace_id is sourced from Source.tenant_id, never from event payload.
 10. _is_sync_marker: uri.startswith("sync://") and external_id=="*" both detected.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from omniscience_connectors.base import DocumentRef, FetchedDocument
from omniscience_server.ingestion.events import DocumentChangeEvent
from omniscience_server.ingestion.pipeline import IngestionPipeline
from omniscience_server.ingestion.worker import IngestionWorker, _is_sync_marker
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Shared config schema
# ---------------------------------------------------------------------------


class _EmptyConfig(BaseModel):
    """Placeholder config for connectors that need no configuration."""


class _RequiredConfig(BaseModel):
    """Config model with a required field — used to test validation errors."""

    must_have: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source_id() -> uuid.UUID:
    return uuid.uuid4()


def _make_sync_event(
    source_id: uuid.UUID | None = None,
    source_type: str = "k8s",
) -> DocumentChangeEvent:
    sid = source_id or _source_id()
    return DocumentChangeEvent(
        source_id=sid,
        source_type=source_type,
        external_id="*",
        uri=f"sync://{sid}",
        action="updated",
    )


def _make_single_event(
    source_id: uuid.UUID | None = None,
    source_type: str = "git",
    external_id: str = "src/main.py",
    uri: str = "file://src/main.py",
) -> DocumentChangeEvent:
    return DocumentChangeEvent(
        source_id=source_id or _source_id(),
        source_type=source_type,
        external_id=external_id,
        uri=uri,
        action="updated",
    )


def _make_fetched(ref: DocumentRef, content: bytes = b"resource-content") -> FetchedDocument:
    return FetchedDocument(ref=ref, content_bytes=content, content_type="application/json")


def _make_connector_with_discover(
    refs: list[DocumentRef],
    content: bytes = b"resource-content",
) -> MagicMock:
    """Connector that yields ``refs`` from discover() and fetches each."""
    connector = MagicMock()
    connector.config_schema = _EmptyConfig

    async def _discover(_config: Any, _secrets: Any) -> AsyncIterator[DocumentRef]:
        for ref in refs:
            yield ref

    connector.discover = _discover

    async def _fetch(_config: Any, _secrets: Any, ref: DocumentRef) -> FetchedDocument:
        return _make_fetched(ref, content)

    connector.fetch = AsyncMock(side_effect=_fetch)
    return connector


def _make_connector_no_discover(content: bytes = b"hello") -> MagicMock:
    """Single-doc connector: discover() raises NotImplementedError."""
    connector = MagicMock()
    connector.config_schema = _EmptyConfig
    connector.discover = MagicMock(side_effect=NotImplementedError)

    async def _discover(_config: Any, _secrets: Any) -> AsyncIterator[DocumentRef]:
        raise NotImplementedError
        yield  # make it an async generator type-wise  # pragma: no cover

    connector.discover = _discover

    ref = DocumentRef(external_id="*", uri="sync://test")
    connector.fetch = AsyncMock(return_value=_make_fetched(ref, content))
    return connector


def _make_embedding_provider() -> MagicMock:
    provider = MagicMock()
    provider.dim = 4
    provider.model_name = "test-model"
    provider.provider_name = "test-provider"
    provider.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])
    return provider


def _make_index_writer(action: str = "created") -> MagicMock:
    from omniscience_server.ingestion.pipeline import IndexWriterProtocol

    result = MagicMock()
    result.action = action
    result.chunks_written = 1
    result.document_id = uuid.uuid4()

    writer = MagicMock(spec=IndexWriterProtocol)
    writer.upsert_document = AsyncMock(return_value=result)
    writer.upsert_graph = AsyncMock(return_value=None)
    writer.tombstone = AsyncMock(return_value=True)
    return writer


def _make_source(
    tenant_id: uuid.UUID | None = None,
    config: dict[str, Any] | None = None,
    secrets_ref: str | None = None,
) -> Any:
    src = MagicMock()
    src.id = uuid.uuid4()
    src.name = "test-discovery-source"
    src.tenant_id = tenant_id if tenant_id is not None else uuid.uuid4()
    src.config = config if config is not None else {}
    src.secrets_ref = secrets_ref
    return src


def _make_session_factory(source: Any = None) -> MagicMock:
    """Session factory that always returns ``source`` from scalar_one_or_none."""
    inner_session = AsyncMock()
    inner_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=source))
    )

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=inner_session)
    cm.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock()
    factory.return_value = cm
    factory.session = inner_session
    return factory


def _make_worker(
    connector: Any,
    source: Any = None,
    writer: Any = None,
) -> IngestionWorker:
    """Build a minimal worker with the given connector and source."""
    registry = MagicMock()
    registry.get = MagicMock(return_value=connector)

    src = source if source is not None else _make_source()
    session_factory = _make_session_factory(source=src)

    worker = IngestionWorker(
        queue_consumer=MagicMock(),
        connector_registry=registry,
        embedding_provider=_make_embedding_provider(),
        index_writer=writer or _make_index_writer(),
        session_factory=session_factory,
    )
    return worker


# ---------------------------------------------------------------------------
# 1. Sync event — discover yields N refs → N docs indexed
# ---------------------------------------------------------------------------


class TestDiscoverySyncFanOut:
    @pytest.mark.asyncio
    async def test_discover_three_refs_indexes_three_docs(self) -> None:
        """A sync event with 3 discovered refs produces 3 upsert_document calls."""
        refs = [
            DocumentRef(
                external_id=f"k8s/Deployment/{i}",
                uri=f"k8s://Deployment/{i}",
                metadata={"kind": "Deployment", "name": f"deploy-{i}"},
            )
            for i in range(3)
        ]
        connector = _make_connector_with_discover(refs)
        writer = _make_index_writer()
        worker = _make_worker(connector, writer=writer)

        event = _make_sync_event()
        with patch.object(worker._run_tracker, "start", AsyncMock(return_value=uuid.uuid4())):
            result = await worker.process_document(event)

        assert result.action in ("created", "updated")
        assert writer.upsert_document.await_count == 3

    @pytest.mark.asyncio
    async def test_discover_zero_refs_returns_updated(self) -> None:
        """A connector that discovers no refs still returns an aggregated result."""
        connector = _make_connector_with_discover([])
        writer = _make_index_writer()
        worker = _make_worker(connector, writer=writer)

        event = _make_sync_event()
        result = await worker.process_document(event)

        assert result.action == "updated"
        writer.upsert_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_all_ref_errors_returns_error(self) -> None:
        """When every ref fails to index, the aggregate action is 'error'."""
        refs = [
            DocumentRef(external_id="k8s/A/1", uri="k8s://A/1"),
        ]
        connector = _make_connector_with_discover(refs)
        writer = _make_index_writer()
        writer.upsert_document = AsyncMock(side_effect=RuntimeError("index failure"))
        worker = _make_worker(connector, writer=writer)

        event = _make_sync_event()
        result = await worker.process_document(event)

        assert result.action == "error"


# ---------------------------------------------------------------------------
# 2. Sync event → NotImplementedError → single-doc fallback
# ---------------------------------------------------------------------------


class TestDiscoverySyncFallback:
    @pytest.mark.asyncio
    async def test_no_discover_falls_back_to_single_doc(self) -> None:
        """Connectors without discover() fall back to single-document processing."""
        connector = _make_connector_no_discover()
        writer = _make_index_writer()
        worker = _make_worker(connector, writer=writer)

        event = _make_sync_event()
        result = await worker.process_document(event)

        assert result.action in ("created", "updated", "unchanged")
        writer.upsert_document.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3. Non-sync event receives validated config (gap #1 regression)
# ---------------------------------------------------------------------------


class TestNonSyncEventValidatedConfig:
    @pytest.mark.asyncio
    async def test_non_sync_event_passes_config_to_pipeline(self) -> None:
        """Non-sync events must receive validated config, not None."""
        captured: dict[str, Any] = {}

        connector = MagicMock()
        connector.config_schema = _EmptyConfig

        async def _fetch(_config: Any, _secrets: Any, ref: DocumentRef) -> FetchedDocument:
            captured["config"] = _config
            return _make_fetched(ref)

        connector.fetch = AsyncMock(side_effect=_fetch)

        writer = _make_index_writer()
        worker = _make_worker(connector, writer=writer)

        event = _make_single_event()
        await worker.process_document(event)

        assert "config" in captured, "fetch was never called"
        assert isinstance(captured["config"], _EmptyConfig), (
            f"Expected _EmptyConfig, got {type(captured['config'])}"
        )

    @pytest.mark.asyncio
    async def test_non_sync_config_is_not_none(self) -> None:
        """Confirm the old bug (config=None) is fixed: config must never be None."""
        captured: dict[str, Any] = {}

        connector = MagicMock()
        connector.config_schema = _EmptyConfig

        async def _fetch(_config: Any, _secrets: Any, ref: DocumentRef) -> FetchedDocument:
            captured["config"] = _config
            return _make_fetched(ref)

        connector.fetch = AsyncMock(side_effect=_fetch)

        worker = _make_worker(connector)
        event = _make_single_event()
        await worker.process_document(event)

        assert captured.get("config") is not None


# ---------------------------------------------------------------------------
# 4. Invalid source.config → action="error", no index writes
# ---------------------------------------------------------------------------


class TestConfigValidation:
    @pytest.mark.asyncio
    async def test_invalid_config_returns_error_result(self) -> None:
        """Malformed Source.config must produce action='error' (fail-closed)."""
        connector = MagicMock()
        connector.config_schema = _RequiredConfig  # requires 'must_have' field

        writer = _make_index_writer()
        source = _make_source(config={})  # missing required field
        worker = _make_worker(connector, source=source, writer=writer)

        event = _make_single_event()
        result = await worker.process_document(event)

        assert result.action == "error"
        assert result.error == "source_config_invalid"

    @pytest.mark.asyncio
    async def test_invalid_config_does_not_write_to_index(self) -> None:
        """Invalid config must not trigger any index writes."""
        connector = MagicMock()
        connector.config_schema = _RequiredConfig

        writer = _make_index_writer()
        source = _make_source(config={})
        worker = _make_worker(connector, source=source, writer=writer)

        event = _make_single_event()
        await worker.process_document(event)

        writer.upsert_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_valid_config_passes_through(self) -> None:
        """A source with a valid config proceeds normally."""
        connector = MagicMock()
        connector.config_schema = _RequiredConfig

        ref = DocumentRef(external_id="f.py", uri="file://f.py")
        connector.fetch = AsyncMock(return_value=_make_fetched(ref))

        writer = _make_index_writer()
        source = _make_source(config={"must_have": "present"})
        worker = _make_worker(connector, source=source, writer=writer)

        event = _make_single_event()
        result = await worker.process_document(event)

        assert result.action in ("created", "updated", "unchanged")


# ---------------------------------------------------------------------------
# 5. Pipeline.run_ref: ref.metadata reaches connector.fetch intact
# ---------------------------------------------------------------------------


class TestPipelineRunRef:
    @pytest.mark.asyncio
    async def test_run_ref_passes_metadata_to_fetch(self) -> None:
        """run_ref must pass the supplied ref (with metadata) to connector.fetch."""
        captured_ref: list[DocumentRef] = []

        connector = MagicMock()

        async def _fetch(_config: Any, _secrets: Any, ref: DocumentRef) -> FetchedDocument:
            captured_ref.append(ref)
            return _make_fetched(ref)

        connector.fetch = AsyncMock(side_effect=_fetch)

        writer = _make_index_writer()
        pipeline = IngestionPipeline(
            connector=connector,
            embedding_provider=_make_embedding_provider(),
            index_writer=writer,
        )

        ref_with_meta = DocumentRef(
            external_id="k8s/Deployment/myapp",
            uri="k8s://Deployment/myapp",
            metadata={"kind": "Deployment", "name": "myapp", "namespace": "default"},
        )
        event = _make_single_event(
            external_id=ref_with_meta.external_id,
            uri=ref_with_meta.uri,
        )

        await pipeline.run_ref(
            event=event,
            ref=ref_with_meta,
            config=_EmptyConfig(),
            secrets={},
            workspace_id=uuid.uuid4(),
        )

        assert len(captured_ref) == 1
        assert captured_ref[0].metadata["kind"] == "Deployment"
        assert captured_ref[0].metadata["name"] == "myapp"

    @pytest.mark.asyncio
    async def test_run_ref_metadata_not_stripped(self) -> None:
        """run_ref must NOT reconstruct a bare ref (that strips metadata)."""
        captured_ref: list[DocumentRef] = []

        connector = MagicMock()

        async def _fetch(_config: Any, _secrets: Any, ref: DocumentRef) -> FetchedDocument:
            captured_ref.append(ref)
            return _make_fetched(ref)

        connector.fetch = AsyncMock(side_effect=_fetch)

        pipeline = IngestionPipeline(
            connector=connector,
            embedding_provider=_make_embedding_provider(),
            index_writer=_make_index_writer(),
        )

        ref = DocumentRef(
            external_id="x",
            uri="k8s://x",
            metadata={"kind": "Service", "custom_key": "custom_value"},
        )
        event = _make_single_event(external_id="x", uri="k8s://x")

        await pipeline.run_ref(
            event=event, ref=ref, config=None, secrets={}, workspace_id=uuid.uuid4()
        )

        assert captured_ref[0].metadata.get("custom_key") == "custom_value"


# ---------------------------------------------------------------------------
# 6. Pipeline.run() backwards compat
# ---------------------------------------------------------------------------


class TestPipelineRunBackwardsCompat:
    @pytest.mark.asyncio
    async def test_run_still_works(self) -> None:
        """The existing run() entrypoint must stay green after refactoring."""
        ref = DocumentRef(external_id="src/a.py", uri="file://src/a.py")

        connector = MagicMock()
        connector.fetch = AsyncMock(return_value=_make_fetched(ref, b"code"))

        writer = _make_index_writer()
        pipeline = IngestionPipeline(
            connector=connector,
            embedding_provider=_make_embedding_provider(),
            index_writer=writer,
        )

        event = _make_single_event(external_id="src/a.py", uri="file://src/a.py")
        result = await pipeline.run(
            event=event, config=None, secrets={}, workspace_id=uuid.uuid4()
        )

        assert result.action in ("created", "updated", "unchanged")
        writer.upsert_document.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_builds_bare_ref(self) -> None:
        """run() constructs a ref from the event (no pre-existing metadata) — expected."""
        captured_ref: list[DocumentRef] = []

        connector = MagicMock()

        async def _fetch(_config: Any, _secrets: Any, ref: DocumentRef) -> FetchedDocument:
            captured_ref.append(ref)
            return _make_fetched(ref)

        connector.fetch = AsyncMock(side_effect=_fetch)

        pipeline = IngestionPipeline(
            connector=connector,
            embedding_provider=_make_embedding_provider(),
            index_writer=_make_index_writer(),
        )

        event = _make_single_event(external_id="main.go", uri="file://main.go")
        await pipeline.run(event=event, config=None, secrets={}, workspace_id=uuid.uuid4())

        assert len(captured_ref) == 1
        assert captured_ref[0].external_id == "main.go"
        assert captured_ref[0].metadata == {}  # bare ref, no metadata


# ---------------------------------------------------------------------------
# 7. Bounded concurrency
# ---------------------------------------------------------------------------


class TestBoundedConcurrency:
    @pytest.mark.asyncio
    async def test_concurrency_bounded_to_discovery_limit(self) -> None:
        """Concurrent pipeline.run_ref calls must not exceed _DISCOVERY_CONCURRENCY."""
        from omniscience_server.ingestion.worker import _DISCOVERY_CONCURRENCY

        concurrency_watermark: list[int] = [0]
        active: list[int] = [0]

        refs = [
            DocumentRef(external_id=f"r{i}", uri=f"k8s://r{i}")
            for i in range(_DISCOVERY_CONCURRENCY + 5)
        ]

        connector = MagicMock()
        connector.config_schema = _EmptyConfig

        async def _discover(_config: Any, _secrets: Any) -> AsyncIterator[DocumentRef]:
            for r in refs:
                yield r

        connector.discover = _discover

        async def _fetch(_config: Any, _secrets: Any, ref: DocumentRef) -> FetchedDocument:
            active[0] += 1
            if active[0] > concurrency_watermark[0]:
                concurrency_watermark[0] = active[0]
            await asyncio.sleep(0)
            active[0] -= 1
            return _make_fetched(ref)

        connector.fetch = AsyncMock(side_effect=_fetch)

        writer = _make_index_writer()
        worker = _make_worker(connector, writer=writer)

        event = _make_sync_event()
        await worker.process_document(event)

        assert concurrency_watermark[0] <= _DISCOVERY_CONCURRENCY


# ---------------------------------------------------------------------------
# 8. Secrets: env: prefix form
# ---------------------------------------------------------------------------


class TestSecretsResolution:
    @pytest.mark.asyncio
    async def test_env_prefix_resolves_to_token_and_ca(self) -> None:
        """env:OMNI_K8S_QBIQ_ prefix form resolves {token, ca_cert_b64}."""
        captured_secrets: dict[str, Any] = {}

        connector = MagicMock()
        connector.config_schema = _EmptyConfig

        async def _fetch(
            _config: Any, secrets: dict[str, str], ref: DocumentRef
        ) -> FetchedDocument:
            captured_secrets.update(secrets)
            return _make_fetched(ref)

        connector.fetch = AsyncMock(side_effect=_fetch)

        source = _make_source(secrets_ref="env:OMNI_K8S_QBIQ_")
        worker = _make_worker(connector, source=source)

        env_patch = {
            "OMNI_K8S_QBIQ_token": "my-bearer-token",
            "OMNI_K8S_QBIQ_ca_cert_b64": "base64cacert==",
        }
        event = _make_single_event()
        with patch.dict(os.environ, env_patch):
            await worker.process_document(event)

        assert captured_secrets.get("token") == "my-bearer-token"
        assert captured_secrets.get("ca_cert_b64") == "base64cacert=="

    @pytest.mark.asyncio
    async def test_null_secrets_ref_yields_empty_dict(self) -> None:
        """secrets_ref=None means no secrets (empty dict passed to connector)."""
        captured_secrets: dict[str, Any] = {}

        connector = MagicMock()
        connector.config_schema = _EmptyConfig

        async def _fetch(
            _config: Any, secrets: dict[str, str], ref: DocumentRef
        ) -> FetchedDocument:
            captured_secrets.update(secrets)
            return _make_fetched(ref)

        connector.fetch = AsyncMock(side_effect=_fetch)

        source = _make_source(secrets_ref=None)
        worker = _make_worker(connector, source=source)

        event = _make_single_event()
        await worker.process_document(event)

        assert captured_secrets == {}


# ---------------------------------------------------------------------------
# 9. ACL: workspace_id from Source.tenant_id, never from event payload
# ---------------------------------------------------------------------------


class TestAclInvariant:
    @pytest.mark.asyncio
    async def test_workspace_id_from_source_not_event(self) -> None:
        """workspace_id passed to pipeline must equal Source.tenant_id."""
        target_workspace = uuid.uuid4()
        source = _make_source(tenant_id=target_workspace)
        captured: dict[str, Any] = {}

        connector = MagicMock()
        connector.config_schema = _EmptyConfig

        async def _fetch(_config: Any, _secrets: Any, ref: DocumentRef) -> FetchedDocument:
            return _make_fetched(ref)

        connector.fetch = AsyncMock(side_effect=_fetch)

        writer = _make_index_writer()

        async def _upsert_doc(**kwargs: Any) -> Any:
            captured["workspace_id"] = kwargs.get("workspace_id")
            r = MagicMock()
            r.action = "created"
            r.chunks_written = 1
            r.document_id = uuid.uuid4()
            return r

        writer.upsert_document = AsyncMock(side_effect=_upsert_doc)

        worker = _make_worker(connector, source=source, writer=writer)
        event = _make_single_event()
        await worker.process_document(event)

        assert captured.get("workspace_id") == target_workspace

    @pytest.mark.asyncio
    async def test_sync_discovery_workspace_from_source(self) -> None:
        """During discovery fan-out, workspace_id must still come from Source."""
        target_workspace = uuid.uuid4()
        source = _make_source(tenant_id=target_workspace)
        workspace_ids_seen: list[uuid.UUID] = []

        refs = [DocumentRef(external_id="x", uri="k8s://x", metadata={"kind": "Namespace"})]
        connector = _make_connector_with_discover(refs)

        writer = _make_index_writer()

        async def _upsert_doc(**kwargs: Any) -> Any:
            workspace_ids_seen.append(kwargs.get("workspace_id"))
            r = MagicMock()
            r.action = "created"
            r.chunks_written = 1
            r.document_id = uuid.uuid4()
            return r

        writer.upsert_document = AsyncMock(side_effect=_upsert_doc)

        worker = _make_worker(connector, source=source, writer=writer)
        event = _make_sync_event()
        await worker.process_document(event)

        assert workspace_ids_seen == [target_workspace]


# ---------------------------------------------------------------------------
# 10. _is_sync_marker helper
# ---------------------------------------------------------------------------


class TestIsSyncMarker:
    def test_wildcard_external_id(self) -> None:
        event = _make_sync_event()
        assert _is_sync_marker(event) is True

    def test_sync_uri_prefix(self) -> None:
        sid = _source_id()
        event = DocumentChangeEvent(
            source_id=sid,
            source_type="k8s",
            external_id="*",
            uri=f"sync://{sid}",
            action="updated",
        )
        assert _is_sync_marker(event) is True

    def test_regular_event_not_marker(self) -> None:
        event = _make_single_event()
        assert _is_sync_marker(event) is False

    def test_uri_sync_only_without_wildcard(self) -> None:
        """uri.startswith('sync://') alone is sufficient (belt-and-suspenders)."""
        sid = _source_id()
        event = DocumentChangeEvent(
            source_id=sid,
            source_type="k8s",
            external_id="some-specific-id",
            uri=f"sync://{sid}",
            action="updated",
        )
        assert _is_sync_marker(event) is True
