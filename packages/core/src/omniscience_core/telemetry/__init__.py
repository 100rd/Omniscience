"""Telemetry sub-package: OTel initialisation and Prometheus metrics."""

from omniscience_core.telemetry.metrics import (
    REQUEST_COUNT,
    REQUEST_DURATION,
    REQUEST_IN_PROGRESS,
)
from omniscience_core.telemetry.otel import init_telemetry

__all__ = [
    "REQUEST_COUNT",
    "REQUEST_DURATION",
    "REQUEST_IN_PROGRESS",
    "init_telemetry",
]
