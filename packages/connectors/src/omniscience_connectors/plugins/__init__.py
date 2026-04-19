"""Connector plugin system for the Omniscience marketplace.

Third-party connector plugins are discovered via Python entry points
(group: ``omniscience.connectors``) and loaded at runtime.

Usage::

    from omniscience_connectors.plugins import PluginLoader, ConnectorPlugin
    from omniscience_connectors.registry import ConnectorRegistry

    registry = ConnectorRegistry()
    loader = PluginLoader()
    count = loader.register_all(registry)
    print(f"Loaded {count} third-party connector(s)")
"""

from omniscience_connectors.plugins.loader import ConnectorPlugin, PluginLoader
from omniscience_connectors.plugins.manifest import PluginManifest
from omniscience_connectors.plugins.sandbox import (
    PluginTimeoutError,
    SandboxConfig,
    SandboxedConnector,
    SandboxError,
)

__all__ = [
    "ConnectorPlugin",
    "PluginLoader",
    "PluginManifest",
    "PluginTimeoutError",
    "SandboxConfig",
    "SandboxError",
    "SandboxedConnector",
]
