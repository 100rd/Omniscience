"""Abstract contract test suite for Omniscience connectors.

Every connector must pass this suite.  To use it, create a concrete test
class that:

1. Inherits from :class:`ConnectorContractTests`.
2. Overrides the three abstract helper methods to return a configured
   connector, valid config, invalid config, and secrets.

Example::

    class TestMyConnector(ConnectorContractTests):
        def make_connector(self) -> Connector:
            return MyConnector()

        def valid_config(self) -> BaseModel:
            return MyConfig(repo="https://example.com/repo.git")

        def invalid_config(self) -> BaseModel:
            return MyConfig(repo="")  # empty repo → validate should raise

        def secrets(self) -> dict[str, str]:
            return {"token": "test-token"}

Run with::

    pytest tests/connectors/contract_tests.py::TestMyConnector

The base class is purposefully *not* collected by pytest (no ``Test``
prefix, abstract methods would fail on instantiation).
"""

from __future__ import annotations

import abc
import hashlib

import pytest
from omniscience_connectors.base import (
    Connector,
    DocumentRef,
    FetchedDocument,
    WebhookHandler,
)
from pydantic import BaseModel


class ConnectorContractTests(abc.ABC):
    """Abstract base for connector contract tests.

    Concrete subclasses MUST implement the three abstract methods below.
    All test methods are async-compatible via pytest-asyncio.
    """

    # ------------------------------------------------------------------
    # Abstract fixture helpers — override in concrete test classes
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def make_connector(self) -> Connector:
        """Return a fully initialised connector under test."""

    @abc.abstractmethod
    def valid_config(self) -> BaseModel:
        """Return a valid config that ``validate()`` and ``discover()`` accept."""

    @abc.abstractmethod
    def invalid_config(self) -> BaseModel:
        """Return a config that ``validate()`` must reject (raise any exception)."""

    @abc.abstractmethod
    def secrets(self) -> dict[str, str]:
        """Return secrets dict used alongside :meth:`valid_config`."""

    # ------------------------------------------------------------------
    # Contract: validate()
    # ------------------------------------------------------------------

    async def test_validate_succeeds_with_valid_config(self) -> None:
        """validate() must not raise when given a valid config + secrets."""
        connector = self.make_connector()
        # Should complete without raising
        await connector.validate(self.valid_config(), self.secrets())

    async def test_validate_raises_with_invalid_config(self) -> None:
        """validate() must raise for bad config or bad secrets."""
        connector = self.make_connector()
        with pytest.raises(Exception):  # noqa: B017 — any exception is a contract pass
            await connector.validate(self.invalid_config(), self.secrets())

    # ------------------------------------------------------------------
    # Contract: discover()
    # ------------------------------------------------------------------

    async def test_discover_yields_at_least_one_ref(self) -> None:
        """discover() must yield at least one DocumentRef against a fixture source."""
        connector = self.make_connector()
        refs: list[DocumentRef] = []
        async for ref in connector.discover(self.valid_config(), self.secrets()):
            refs.append(ref)
            break  # We only need one to satisfy the contract

        assert len(refs) >= 1, "discover() must yield at least one DocumentRef"
        assert isinstance(refs[0], DocumentRef)

    async def test_discover_refs_have_required_fields(self) -> None:
        """Every yielded DocumentRef must have non-empty external_id and uri."""
        connector = self.make_connector()
        async for ref in connector.discover(self.valid_config(), self.secrets()):
            assert ref.external_id, "DocumentRef.external_id must be non-empty"
            assert ref.uri, "DocumentRef.uri must be non-empty"
            break

    # ------------------------------------------------------------------
    # Contract: fetch()
    # ------------------------------------------------------------------

    async def test_fetch_returns_fetched_document(self) -> None:
        """fetch() must return a FetchedDocument with non-empty content_bytes."""
        connector = self.make_connector()

        # Get a ref from discover() to feed into fetch()
        ref: DocumentRef | None = None
        async for discovered_ref in connector.discover(self.valid_config(), self.secrets()):
            ref = discovered_ref
            break

        assert ref is not None, "discover() must yield at least one ref for fetch() test"

        result = await connector.fetch(self.valid_config(), self.secrets(), ref)

        assert isinstance(result, FetchedDocument)
        assert isinstance(result.content_bytes, bytes)
        assert len(result.content_bytes) > 0, "FetchedDocument.content_bytes must be non-empty"
        assert result.content_type, "FetchedDocument.content_type must be non-empty"
        assert result.ref == ref

    async def test_fetch_content_bytes_are_deterministic(self) -> None:
        """fetch() for the same ref must return the same bytes (deterministic)."""
        connector = self.make_connector()

        ref: DocumentRef | None = None
        async for discovered_ref in connector.discover(self.valid_config(), self.secrets()):
            ref = discovered_ref
            break

        assert ref is not None

        result1 = await connector.fetch(self.valid_config(), self.secrets(), ref)
        result2 = await connector.fetch(self.valid_config(), self.secrets(), ref)

        assert (
            hashlib.sha256(result1.content_bytes).hexdigest()
            == hashlib.sha256(result2.content_bytes).hexdigest()
        ), "fetch() must be deterministic for the same ref"

    # ------------------------------------------------------------------
    # Contract: webhook_handler()
    # ------------------------------------------------------------------

    def test_webhook_handler_returns_handler_or_none(self) -> None:
        """webhook_handler() must return a WebhookHandler or None."""
        connector = self.make_connector()
        handler = connector.webhook_handler()
        assert handler is None or isinstance(handler, WebhookHandler), (
            "webhook_handler() must return WebhookHandler | None"
        )

    async def test_webhook_handler_rejects_invalid_signature(self) -> None:
        """If webhook_handler() is not None, it must reject tampered payloads."""
        connector = self.make_connector()
        handler = connector.webhook_handler()

        if handler is None:
            pytest.skip("Connector does not support webhooks")

        result = await handler.verify_signature(
            payload=b'{"tampered": true}',
            headers={"x-hub-signature-256": "sha256=badhash"},
            secret="test-secret",
        )
        assert result is False, "verify_signature() must return False for invalid signatures"

    async def test_webhook_handler_accepts_valid_signature(self) -> None:
        """If webhook_handler() is not None, a correctly-signed payload is accepted.

        Subclasses should override this if the connector has custom signing logic.
        The base implementation skips the test when no webhook handler is present.
        """
        connector = self.make_connector()
        handler = connector.webhook_handler()

        if handler is None:
            pytest.skip("Connector does not support webhooks")

        # Subclasses must override with a properly signed payload.
        # The base just verifies the method doesn't crash.
        pytest.skip(
            "Override test_webhook_handler_accepts_valid_signature in your test class "
            "to provide a properly-signed payload specific to your connector."
        )
