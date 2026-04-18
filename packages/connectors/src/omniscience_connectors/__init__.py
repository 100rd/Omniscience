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

Built-in connectors (``git``, ``fs``, ``confluence``, ``notion``, ``slack``,
``jira``) are registered below and available immediately on import.
Third-party connectors call :func:`get_connector` after registering against
the shared registry.
"""

from omniscience_connectors.base import (
    Connector,
    DocumentRef,
    FetchedDocument,
    WebhookHandler,
    WebhookPayload,
)
from omniscience_connectors.confluence.connector import ConfluenceConnector
from omniscience_connectors.fs.connector import FsConnector
from omniscience_connectors.git.connector import GitConnector
from omniscience_connectors.jira.connector import JiraConnector
from omniscience_connectors.notion.connector import NotionConnector
from omniscience_connectors.registry import (
    ConnectorRegistry,
    NotFoundError,
    _registry,
    get_connector,
)
from omniscience_connectors.slack.connector import SlackConnector

# Register built-in connectors in the shared module-level registry
_registry.register(GitConnector)
_registry.register(FsConnector)
_registry.register(ConfluenceConnector)
_registry.register(NotionConnector)
_registry.register(SlackConnector)
_registry.register(JiraConnector)

# Public alias for the shared registry instance (all built-ins pre-registered).
default_registry: ConnectorRegistry = _registry

__all__ = [
    "ConfluenceConnector",
    "Connector",
    "ConnectorRegistry",
    "DocumentRef",
    "FetchedDocument",
    "FsConnector",
    "GitConnector",
    "JiraConnector",
    "NotFoundError",
    "NotionConnector",
    "SlackConnector",
    "WebhookHandler",
    "WebhookPayload",
    "default_registry",
    "get_connector",
]
