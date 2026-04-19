"""ExampleConnector — a minimal connector plugin implementation.

This module ships with the ``omniscience-plugin-example`` package and
demonstrates the absolute minimum that a third-party connector must provide:

1. A Pydantic ``config_schema`` model that validates public configuration.
2. A ``connector_type`` class variable that names the connector in the registry.
3. Implementations of ``validate()``, ``discover()``, and ``fetch()``.

The connector is intentionally trivial: it returns a fixed set of in-memory
documents without connecting to any external service.  Real plugins replace
this with HTTP calls, database queries, filesystem traversal, etc.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from omniscience_connectors.base import (
    Connector,
    DocumentRef,
    FetchedDocument,
)
from pydantic import BaseModel, Field


class ExampleConfig(BaseModel):
    """Public (non-secret) configuration for the ExampleConnector.

    Secrets (e.g. API keys) are never part of config_schema — they arrive
    via the ``secrets`` argument at call-time.
    """

    base_url: str = Field(
        default="https://example.com",
        description="Base URL of the data source.",
    )
    max_documents: int = Field(
        default=10,
        ge=1,
        le=1000,
        description="Maximum number of documents to return during discovery.",
    )


# Hardcoded sample data the connector returns.
_SAMPLE_DOCUMENTS: list[tuple[str, str, str]] = [
    ("doc-001", "https://example.com/intro", "# Introduction\n\nWelcome to the example plugin."),
    ("doc-002", "https://example.com/guide", "# Guide\n\nThis is the user guide content."),
    ("doc-003", "https://example.com/api", "# API Reference\n\nAPI documentation lives here."),
]


class ExampleConnector(Connector):
    """Minimal example connector that returns in-memory documents.

    Demonstrates the connector plugin contract without requiring external
    connectivity.  Useful as a starting point for real plugin development.

    Class variables
    ---------------
    connector_type:
        Registered as ``"example"`` in the Omniscience connector registry.
    config_schema:
        :class:`ExampleConfig` — validates public configuration.
    """

    connector_type: ClassVar[str] = "example"
    config_schema: ClassVar[type[BaseModel]] = ExampleConfig

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        """Verify that config can be parsed.  No network calls required.

        Args:
            config: Validated :class:`ExampleConfig` instance.
            secrets: Runtime secret values (unused by this connector).
        """
        # Cast to ExampleConfig to access typed fields.
        cfg = ExampleConfig.model_validate(config.model_dump())
        if cfg.max_documents < 1:
            raise ValueError("max_documents must be at least 1")

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Yield up to ``max_documents`` document references.

        Args:
            config: Validated :class:`ExampleConfig` instance.
            secrets: Runtime secret values (unused).

        Yields:
            :class:`~omniscience_connectors.base.DocumentRef` objects.
        """
        cfg = ExampleConfig.model_validate(config.model_dump())
        for i, (doc_id, uri, _) in enumerate(_SAMPLE_DOCUMENTS):
            if i >= cfg.max_documents:
                break
            yield DocumentRef(
                external_id=doc_id,
                uri=uri,
                metadata={"source": "example-plugin"},
            )

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        """Return the content for *ref*.

        Args:
            config: Validated :class:`ExampleConfig` instance.
            secrets: Runtime secret values (unused).
            ref: A reference previously returned by :meth:`discover`.

        Returns:
            :class:`~omniscience_connectors.base.FetchedDocument` with markdown content.

        Raises:
            KeyError: If *ref.external_id* is not found in the sample data.
        """
        data = {doc_id: content for doc_id, _, content in _SAMPLE_DOCUMENTS}
        content = data.get(ref.external_id)
        if content is None:
            raise KeyError(f"Document {ref.external_id!r} not found in example data")

        return FetchedDocument(
            ref=ref,
            content_bytes=content.encode("utf-8"),
            content_type="text/markdown",
        )
