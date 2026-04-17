"""Prometheus metrics for the NATS JetStream queue subsystem.

All metric names are prefixed with ``omniscience_queue_`` to avoid
collisions with the HTTP metrics defined in ``telemetry/metrics.py``.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

# ---------------------------------------------------------------------------
# Publish metrics
# ---------------------------------------------------------------------------

QUEUE_PUBLISHED_TOTAL: Counter = Counter(
    name="omniscience_queue_published_total",
    documentation="Total number of messages successfully published to NATS JetStream.",
    labelnames=["subject"],
)

# ---------------------------------------------------------------------------
# Consume metrics
# ---------------------------------------------------------------------------

QUEUE_CONSUMED_TOTAL: Counter = Counter(
    name="omniscience_queue_consumed_total",
    documentation="Total number of messages consumed from NATS JetStream.",
    labelnames=["subject", "status"],
)

QUEUE_DLQ_TOTAL: Counter = Counter(
    name="omniscience_queue_dlq_total",
    documentation="Total number of messages forwarded to the Dead Letter Queue.",
    labelnames=["subject"],
)

QUEUE_PROCESSING_DURATION_SECONDS: Histogram = Histogram(
    name="omniscience_queue_processing_duration_seconds",
    documentation="Time spent processing a single NATS JetStream message.",
    labelnames=["subject"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
