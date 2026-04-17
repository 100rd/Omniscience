"""Unit tests for the Omniscience connector SDK.

Covers:
- ConnectorRegistry register / get / unknown-type error
- DocumentRef / FetchedDocument / WebhookPayload model validation
- A dummy connector implementing the full protocol
- Secrets-config separation enforcement
- Contract test suite running against the mock connector
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar, cast

import pytest
from omniscience_connectors import (
    Connector,
    ConnectorRegistry,
    DocumentRef,
    FetchedDocument,
    NotFoundError,
    WebhookHandler,
    WebhookPayload,
    get_connector,
)
from omniscience_connectors.registry import _registry
from pydantic import BaseModel

from tests.connectors.contract_tests import ConnectorContractTests

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


class _DummyConfig(BaseModel):
    """Minimal config for the dummy connector.  No secrets here."""

    path: str = "/fixtures"


class _InvalidDummyConfig(BaseModel):
    """Config that will cause the dummy connector's validate() to raise."""

    path: str = ""


class _DummyWebhookHandler(WebhookHandler):
    """Webhook handler that uses HMAC-SHA256 for signature verification."""

    async def verify_signature(
        self,
        payload: bytes,
        headers: dict[str, str],
        secret: str,
    ) -> bool:
        sig_header = headers.get("x-hub-signature-256", "")
        if not sig_header.startswith("sha256="):
            return False
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        provided = sig_header[len("sha256=") :]
        return hmac.compare_digest(expected, provided)

    async def parse_payload(
        self,
        payload: bytes,
        headers: dict[str, str],
    ) -> WebhookPayload:
        return WebhookPayload(
            source_name="dummy",
            affected_refs=[
                DocumentRef(
                    external_id="abc123",
                    uri="/fixtures/hello.md",
                    updated_at=datetime.now(UTC),
                )
            ],
            raw_headers=headers,
        )


class DummyConnector(Connector):
    """Mock connector implementing the full Connector protocol for testing."""

    connector_type: ClassVar[str] = "dummy"
    config_schema: ClassVar[type[BaseModel]] = _DummyConfig

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        cfg = cast("_DummyConfig", config)
        if not isinstance(cfg, _DummyConfig) or not cfg.path:
            raise ValueError("DummyConnector requires a non-empty path")

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        cfg = cast("_DummyConfig", config)
        yield DocumentRef(
            external_id="doc-1",
            uri=f"{cfg.path}/doc-1.md",
            updated_at=datetime.now(UTC),
            metadata={"author": "test"},
        )
        yield DocumentRef(
            external_id="doc-2",
            uri=f"{cfg.path}/doc-2.md",
        )

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        content = f"# {ref.external_id}\n\nContent for {ref.uri}".encode()
        return FetchedDocument(
            ref=ref,
            content_bytes=content,
            content_type="text/markdown",
        )

    def webhook_handler(self) -> WebhookHandler | None:
        return _DummyWebhookHandler()


class DummyPullOnlyConnector(Connector):
    """Pull-only connector — webhook_handler returns None."""

    connector_type: ClassVar[str] = "dummy-pull-only"
    config_schema: ClassVar[type[BaseModel]] = _DummyConfig

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        pass

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        yield DocumentRef(external_id="ref-0", uri="/path/ref-0.txt")

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        return FetchedDocument(
            ref=ref,
            content_bytes=b"hello world",
            content_type="text/plain",
        )

    def webhook_handler(self) -> WebhookHandler | None:
        return None


# ---------------------------------------------------------------------------
# ConnectorRegistry tests
# ---------------------------------------------------------------------------


class TestConnectorRegistry:
    def setup_method(self) -> None:
        """Use a fresh registry for each test."""
        self.registry = ConnectorRegistry()

    def test_register_and_get_returns_instance(self) -> None:
        self.registry.register(DummyConnector)
        connector = self.registry.get("dummy")
        assert isinstance(connector, DummyConnector)

    def test_get_unknown_type_raises_not_found_error(self) -> None:
        with pytest.raises(NotFoundError) as exc_info:
            self.registry.get("nonexistent")
        assert exc_info.value.connector_type == "nonexistent"
        assert "nonexistent" in str(exc_info.value)

    def test_register_multiple_connectors(self) -> None:
        self.registry.register(DummyConnector)
        self.registry.register(DummyPullOnlyConnector)
        assert isinstance(self.registry.get("dummy"), DummyConnector)
        assert isinstance(self.registry.get("dummy-pull-only"), DummyPullOnlyConnector)

    def test_registered_types_returns_sorted_list(self) -> None:
        self.registry.register(DummyPullOnlyConnector)
        self.registry.register(DummyConnector)
        types = self.registry.registered_types()
        assert types == sorted(types)
        assert "dummy" in types
        assert "dummy-pull-only" in types

    def test_reregister_replaces_previous_entry(self) -> None:
        self.registry.register(DummyConnector)
        self.registry.register(DummyConnector)  # Re-register same type
        # Should not raise; should still work
        connector = self.registry.get("dummy")
        assert isinstance(connector, DummyConnector)

    def test_register_raises_for_connector_without_type(self) -> None:
        class NoTypeConnector(Connector):
            config_schema: ClassVar[type[BaseModel]] = _DummyConfig

            async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
                pass

            async def discover(
                self, config: BaseModel, secrets: dict[str, str]
            ) -> AsyncIterator[DocumentRef]:
                yield DocumentRef(external_id="x", uri="y")  # pragma: no cover

            async def fetch(
                self, config: BaseModel, secrets: dict[str, str], ref: DocumentRef
            ) -> FetchedDocument:
                return FetchedDocument(  # pragma: no cover
                    ref=ref, content_bytes=b"", content_type="text/plain"
                )

        with pytest.raises(ValueError, match="non-empty 'type'"):
            self.registry.register(NoTypeConnector)

    def test_not_found_error_is_key_error_subclass(self) -> None:
        """NotFoundError inherits from KeyError for dict-style exception handling."""
        err = NotFoundError("test-type")
        assert isinstance(err, KeyError)

    def test_module_level_get_connector_uses_shared_registry(self) -> None:
        """get_connector() delegates to the module-level singleton registry."""
        _registry.register(DummyConnector)
        connector = get_connector("dummy")
        assert isinstance(connector, DummyConnector)


# ---------------------------------------------------------------------------
# DocumentRef model tests
# ---------------------------------------------------------------------------


class TestDocumentRef:
    def test_minimal_fields(self) -> None:
        ref = DocumentRef(external_id="abc", uri="https://example.com/doc")
        assert ref.external_id == "abc"
        assert ref.uri == "https://example.com/doc"
        assert ref.updated_at is None
        assert ref.metadata == {}

    def test_with_all_fields(self) -> None:
        ts = datetime(2024, 1, 1, tzinfo=UTC)
        ref = DocumentRef(
            external_id="abc",
            uri="https://example.com/doc",
            updated_at=ts,
            metadata={"author": "alice", "tags": ["infra", "k8s"]},
        )
        assert ref.updated_at == ts
        assert ref.metadata["author"] == "alice"

    def test_metadata_default_is_empty_dict(self) -> None:
        ref1 = DocumentRef(external_id="a", uri="b")
        ref2 = DocumentRef(external_id="c", uri="d")
        # Ensure they are not the same object (mutable default factory)
        ref1.metadata["key"] = "value"
        assert "key" not in ref2.metadata

    def test_equality(self) -> None:
        ref1 = DocumentRef(external_id="x", uri="y")
        ref2 = DocumentRef(external_id="x", uri="y")
        assert ref1 == ref2

    def test_json_roundtrip(self) -> None:
        ref = DocumentRef(
            external_id="abc",
            uri="file:///tmp/doc.md",
            updated_at=datetime(2024, 6, 1, tzinfo=UTC),
            metadata={"size": 1234},
        )
        dumped = ref.model_dump_json()
        loaded = DocumentRef.model_validate_json(dumped)
        assert loaded == ref


# ---------------------------------------------------------------------------
# FetchedDocument model tests
# ---------------------------------------------------------------------------


class TestFetchedDocument:
    def test_basic_construction(self) -> None:
        ref = DocumentRef(external_id="id1", uri="/path/doc.md")
        doc = FetchedDocument(ref=ref, content_bytes=b"hello", content_type="text/markdown")
        assert doc.ref == ref
        assert doc.content_bytes == b"hello"
        assert doc.content_type == "text/markdown"

    def test_content_bytes_can_be_empty(self) -> None:
        """Empty byte string is technically valid (e.g. zero-byte files)."""
        ref = DocumentRef(external_id="empty", uri="/empty.txt")
        doc = FetchedDocument(ref=ref, content_bytes=b"", content_type="text/plain")
        assert doc.content_bytes == b""

    def test_binary_content(self) -> None:
        ref = DocumentRef(external_id="bin", uri="/doc.pdf")
        data = bytes(range(256))
        doc = FetchedDocument(ref=ref, content_bytes=data, content_type="application/pdf")
        assert doc.content_bytes == data


# ---------------------------------------------------------------------------
# WebhookPayload model tests
# ---------------------------------------------------------------------------


class TestWebhookPayload:
    def test_basic_construction(self) -> None:
        refs = [DocumentRef(external_id="r1", uri="/a"), DocumentRef(external_id="r2", uri="/b")]
        payload = WebhookPayload(
            source_name="github",
            affected_refs=refs,
            raw_headers={"x-github-event": "push", "content-type": "application/json"},
        )
        assert payload.source_name == "github"
        assert len(payload.affected_refs) == 2
        assert payload.raw_headers["x-github-event"] == "push"

    def test_empty_affected_refs_allowed(self) -> None:
        payload = WebhookPayload(source_name="noop", affected_refs=[], raw_headers={})
        assert payload.affected_refs == []

    def test_json_roundtrip(self) -> None:
        payload = WebhookPayload(
            source_name="gitlab",
            affected_refs=[DocumentRef(external_id="abc", uri="https://gl.example.com/file.py")],
            raw_headers={"x-gitlab-token": "redacted"},
        )
        restored = WebhookPayload.model_validate_json(payload.model_dump_json())
        assert restored.source_name == payload.source_name
        assert len(restored.affected_refs) == 1


# ---------------------------------------------------------------------------
# Secrets-config separation tests
# ---------------------------------------------------------------------------


class TestSecretsConfigSeparation:
    """Verifies the contract that secrets must never appear in config objects."""

    def test_dummy_config_has_no_secret_fields(self) -> None:
        """Config model fields must not be named 'secret', 'token', 'password', etc."""
        sensitive_names = {"secret", "token", "password", "api_key", "key", "credential"}
        config_fields = set(_DummyConfig.model_fields.keys())
        overlap = config_fields & sensitive_names
        assert not overlap, f"Config model must not contain secret fields: {overlap}"

    async def test_secrets_passed_separately_to_validate(self) -> None:
        """validate() receives config and secrets as distinct arguments."""
        connector = DummyConnector()
        config = _DummyConfig(path="/test")
        secrets: dict[str, str] = {"token": "super-secret-value"}
        # Should work without error
        await connector.validate(config, secrets)
        # Config object must not have the token attribute
        assert not hasattr(config, "token")

    async def test_secrets_passed_separately_to_fetch(self) -> None:
        """fetch() must accept secrets as a separate dict, not embedded in config."""
        connector = DummyConnector()
        config = _DummyConfig(path="/test")
        secrets: dict[str, str] = {"token": "test-token"}
        ref = DocumentRef(external_id="doc-1", uri="/test/doc-1.md")
        doc = await connector.fetch(config, secrets, ref)
        assert doc.content_bytes  # fetched successfully without secrets in config

    async def test_connector_config_schema_declared(self) -> None:
        """config_schema ClassVar must be declared and be a BaseModel subclass."""
        assert hasattr(DummyConnector, "config_schema")
        assert issubclass(DummyConnector.config_schema, BaseModel)


# ---------------------------------------------------------------------------
# Dummy connector protocol compliance tests
# ---------------------------------------------------------------------------


class TestDummyConnectorProtocol:
    async def test_validate_succeeds_with_valid_config(self) -> None:
        connector = DummyConnector()
        await connector.validate(_DummyConfig(path="/repo"), {"token": "tok"})

    async def test_validate_fails_with_empty_path(self) -> None:
        connector = DummyConnector()
        with pytest.raises(ValueError):
            await connector.validate(_InvalidDummyConfig(), {})

    async def test_discover_yields_document_refs(self) -> None:
        connector = DummyConnector()
        refs: list[DocumentRef] = []
        async for ref in connector.discover(_DummyConfig(), {}):
            refs.append(ref)
        assert len(refs) == 2
        assert all(isinstance(r, DocumentRef) for r in refs)

    async def test_fetch_returns_fetched_document(self) -> None:
        connector = DummyConnector()
        ref = DocumentRef(external_id="doc-1", uri="/fixtures/doc-1.md")
        doc = await connector.fetch(_DummyConfig(), {}, ref)
        assert isinstance(doc, FetchedDocument)
        assert b"doc-1" in doc.content_bytes
        assert doc.content_type == "text/markdown"

    def test_webhook_handler_returns_handler(self) -> None:
        connector = DummyConnector()
        handler = connector.webhook_handler()
        assert isinstance(handler, WebhookHandler)

    def test_pull_only_webhook_handler_returns_none(self) -> None:
        connector = DummyPullOnlyConnector()
        assert connector.webhook_handler() is None

    async def test_webhook_verify_rejects_bad_signature(self) -> None:
        connector = DummyConnector()
        handler = connector.webhook_handler()
        assert handler is not None
        result = await handler.verify_signature(
            payload=b'{"event":"push"}',
            headers={"x-hub-signature-256": "sha256=badhash"},
            secret="test-secret",
        )
        assert result is False

    async def test_webhook_verify_accepts_valid_signature(self) -> None:
        connector = DummyConnector()
        handler = connector.webhook_handler()
        assert handler is not None

        payload = b'{"event":"push","repo":"myrepo"}'
        secret = "my-webhook-secret"
        mac = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        headers = {"x-hub-signature-256": f"sha256={mac}"}

        result = await handler.verify_signature(payload=payload, headers=headers, secret=secret)
        assert result is True

    async def test_webhook_parse_returns_webhook_payload(self) -> None:
        connector = DummyConnector()
        handler = connector.webhook_handler()
        assert handler is not None

        parsed = await handler.parse_payload(
            payload=b'{"event":"push"}',
            headers={"content-type": "application/json"},
        )
        assert isinstance(parsed, WebhookPayload)
        assert parsed.source_name == "dummy"
        assert len(parsed.affected_refs) == 1


# ---------------------------------------------------------------------------
# Contract test suite - runs against the mock connector
# ---------------------------------------------------------------------------


class TestDummyConnectorContract(ConnectorContractTests):
    """Runs the full connector contract against :class:`DummyConnector`."""

    def make_connector(self) -> Connector:
        return DummyConnector()

    def valid_config(self) -> BaseModel:
        return _DummyConfig(path="/fixtures")

    def invalid_config(self) -> BaseModel:
        return _InvalidDummyConfig()

    def secrets(self) -> dict[str, str]:
        return {"token": "test-token"}

    async def test_webhook_handler_accepts_valid_signature(self) -> None:
        """Override: provide a correctly-signed payload for the HMAC handler."""
        connector = self.make_connector()
        handler = connector.webhook_handler()
        assert handler is not None

        payload = b'{"ref":"refs/heads/main"}'
        secret = "contract-test-secret"
        mac = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

        result = await handler.verify_signature(
            payload=payload,
            headers={"x-hub-signature-256": f"sha256={mac}"},
            secret=secret,
        )
        assert result is True


# ---------------------------------------------------------------------------
# Registry integration - shared module-level registry
# ---------------------------------------------------------------------------


class TestModuleLevelRegistry:
    def test_get_connector_raises_not_found_for_unregistered_type(self) -> None:
        with pytest.raises(NotFoundError):
            get_connector("type-that-definitely-does-not-exist-xyz")

    def test_registered_connector_retrievable_via_module_function(self) -> None:
        _registry.register(DummyConnector)
        conn = get_connector("dummy")
        assert isinstance(conn, DummyConnector)

    def test_registered_types_includes_all_registered(self) -> None:
        _registry.register(DummyConnector)
        _registry.register(DummyPullOnlyConnector)
        types = _registry.registered_types()
        assert "dummy" in types
        assert "dummy-pull-only" in types

    def test_document_ref_metadata_isolation(self) -> None:
        """Ensure mutable default for metadata doesn't bleed between instances."""
        ref_a: dict[str, Any] = DocumentRef(external_id="a", uri="a").metadata
        ref_b: dict[str, Any] = DocumentRef(external_id="b", uri="b").metadata
        ref_a["injected"] = True
        assert "injected" not in ref_b
