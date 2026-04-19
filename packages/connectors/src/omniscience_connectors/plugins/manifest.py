"""Plugin manifest: parsed from ``omniscience-plugin.json`` in a plugin package.

Plugin packages ship an ``omniscience-plugin.json`` file at the package root that
supplements entry-point metadata with richer documentation and capability flags.

Example manifest::

    {
        "name": "my-github",
        "version": "1.0.0",
        "connector_type": "github-enterprise",
        "description": "Indexes GitHub Enterprise repositories",
        "author": "Acme Corp <oss@acme.com>",
        "homepage": "https://github.com/acme/omniscience-plugin-github",
        "min_omniscience_version": "0.1.0",
        "capabilities": ["webhook", "incremental"],
        "config_schema_ref": "omniscience_plugin_github.config:GitHubConfig"
    }
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from omniscience_connectors.base import Connector

__all__ = [
    "ManifestValidationError",
    "PluginManifest",
]

logger = logging.getLogger(__name__)

_MANIFEST_FILENAME = "omniscience-plugin.json"


class ManifestValidationError(ValueError):
    """Raised when a plugin manifest fails validation."""


class PluginManifest(BaseModel):
    """Parsed and validated ``omniscience-plugin.json`` manifest.

    Instances are created via :meth:`from_path` or :meth:`from_package`.
    """

    name: str
    """Logical plugin name matching the entry-point key."""

    version: str
    """Plugin package version."""

    connector_type: str
    """The connector type string that will appear in the registry."""

    description: str = ""
    """Human-readable description."""

    author: str = ""
    """Author or maintainer contact."""

    homepage: str | None = None
    """Optional project homepage URL."""

    min_omniscience_version: str = "0.1.0"
    """Minimum Omniscience version required for this plugin."""

    capabilities: list[str] = Field(default_factory=list)
    """Optional capability flags, e.g. ``["webhook", "incremental"]``."""

    config_schema_ref: str | None = None
    """Optional dotted path to the config Pydantic model, e.g.
    ``"omniscience_plugin_github.config:GitHubConfig"``."""

    @classmethod
    def from_path(cls, manifest_path: Path) -> PluginManifest:
        """Parse and validate a manifest from a file path.

        Args:
            manifest_path: Absolute or relative path to ``omniscience-plugin.json``.

        Returns:
            A validated :class:`PluginManifest` instance.

        Raises:
            ManifestValidationError: If the file is missing, not valid JSON,
                or fails schema validation.
        """
        if not manifest_path.exists():
            raise ManifestValidationError(f"Manifest file not found: {manifest_path}")

        try:
            raw: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ManifestValidationError(
                f"Manifest is not valid JSON ({manifest_path}): {exc}"
            ) from exc

        return cls._parse_raw_dict(raw, source=str(manifest_path))

    @classmethod
    def from_package(cls, package_name: str) -> PluginManifest:
        """Locate and parse the manifest bundled with an installed package.

        Searches the package directory for ``omniscience-plugin.json``.

        Args:
            package_name: The importable Python package name
                (e.g. ``"omniscience_plugin_github"``).

        Returns:
            A validated :class:`PluginManifest` instance.

        Raises:
            ManifestValidationError: If the package is not importable, the
                manifest is missing, or validation fails.
        """
        try:
            mod = importlib.import_module(package_name)
        except ImportError as exc:
            raise ManifestValidationError(
                f"Cannot import package {package_name!r}: {exc}"
            ) from exc

        pkg_file = getattr(mod, "__file__", None)
        if pkg_file is None:
            raise ManifestValidationError(
                f"Package {package_name!r} has no __file__ — cannot locate manifest"
            )

        pkg_dir = Path(pkg_file).parent
        manifest_path = pkg_dir / _MANIFEST_FILENAME

        return cls.from_path(manifest_path)

    def validate_connector_class(self, connector_cls: type[Connector]) -> None:
        """Sanity-check that *connector_cls* matches the manifest declaration.

        Checks:
        - ``connector_cls`` is a :class:`~omniscience_connectors.base.Connector` subclass
        - ``connector_cls.connector_type`` matches :attr:`connector_type`
        - ``connector_cls.config_schema`` is defined

        Args:
            connector_cls: The loaded connector class to validate.

        Raises:
            ManifestValidationError: On any mismatch.
        """
        if not isinstance(connector_cls, type) or not issubclass(connector_cls, Connector):
            raise ManifestValidationError(f"{connector_cls!r} must be a Connector subclass")

        actual_type: str = getattr(connector_cls, "connector_type", "")
        if actual_type != self.connector_type:
            raise ManifestValidationError(
                f"connector_type mismatch: manifest declares {self.connector_type!r} "
                f"but class has {actual_type!r}"
            )

        if getattr(connector_cls, "config_schema", None) is None:
            raise ManifestValidationError(
                f"{connector_cls.__name__!r} must define a 'config_schema' class variable"
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @classmethod
    def _parse_raw_dict(cls, raw: dict[str, Any], *, source: str) -> PluginManifest:
        """Validate a raw dict and return a :class:`PluginManifest`."""
        required = ("name", "version", "connector_type")
        missing = [k for k in required if not raw.get(k)]
        if missing:
            raise ManifestValidationError(
                f"Manifest {source!r} is missing required fields: {missing}"
            )

        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise ManifestValidationError(f"Manifest {source!r} failed validation: {exc}") from exc
