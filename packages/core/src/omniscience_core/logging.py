"""Structured logging configuration using structlog.

Call ``configure_logging`` once at application startup.  All subsequent
``structlog.get_logger()`` calls will produce JSON-formatted log records
that include the active OpenTelemetry trace/span IDs.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from opentelemetry import trace


def _inject_trace_context(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Structlog processor: inject OTel trace_id and span_id into every log event.

    When no active span exists the keys are omitted so log records outside a
    request context remain clean.
    """
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def configure_logging(log_level: str = "INFO") -> None:
    """Configure structlog for JSON output with trace context injection.

    This function is idempotent — safe to call multiple times (subsequent
    calls reconfigure structlog but do not add duplicate handlers).

    Args:
        log_level: Python log-level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    # Configure the stdlib root logger so that third-party libraries that use
    # logging.getLogger() also emit structured records via structlog.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        _inject_trace_context,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Avoid adding duplicate handlers on re-configuration.
    root.handlers = [handler]
    root.setLevel(level)
