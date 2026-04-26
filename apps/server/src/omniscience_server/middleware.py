"""ASGI middleware for request tracing, logging context, and Prometheus metrics.

Two middleware classes live here:

* :class:`TracingMiddleware` — request id, OTel span context, structlog
  binding, request-latency histogram (the original; unchanged).
* :class:`TelemetryMiddleware` — connected-clients telemetry (issue #113).
  Records per-token request counts, last-seen timestamps, and bytes-out
  on the in-process registry; emits Prometheus counters for downstream
  scrapers.

The two are intentionally separate classes — one handles the cross-
cutting trace/log scope, the other handles the per-token telemetry —
so the failure modes do not couple.  TelemetryMiddleware is appended
*after* the auth dependency has had a chance to populate
``request.state.api_token``; we read that field if present and fall
back to the literal ``"anonymous"`` token id when absent.
"""

from __future__ import annotations

import time
import uuid

import structlog
from omniscience_core.auth.scopes import Scope
from omniscience_core.db.models import ApiToken
from omniscience_core.telemetry.clients import (
    ANONYMOUS_TOKEN_ID,
    ClientTelemetryRegistry,
    get_client_registry,
)
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
    return "unmatched"


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


# ---------------------------------------------------------------------------
# Connected-clients telemetry (issue #113)
# ---------------------------------------------------------------------------


def _resolve_token_id(request: Request) -> tuple[str, str]:
    """Return ``(token_id_label, scope_set_label)`` for the current request.

    Reads ``request.state.api_token`` which is populated by
    :func:`omniscience_core.auth.middleware.get_current_token` for any
    authenticated route.  Unauthenticated requests use the
    :data:`ANONYMOUS_TOKEN_ID` sentinel and an empty scope set.
    """
    token = getattr(request.state, "api_token", None)
    if not isinstance(token, ApiToken):
        return ANONYMOUS_TOKEN_ID, ""
    valid_scopes = sorted(s for s in token.scopes if s in Scope.__members__.values())
    return str(token.id), ",".join(valid_scopes)


def _resolve_response_bytes(response: Response) -> int:
    """Best-effort response body size.

    Strategy
    --------
    * Prefer the ``Content-Length`` header — it is present on every plain
      :class:`Response` and on any response a route returns directly.
    * For streaming responses (no Content-Length) we deliberately return
      ``0`` rather than buffer the body just to count bytes; the spec
      explicitly accepts ``len(body)`` only when the body is fully
      buffered.  Buffering a stream just to measure its size would defeat
      the point of streaming and risk OOM on large payloads.
    """
    raw = response.headers.get("content-length")
    if raw is None:
        return 0
    try:
        size = int(raw)
    except ValueError:
        return 0
    return max(0, size)


class TelemetryMiddleware(BaseHTTPMiddleware):
    """Per-request connected-clients telemetry (issue #113).

    Records: request count, last-seen timestamp, bytes-out (Content-
    Length) per resolved token id.  Prometheus counters
    ``omniscience_requests_total`` and ``omniscience_response_bytes_total``
    are incremented as a side effect so external scrapers stay in sync
    with the in-memory rolling state.

    Failure isolation: the registry call is wrapped in a try/except so a
    bug in the telemetry path can never take down a real request.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        registry: ClientTelemetryRegistry | None = None,
    ) -> None:
        super().__init__(app)
        self._registry = registry or get_client_registry()

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        response = await call_next(request)
        try:
            token_id, scope_set = _resolve_token_id(request)
            bytes_out = _resolve_response_bytes(response)
            self._registry.record_request(
                token_id=token_id,
                bytes_out=bytes_out,
                scope_set=scope_set,
            )
        except Exception as exc:  # pragma: no cover - defensive only
            log.warning("telemetry_record_failed", error=str(exc))
        return response
