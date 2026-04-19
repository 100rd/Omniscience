"""omniscience-connectors — Source connector framework for Omniscience.

Public surface
--------------
Data types::

    from omniscience_connectors import DocumentRef, FetchedDocument
    from omniscience_connectors import WebhookHandler, WebhookPayload

Protocols::

    from omniscience_connectors import Connector

Agentic::

    from omniscience_connectors import AgentConfig, AgenticConnector
    from omniscience_connectors import K8sAgenticConnector

Registry::

    from omniscience_connectors import ConnectorRegistry, get_connector

Built-in connectors (``git``, ``fs``, ``confluence``, ``notion``, ``slack``,
``jira``, ``k8s-agentic``) are registered below and available immediately on
import.  Third-party connectors call :func:`get_connector` after registering
against the shared registry.
"""

from omniscience_connectors.agentic import (
    AgentConfig,
    AgenticConnector,
    K8sAgenticConfig,
    K8sAgenticConnector,
    LLMProvider,
    OllamaLLMProvider,
    build_provider,
)
from omniscience_connectors.base import (
    Connector,
    DocumentRef,
    FetchedDocument,
    WebhookHandler,
    WebhookPayload,
)
from omniscience_connectors.confluence.connector import ConfluenceConnector
from omniscience_connectors.database.connector import DatabaseConnector
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
_registry.register(K8sAgenticConnector)
_registry.register(DatabaseConnector)

# Public alias for the shared registry instance (all built-ins pre-registered).
default_registry: ConnectorRegistry = _registry

__all__ = [
    "AgentConfig",
    "AgenticConnector",
    "ConfluenceConnector",
    "Connector",
    "ConnectorRegistry",
    "DatabaseConnector",
    "DocumentRef",
    "FetchedDocument",
    "FsConnector",
    "GitConnector",
    "JiraConnector",
    "K8sAgenticConfig",
    "K8sAgenticConnector",
    "LLMProvider",
    "NotFoundError",
    "NotionConnector",
    "OllamaLLMProvider",
    "SlackConnector",
    "WebhookHandler",
    "WebhookPayload",
    "build_provider",
    "default_registry",
    "get_connector",
]
