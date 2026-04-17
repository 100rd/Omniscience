"""omniscience-connectors — Source connector framework for Omniscience.

Public surface
--------------
Data types::

    from omniscience_connectors import DocumentRef, FetchedDocument
    from omniscience_connectors import WebhookHandler, WebhookPayload

Protocols::

    from omniscience_connectors import Connector

Registry::

    from omniscience_connectors import ConnectorRegistry, get_connector

Built-in connectors (``git``, ``fs``) will be registered here in subsequent
issues.  Third-party connectors call :func:`get_connector` after registering
against the shared registry.
"""

from omniscience_connectors.base import (
    Connector,
    DocumentRef,
    FetchedDocument,
    WebhookHandler,
    WebhookPayload,
)
from omniscience_connectors.registry import (
    ConnectorRegistry,
    NotFoundError,
    get_connector,
)

__all__ = [
    "Connector",
    "ConnectorRegistry",
    "DocumentRef",
    "FetchedDocument",
    "NotFoundError",
    "WebhookHandler",
    "WebhookPayload",
    "get_connector",
]
