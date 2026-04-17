"""Omniscience core — shared types, config, logging, and telemetry."""

from omniscience_core.config import Settings
from omniscience_core.errors import (
    ConfigError,
    NotFoundError,
    OmniscienceError,
    ServiceConnectionError,
)
from omniscience_core.logging import configure_logging

__all__ = [
    "ConfigError",
    "NotFoundError",
    "OmniscienceError",
    "ServiceConnectionError",
    "Settings",
    "configure_logging",
]
