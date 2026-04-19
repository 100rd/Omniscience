"""omniscience-plugin-example — Minimal Omniscience connector plugin.

This package demonstrates the minimum required structure for a third-party
Omniscience connector plugin.  It registers an ``ExampleConnector`` under the
``omniscience.connectors`` entry-point group so that Omniscience's
:class:`~omniscience_connectors.plugins.PluginLoader` can discover it.
"""

from omniscience_plugin_example.connector import ExampleConfig, ExampleConnector

__all__ = ["ExampleConfig", "ExampleConnector"]
