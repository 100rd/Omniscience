"""Prometheus metrics for the ingestion pipeline.

All metric names are prefixed with ``omniscience_ingestion_`` to avoid
collisions with the queue and HTTP metrics defined elsewhere.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

# ---------------------------------------------------------------------------
# Document-level counters
# ---------------------------------------------------------------------------

INGESTION_DOCUMENTS_PROCESSED_TOTAL: Counter = Counter(
    name="omniscience_ingestion_documents_processed_total",
    documentation=(
        "Total number of documents processed by the ingestion pipeline, "
        "labelled by source type and effective action."
    ),
    labelnames=["source_type", "action"],
)

INGESTION_ERRORS_TOTAL: Counter = Counter(
    name="omniscience_ingestion_errors_total",
    documentation=("Total number of per-stage errors encountered during ingestion."),
    labelnames=["source_type", "stage"],
)

# ---------------------------------------------------------------------------
# Stage-level duration histogram
# ---------------------------------------------------------------------------

INGESTION_STAGE_DURATION_SECONDS: Histogram = Histogram(
    name="omniscience_ingestion_stage_duration_seconds",
    documentation=(
        "Time spent inside each pipeline stage, in seconds. "
        "Labelled by stage name: fetch, hash_check, parse, chunk, embed, index."
    ),
    labelnames=["stage"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

__all__ = [
    "INGESTION_DOCUMENTS_PROCESSED_TOTAL",
    "INGESTION_ERRORS_TOTAL",
    "INGESTION_STAGE_DURATION_SECONDS",
]
