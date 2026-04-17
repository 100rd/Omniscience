"""Connector protocol and data types for the Omniscience source connector framework.

Every source connector must implement the ``Connector`` protocol.  The framework
is intentionally small: connectors own discovery and fetching; parsing, chunking,
and embedding happen downstream in the ingestion pipeline.

Design constraints (see docs/api/connector-sdk.md):
- Connectors never receive secrets in ``config``; secrets arrive via a separate
  ``secrets: dict[str, str]`` argument resolved at runtime.
- Connectors must not log raw secret values.
- Parsing/chunking happens downstream, NOT in connectors.
"""

from __future__ import annotations

import builtins
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, Field

__all__ = [
    "Connector",
    "DocumentRef",
    "FetchedDocument",
    "WebhookHandler",
    "WebhookPayload",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------


class DocumentRef(BaseModel):
    """Reference to a document in a source.

    Passed from ``discover()`` to the ingestion pipeline; the pipeline then
    calls ``fetch()`` with this ref to retrieve the actual content.
    """

    external_id: str
    """Source-native identifier — stable across syncs (e.g. git blob SHA, Confluence page ID)."""

    uri: str
    """Human-readable address for the document (URL, file path, …)."""

    updated_at: datetime | None = None
    """Last-modified timestamp as reported by the source.  May be ``None`` when
    the source does not expose modification timestamps."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Arbitrary source-specific metadata (labels, mime-hint, author, …).
    Must not contain secrets."""


class FetchedDocument(BaseModel):
    """Full document content fetched from a source.

    Raw bytes + MIME type.  Parsing is left to the downstream pipeline.
    """

    ref: DocumentRef
    """The reference that triggered this fetch."""

    content_bytes: bytes
    """Raw document bytes.  Encoding is source-specific; the MIME type provides a hint."""

    content_type: str
    """IANA MIME type (e.g. ``"text/markdown"``, ``"text/plain"``, ``"application/json"``)."""


class WebhookPayload(BaseModel):
    """Parsed webhook payload produced by :class:`WebhookHandler`.

    After signature verification and parsing, the connector returns the
    affected document refs so the ingestion pipeline can trigger a targeted
    partial sync.
    """

    source_name: str
    """Logical name of the source that sent the webhook."""

    affected_refs: list[DocumentRef]
    """Documents reported as changed by this webhook event."""

    raw_headers: dict[str, str]
    """Original HTTP headers forwarded by the webhook receiver (lower-cased keys)."""


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


class WebhookHandler:
    """Handler for push-style source updates.

    Connectors that support webhooks return an instance of this class from
    :meth:`Connector.webhook_handler`.  Callers MUST call
    :meth:`verify_signature` before :meth:`parse_payload`.
    """

    async def verify_signature(
        self,
        payload: bytes,
        headers: dict[str, str],
        secret: str,
    ) -> bool:
        """Return ``True`` if the request is authentic, ``False`` otherwise.

        Implementations should use a constant-time comparison to prevent
        timing attacks.  Must not raise on invalid inputs — return ``False``
        instead.
        """
        raise NotImplementedError

    async def parse_payload(
        self,
        payload: bytes,
        headers: dict[str, str],
    ) -> WebhookPayload:
        """Parse the raw request body into a :class:`WebhookPayload`.

        Should only be called after :meth:`verify_signature` returns ``True``.
        Raises :class:`ValueError` if the payload cannot be parsed.
        """
        raise NotImplementedError


class Connector:
    """Base class every source connector must implement.

    Connectors are **stateless** with respect to configuration — all
    configuration is passed explicitly at call-time so a single class
    instance can serve multiple source records.

    Class variables
    ---------------
    connector_type:
        Short, unique string key used in the registry (e.g. ``"git"``).
        Exposed as ``type`` via a property for backwards compatibility with
        the spec.
    config_schema:
        Pydantic model class that validates the public, persisted config block
        for this connector type.  Never include secrets here.
    """

    # ``type`` conflicts with the Python 3.12 soft keyword in class bodies when
    # used inside ClassVar[type[...]] annotations — mypy misinterprets the
    # inner ``type`` as a forward reference to this attribute.  We store the
    # connector's type string under ``connector_type`` and expose it as
    # ``type`` via a class property to satisfy the interface and the registry.
    connector_type: ClassVar[str]
    config_schema: ClassVar[builtins.type[BaseModel]]

    @classmethod
    def get_connector_type(cls) -> str:
        """Return the connector type string (alias for ``connector_type``)."""
        return cls.connector_type

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        """Dry-run connectivity and permission check.

        Should attempt the cheapest possible authenticated request to verify
        that the configuration and secrets are correct.  Raises on failure —
        callers treat any exception as a validation error.

        Args:
            config: Validated public configuration (never contains secrets).
            secrets: Runtime-resolved secret values keyed by name.  Must not
                be logged or stored.
        """
        raise NotImplementedError

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Yield every document currently present in the source.

        For large sources this may be a long-running stream.  The pipeline
        controls back-pressure — connectors should not buffer the full list
        in memory.

        Args:
            config: Validated public configuration.
            secrets: Runtime-resolved secret values.

        Yields:
            :class:`DocumentRef` objects for each discovered document.
        """
        raise NotImplementedError
        # Make this a valid async generator at the type level.
        yield  # pragma: no cover  # type: ignore[misc]

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        """Return content + metadata for one document.

        Args:
            config: Validated public configuration.
            secrets: Runtime-resolved secret values.
            ref: The reference returned by :meth:`discover` (or from a webhook).

        Returns:
            :class:`FetchedDocument` with raw bytes and MIME type.
        """
        raise NotImplementedError

    def webhook_handler(self) -> WebhookHandler | None:
        """Return a handler if this connector supports push-style updates.

        Returns:
            A :class:`WebhookHandler` instance, or ``None`` for pull-only connectors.
        """
        return None
