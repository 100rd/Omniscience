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

# ---------------------------------------------------------------------------
# Freshness SLO metrics
# ---------------------------------------------------------------------------

# Per-source age in seconds since last successful sync.
# Labels: source_id, source_name
FRESHNESS_AGE_SECONDS: Gauge = Gauge(
    name="omniscience_source_freshness_age_seconds",
    documentation=(
        "Age in seconds since the source was last successfully synced. "
        "Set to a large sentinel value (1e15) when the source has never been synced."
    ),
    labelnames=["source_id", "source_name"],
)

# Count of sources currently violating their freshness SLO.
FRESHNESS_STALE_TOTAL: Gauge = Gauge(
    name="omniscience_source_stale_total",
    documentation="Number of sources currently exceeding their freshness_sla_seconds budget.",
)

# ---------------------------------------------------------------------------
# Scheduler metrics
# ---------------------------------------------------------------------------

# Total re-syncs triggered by the scheduler, labelled by source_type.
SCHEDULER_SYNCS_TRIGGERED_TOTAL: Counter = Counter(
    name="omniscience_scheduler_syncs_triggered_total",
    documentation=(
        "Total number of automatic re-sync triggers published by the scheduler worker. "
        "Incremented once per stale source per check cycle."
    ),
    labelnames=["source_type"],
)

# Histogram of scheduler check cycle wall-clock duration.
SCHEDULER_CHECK_DURATION_SECONDS: Histogram = Histogram(
    name="omniscience_scheduler_check_duration_seconds",
    documentation="Wall-clock time in seconds for a single scheduler check-and-trigger cycle.",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# ---------------------------------------------------------------------------
# Retention worker metrics (ADR-0009 §8, issue #135)
# ---------------------------------------------------------------------------
#
# Four metric families specified verbatim in ADR-0009 §8:
#   * `omniscience_graph_records_total{tier, store}` — gauge, per-tier
#     per-store record count, scraped after each retention run.
#   * `omniscience_retention_eviction_total{transition, store}` — counter,
#     increments per record evicted.
#   * `omniscience_retention_worker_duration_seconds{phase, store}` —
#     histogram, per-phase per-store wall time.
#   * `omniscience_retention_worker_lag_seconds` — gauge, wall time
#     since the oldest `recorded_at`-overdue record was evicted.
#
# SLO: lag stays below 24 hours steady-state; >7d is P1 (existing
# freshness-style alert path).

# Per-tier per-store record count (gauge).  Tier label values:
#   "hot"     — live store, full bitemporal fidelity
#   "warm"    — :EntitySnapshot:Daily rows in same Neo4j db
#   "archive" — Parquet on object storage (always 0 for store=qdrant)
# Store label values: "neo4j" | "qdrant".
RETENTION_GRAPH_RECORDS_TOTAL: Gauge = Gauge(
    name="omniscience_graph_records_total",
    documentation=(
        "Per-tier per-store record count, scraped after each retention run "
        "(ADR-0009 §8). Set by the retention worker on every successful tick."
    ),
    labelnames=["tier", "store"],
)

# Total records evicted, partitioned by transition and store (counter).
# Transition label values:
#   "hot_to_warm"     — :EntityState/edge to :EntitySnapshot:Daily
#   "warm_to_archive" — :EntitySnapshot:Daily to S3 parquet
# Store label values: "neo4j" | "qdrant".
RETENTION_EVICTION_TOTAL: Counter = Counter(
    name="omniscience_retention_eviction_total",
    documentation=(
        "Total records evicted by tier transition (ADR-0009 §8). "
        "Increments per record on the move phase of every successful run."
    ),
    labelnames=["transition", "store"],
)

# Per-phase per-store retention worker duration (histogram).
# Phase label values: "read" | "mark" | "move"  (ADR-0009 §3 read-then-
# mark-then-move).
# Store label values: "neo4j" | "qdrant".
RETENTION_WORKER_DURATION_SECONDS: Histogram = Histogram(
    name="omniscience_retention_worker_duration_seconds",
    documentation=(
        "Wall-clock time in seconds for one phase (read/mark/move) of the "
        "retention worker against one store, per workspace iteration "
        "(ADR-0009 §8). Buckets cover 10ms to 5min — the worker should "
        "complete a full per-workspace per-phase cycle inside this band."
    ),
    labelnames=["phase", "store"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0),
)

# Retention worker lag in seconds (gauge).  Wall time since the oldest
# `recorded_at`-overdue record was evicted, aggregated as `max` for the
# deployment-wide alert.  ADR-0009 §8 SLO: <24h steady; >7d is P1.
RETENTION_WORKER_LAG_SECONDS: Gauge = Gauge(
    name="omniscience_retention_worker_lag_seconds",
    documentation=(
        "Wall-clock seconds since the oldest record overdue for eviction "
        "was last evicted, deployment-wide max (ADR-0009 §8). SLO: <24h "
        "steady; >7d trips a P1 alert via the freshness alert path."
    ),
)
