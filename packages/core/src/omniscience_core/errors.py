"""Domain error hierarchy for Omniscience."""

from __future__ import annotations


class OmniscienceError(Exception):
    """Base class for all Omniscience application errors."""


class ConfigError(OmniscienceError):
    """Raised when configuration is missing or invalid."""


class ServiceConnectionError(OmniscienceError):
    """Raised when a required service connection cannot be established."""


class NotFoundError(OmniscienceError):
    """Raised when a requested resource does not exist."""
