"""Plugin loader: discovers and loads connector plugins from installed packages.

Plugins register themselves via Python entry points using the group name
``omniscience.connectors``.  Each entry point value must be a fully-qualified
dotted path to a :class:`~omniscience_connectors.base.Connector` subclass, e.g.::

    [project.entry-points."omniscience.connectors"]
    my-github = "omniscience_plugin_github.connector:GitHubEnterpriseConnector"

The entry-point name (``my-github`` above) becomes the plugin's logical name.
"""

from __future__ import annotations

import importlib
import logging
from importlib.metadata import entry_points
from typing import cast

from pydantic import BaseModel

from omniscience_connectors.base import Connector
from omniscience_connectors.registry import ConnectorRegistry

__all__ = [
    "ConnectorPlugin",
    "PluginLoadError",
    "PluginLoader",
]

logger = logging.getLogger(__name__)

_ENTRY_POINT_GROUP = "omniscience.connectors"


class PluginLoadError(RuntimeError):
    """Raised when a connector plugin cannot be imported or validated."""

    def __init__(self, plugin_name: str, reason: str) -> None:
        self.plugin_name = plugin_name
        self.reason = reason
        super().__init__(f"Failed to load plugin {plugin_name!r}: {reason}")


class ConnectorPlugin(BaseModel):
    """Metadata for an installable connector plugin.

    Populated from the entry point and, when available, supplemented by the
    ``omniscience-plugin.json`` manifest shipped with the plugin package.
    """

    name: str
    """Logical plugin name (the entry-point key)."""

    version: str
    """Package version string (from importlib.metadata)."""

    connector_type: str
    """The ``connector_type`` string that will be used in the registry."""

    module_path: str
    """Dotted import path to the module, e.g. ``omniscience_plugin_github.connector``."""

    class_name: str
    """Unqualified class name inside the module, e.g. ``GitHubEnterpriseConnector``."""

    description: str = ""
    """Human-readable description of what this connector does."""

    author: str = ""
    """Plugin author or maintainer."""

    homepage: str | None = None
    """Optional URL for project homepage or documentation."""


class PluginLoader:
    """Discovers and loads connector plugins from installed packages.

    Discovery relies on Python's ``importlib.metadata`` entry points mechanism.
    Each installed package that provides connectors must declare them under the
    ``omniscience.connectors`` entry-point group.
    """

    def discover_plugins(self) -> list[ConnectorPlugin]:
        """Find plugins declared via entry points (group: ``omniscience.connectors``).

        Returns a list of :class:`ConnectorPlugin` instances, one per entry point.
        Entry points that cannot be introspected are skipped with a warning.

        Returns:
            List of discovered :class:`ConnectorPlugin` metadata objects.
        """
        eps = entry_points(group=_ENTRY_POINT_GROUP)
        plugins: list[ConnectorPlugin] = []

        for ep in eps:
            try:
                plugin = self._ep_to_plugin(ep)
            except Exception as exc:
                logger.warning(
                    "connector.plugin.discovery_skip: entry_point=%s reason=%s",
                    ep.name,
                    str(exc),
                )
                continue
            plugins.append(plugin)
            logger.debug(
                "connector.plugin.discovered: plugin=%s module=%s",
                plugin.name,
                plugin.module_path,
            )

        logger.info("connector.plugin.discovery_complete: count=%d", len(plugins))
        return plugins

    def load_plugin(self, plugin: ConnectorPlugin) -> type[Connector]:
        """Import and validate the connector class referenced by *plugin*.

        Args:
            plugin: Plugin metadata produced by :meth:`discover_plugins`.

        Returns:
            The validated :class:`~omniscience_connectors.base.Connector` subclass.

        Raises:
            PluginLoadError: If the module cannot be imported, the class is
                missing, or the class does not satisfy the Connector contract.
        """
        try:
            module = importlib.import_module(plugin.module_path)
        except ImportError as exc:
            raise PluginLoadError(plugin.name, f"cannot import module: {exc}") from exc

        cls = getattr(module, plugin.class_name, None)
        if cls is None:
            raise PluginLoadError(
                plugin.name,
                f"module {plugin.module_path!r} has no attribute {plugin.class_name!r}",
            )

        self._validate_connector_class(plugin.name, cls)

        logger.info(
            "connector.plugin.loaded: plugin=%s connector_type=%s class=%s",
            plugin.name,
            plugin.connector_type,
            plugin.class_name,
        )
        return cast("type[Connector]", cls)

    def register_all(self, registry: ConnectorRegistry) -> int:
        """Discover, load, and register all available plugins.

        Plugins that fail to load are skipped with an error log — a single bad
        plugin does not prevent other plugins from loading.

        Args:
            registry: The :class:`~omniscience_connectors.registry.ConnectorRegistry`
                to register loaded connectors into.

        Returns:
            Number of connectors successfully registered.
        """
        plugins = self.discover_plugins()
        registered = 0

        for plugin in plugins:
            try:
                connector_cls = self.load_plugin(plugin)
                registry.register(connector_cls)
                registered += 1
            except Exception as exc:
                logger.error(
                    "connector.plugin.register_failed: plugin=%s error=%s",
                    plugin.name,
                    str(exc),
                )

        logger.info(
            "connector.plugin.register_complete: registered=%d total=%d",
            registered,
            len(plugins),
        )
        return registered

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ep_to_plugin(ep: object) -> ConnectorPlugin:
        """Convert a single entry point object to a :class:`ConnectorPlugin`."""
        # ep.value is "module.path:ClassName"
        value: str = ep.value  # type: ignore[attr-defined]
        ep_name: str = ep.name  # type: ignore[attr-defined]

        if ":" not in value:
            raise ValueError(f"Entry point value {value!r} must be 'module.path:ClassName'")

        module_path, class_name = value.rsplit(":", 1)

        # Resolve package version via the distribution that owns this entry point
        try:
            dist = ep.dist  # type: ignore[attr-defined]
            version: str = dist.version if dist is not None else "unknown"
            dist_meta = dist.metadata if dist is not None else {}
            description: str = dist_meta.get("Summary", "") or ""
            author: str = dist_meta.get("Author", "") or dist_meta.get("Author-email", "") or ""
            homepage: str | None = dist_meta.get("Home-page") or dist_meta.get("Project-URL")
        except AttributeError:
            version = "unknown"
            description = ""
            author = ""
            homepage = None

        # connector_type is derived from the class; we'll resolve it after import
        # if needed. For now, use the entry-point name as a best-effort.
        connector_type = ep_name

        return ConnectorPlugin(
            name=ep_name,
            version=version,
            connector_type=connector_type,
            module_path=module_path,
            class_name=class_name,
            description=description,
            author=author,
            homepage=homepage,
        )

    @staticmethod
    def _validate_connector_class(plugin_name: str, cls: object) -> None:
        """Raise :class:`PluginLoadError` if *cls* is not a valid Connector subclass."""
        if not isinstance(cls, type):
            raise PluginLoadError(plugin_name, f"{cls!r} is not a class")

        if not issubclass(cls, Connector):
            raise PluginLoadError(
                plugin_name,
                f"{cls.__name__!r} must subclass Connector",
            )

        connector_type: str = getattr(cls, "connector_type", "")
        if not connector_type:
            raise PluginLoadError(
                plugin_name,
                f"{cls.__name__!r} must define a non-empty 'connector_type' class variable",
            )

        config_schema = getattr(cls, "config_schema", None)
        if config_schema is None:
            raise PluginLoadError(
                plugin_name,
                f"{cls.__name__!r} must define a 'config_schema' class variable",
            )
