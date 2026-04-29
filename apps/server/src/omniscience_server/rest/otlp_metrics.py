"""Prometheus metrics for the OTLP/HTTP trace receiver (#152).

Two families:

* :data:`OTEL_SPANS_RECEIVED_TOTAL` — counter of received spans, partitioned
  by whether the bearer-token-derived workspace was present.  The label
  ``workspace_id_kind`` follows the ``as_of_kind`` precedent — the metric
  cardinality is bounded to ``"present"`` / ``"missing"`` so a runaway
  client cannot blow out Prometheus storage.

* :data:`OTEL_REQUEST_DURATION_SECONDS` — histogram of OTLP/HTTP route
  latency, labelled by content-type kind (``json``/``protobuf``/``other``)
  and outcome (``accepted``/``rejected``).
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

OTEL_SPANS_RECEIVED_TOTAL: Counter = Counter(
    name="omniscience_otel_spans_received_total",
    documentation=(
        "Total OpenTelemetry spans received by the OTLP/HTTP trace receiver "
        "(issue #152). The ``workspace_id_kind`` label is ``present`` when "
        "the bearer token resolved to a non-NULL workspace_id, ``missing`` "
        "for legacy tokens predating workspace scoping (still ingested but "
        "isolated to the legacy bucket)."
    ),
    labelnames=["workspace_id_kind"],
)


OTEL_REQUEST_DURATION_SECONDS: Histogram = Histogram(
    name="omniscience_otel_request_duration_seconds",
    documentation=(
        "Wall-clock duration of OTLP/HTTP trace export requests in seconds. "
        "Includes auth, decode, and persistence (issue #152). Outcome is "
        "``accepted`` for 2xx responses and ``rejected`` for 4xx/5xx."
    ),
    labelnames=["content_type_kind", "outcome"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)


__all__ = [
    "OTEL_REQUEST_DURATION_SECONDS",
    "OTEL_SPANS_RECEIVED_TOTAL",
]
