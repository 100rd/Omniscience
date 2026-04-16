"""ASGI middleware for request tracing, logging context, and Prometheus metrics."""

from __future__ import annotations

import time
import uuid

import structlog
from omniscience_core.telemetry.metrics import (
    REQUEST_COUNT,
    REQUEST_DURATION,
    REQUEST_IN_PROGRESS,
)
from opentelemetry import trace
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match
from starlette.types import ASGIApp

log = structlog.get_logger(__name__)


def _resolve_path_template(request: Request) -> str:
    """Return the matched route path template (e.g. /sources/{id}) or raw path."""
    for route in request.app.routes:
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            return str(getattr(route, "path", request.url.path))
    return request.url.path


class TracingMiddleware(BaseHTTPMiddleware):
    """Per-request OTel span + structlog context + Prometheus recording.

    For every incoming HTTP request this middleware:
    1. Generates a ``request_id`` (UUID4) for correlation.
    2. Reads the active OTel span (created by OTel ASGI middleware upstream,
       if configured) and extracts trace/span IDs.
    3. Binds ``trace_id``, ``span_id``, ``request_id``, ``method``, and
       ``path`` into structlog contextvars so every log line in the request
       scope carries these fields automatically.
    4. Increments the in-progress gauge, records request duration histogram,
       and increments the total-requests counter after response.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        request_id = str(uuid.uuid4())
        path_template = _resolve_path_template(request)
        method = request.method

        # --- Bind structlog context for this request scope ---
        span = trace.get_current_span()
        ctx = span.get_span_context()
        trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else ""
        span_id = format(ctx.span_id, "016x") if ctx.is_valid else ""

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            trace_id=trace_id,
            span_id=span_id,
            method=method,
            path=path_template,
        )

        REQUEST_IN_PROGRESS.labels(method=method, path=path_template).inc()
        start = time.perf_counter()

        try:
            response = await call_next(request)
            status_code = str(response.status_code)
        except Exception:
            status_code = "500"
            raise
        finally:
            duration = time.perf_counter() - start
            REQUEST_IN_PROGRESS.labels(method=method, path=path_template).dec()
            REQUEST_DURATION.labels(method=method, path=path_template).observe(duration)
            REQUEST_COUNT.labels(method=method, path=path_template, status_code=status_code).inc()
            log.info(
                "request_completed",
                status_code=status_code,
                duration_s=round(duration, 4),
            )

        return response
