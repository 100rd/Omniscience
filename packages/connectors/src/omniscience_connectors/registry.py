"""Connector registry — maps connector type strings to connector instances.

Usage::

    from omniscience_connectors.registry import ConnectorRegistry, get_connector

    registry = ConnectorRegistry()
    registry.register(MyConnector)

    connector = get_connector("my_connector_type")

The module-level :func:`get_connector` convenience function operates on a
shared :data:`_registry` singleton.  Built-in connectors are registered
at import time via the package's ``__init__``.
"""

from __future__ import annotations

import logging
from typing import Final

from omniscience_connectors.base import Connector

__all__ = [
    "ConnectorRegistry",
    "NotFoundError",
    "get_connector",
]

logger = logging.getLogger(__name__)


class NotFoundError(KeyError):
    """Raised when a connector type is not registered."""

    def __init__(self, connector_type: str) -> None:
        self.connector_type = connector_type
        super().__init__(f"No connector registered for type {connector_type!r}")


class ConnectorRegistry:
    """Registry that maps connector ``connector_type`` strings to connector instances.

    Instances are stored once at registration time (connectors are stateless).
    """

    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = {}

    def register(self, connector_cls: type[Connector]) -> None:
        """Register a connector class by its ``connector_type`` attribute.

        Instantiates the class with no arguments and stores the instance.
        Re-registering the same type replaces the previous entry and emits a
        warning.

        Args:
            connector_cls: A concrete :class:`~omniscience_connectors.base.Connector`
                subclass with a non-empty ``connector_type`` class variable.

        Raises:
            ValueError: If the connector class has no ``connector_type`` attribute
                or if ``connector_type`` is an empty string.
        """
        connector_type: str = getattr(connector_cls, "connector_type", "")
        if not connector_type:
            raise ValueError(
                f"Connector class {connector_cls.__name__!r} must define a non-empty "
                "'type' class variable."
            )

        if connector_type in self._connectors:
            logger.warning(
                "connector.registry.overwrite",
                extra={"connector_type": connector_type, "class": connector_cls.__name__},
            )

        instance = connector_cls()
        self._connectors[connector_type] = instance
        logger.info(
            "connector.registry.registered",
            extra={"connector_type": connector_type, "class": connector_cls.__name__},
        )

    def get(self, connector_type: str) -> Connector:
        """Return the registered connector instance for *connector_type*.

        Args:
            connector_type: The ``connector_type`` string used during registration.

        Returns:
            The registered :class:`~omniscience_connectors.base.Connector` instance.

        Raises:
            NotFoundError: If no connector is registered for *connector_type*.
        """
        try:
            connector = self._connectors[connector_type]
        except KeyError:
            logger.debug(
                "connector.registry.not_found",
                extra={"connector_type": connector_type},
            )
            raise NotFoundError(connector_type) from None

        logger.debug(
            "connector.registry.lookup",
            extra={"connector_type": connector_type, "class": type(connector).__name__},
        )
        return connector

    def registered_types(self) -> list[str]:
        """Return a sorted list of registered connector type strings."""
        return sorted(self._connectors.keys())


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_registry: Final[ConnectorRegistry] = ConnectorRegistry()


def get_connector(connector_type: str) -> Connector:
    """Look up *connector_type* in the shared module-level registry.

    Args:
        connector_type: The connector type string (e.g. ``"git"``).

    Returns:
        The registered :class:`~omniscience_connectors.base.Connector` instance.

    Raises:
        NotFoundError: If the type is not registered.
    """
    return _registry.get(connector_type)
