"""Telemetry sub-package: OTel initialisation and Prometheus metrics."""

from omniscience_core.telemetry.clients import (
    ANONYMOUS_TOKEN_ID,
    MCP_SESSIONS_ACTIVE,
    MCP_SESSIONS_TOTAL,
    MCP_TOOL_INVOCATIONS_TOTAL,
    REQUESTS_TOTAL,
    RESPONSE_BYTES_TOTAL,
    ClientTelemetryRegistry,
    McpSessionStats,
    TokenSnapshot,
    ToolSnapshot,
    get_client_registry,
)
from omniscience_core.telemetry.metrics import (
    FRESHNESS_AGE_SECONDS,
    FRESHNESS_STALE_TOTAL,
    REQUEST_COUNT,
    REQUEST_DURATION,
    REQUEST_IN_PROGRESS,
)
from omniscience_core.telemetry.otel import init_telemetry

__all__ = [
    "ANONYMOUS_TOKEN_ID",
    "FRESHNESS_AGE_SECONDS",
    "FRESHNESS_STALE_TOTAL",
    "MCP_SESSIONS_ACTIVE",
    "MCP_SESSIONS_TOTAL",
    "MCP_TOOL_INVOCATIONS_TOTAL",
    "REQUESTS_TOTAL",
    "REQUEST_COUNT",
    "REQUEST_DURATION",
    "REQUEST_IN_PROGRESS",
    "RESPONSE_BYTES_TOTAL",
    "ClientTelemetryRegistry",
    "McpSessionStats",
    "TokenSnapshot",
    "ToolSnapshot",
    "get_client_registry",
    "init_telemetry",
]
