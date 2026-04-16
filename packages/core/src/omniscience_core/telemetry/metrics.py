"""Centralised Prometheus metrics registry.

Import ``REQUEST_COUNT``, ``REQUEST_DURATION``, and ``REQUEST_IN_PROGRESS``
from this module rather than creating new metric instances in each file.

Double-registration is prevented by module-level singletons — the
``prometheus_client`` library also raises ``ValueError`` on re-registration,
but we guard against that for safety.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# Total HTTP requests processed, labelled by method, path template, and status code.
REQUEST_COUNT: Counter = Counter(
    name="omniscience_http_requests_total",
    documentation="Total number of HTTP requests.",
    labelnames=["method", "path", "status_code"],
)

# Request latency distribution in seconds.
REQUEST_DURATION: Histogram = Histogram(
    name="omniscience_http_request_duration_seconds",
    documentation="HTTP request latency in seconds.",
    labelnames=["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# Number of HTTP requests currently being processed.
REQUEST_IN_PROGRESS: Gauge = Gauge(
    name="omniscience_http_requests_in_progress",
    documentation="Number of HTTP requests currently in flight.",
    labelnames=["method", "path"],
)
