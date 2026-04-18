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

Built-in connectors (``git``, ``fs``) are registered below and available
immediately on import.  Third-party connectors call :func:`get_connector`
after registering against the shared registry.
"""

from omniscience_connectors.base import (
    Connector,
    DocumentRef,
    FetchedDocument,
    WebhookHandler,
    WebhookPayload,
)
from omniscience_connectors.fs.connector import FsConnector
from omniscience_connectors.git.connector import GitConnector
from omniscience_connectors.registry import (
    ConnectorRegistry,
    NotFoundError,
    _registry,
    get_connector,
)

# Register built-in connectors in the shared module-level registry
_registry.register(GitConnector)
_registry.register(FsConnector)

# Public alias for the shared registry instance (git + fs pre-registered).
default_registry: ConnectorRegistry = _registry

__all__ = [
    "Connector",
    "ConnectorRegistry",
    "DocumentRef",
    "FetchedDocument",
    "FsConnector",
    "GitConnector",
    "NotFoundError",
    "WebhookHandler",
    "WebhookPayload",
    "default_registry",
    "get_connector",
]
