"""Prometheus metrics for the ingestion pipeline.

All metric names are prefixed with ``omniscience_ingestion_`` to avoid
collisions with the queue and HTTP metrics defined elsewhere.

Dedup metrics (issue #164)
--------------------------

Three counters and one gauge cover the operator-vs-agentic deduplication
state machine.  Labels intentionally omit ``workspace_id`` to avoid
high-cardinality leakage of tenant identity through the metrics endpoint
(same posture as #163).  ``kind`` is the parsed K8s kind (e.g. ``Pod``,
``Deployment``); for non-k8s emitters the kind label is set to
``"unknown"`` and never used by the dedup logic in v0.2.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

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

# ---------------------------------------------------------------------------
# Dedup counters (issue #164)
# ---------------------------------------------------------------------------

# Drops: an event arrived from a non-authoritative emitter and was silently
# discarded.  The two label dimensions are intentional:
#   - ``kind``            — K8s API kind (Pod, Deployment, …).  Cardinality
#                           bounded by the operator's coverage matrix
#                           (single-digit dozens).
#   - ``dropped_emitter`` — the emitter whose event was dropped.
#   - ``authority_emitter`` — the emitter that owns the (workspace, external_id)
#                             pair right now.
# In v0.2 the only meaningful tuple is
# ``(*, k8s-agentic, k8s-operator)`` — agentic events dropped because the
# operator is authoritative.  Other tuples are reserved for future emitters.
INGESTION_DEDUP_DROP_TOTAL: Counter = Counter(
    name="omniscience_ingestion_dedup_drop_total",
    documentation=(
        "Events dropped by the ingestion-worker dedup gate because the "
        "incoming emitter is not authoritative for the "
        "(workspace_id, external_id) pair (issue #164). "
        "No workspace_id label by design — see metrics.py module docstring."
    ),
    labelnames=["kind", "dropped_emitter", "authority_emitter"],
)

# Authority transitions: counts the legal flips of the authority pointer.
# Two transitions are legal:
#   - agentic -> operator: the operator's coverage now covers a kind the
#     agentic connector was previously emitting.  Operator wins.
#   - operator -> agentic: the operator stopped emitting (e.g. uninstalled
#     or crashed) and the TTL window expired.  Agentic re-takes authority.
INGESTION_DEDUP_AUTHORITY_TRANSITIONS_TOTAL: Counter = Counter(
    name="omniscience_ingestion_dedup_authority_transitions_total",
    documentation=(
        "Authority transitions on the entity_emitter table (issue #164). "
        "from='k8s-agentic' to='k8s-operator' is the operator-supersedes-"
        "agentic flow; from='k8s-operator' to='k8s-agentic' is the "
        "TTL-expired re-assignment flow (operator uninstalled or down)."
    ),
    labelnames=["kind", "from_emitter", "to_emitter"],
)

# Convenience aliases honouring the synonyms the prompt and the issue use
# interchangeably.  Both names point to a single underlying time-series so
# dashboards that key on either label set produce identical totals.  The
# call sites use the more descriptive ``DROP`` / ``TRANSITIONS`` counters
# above and the symbolic action below; the canonical action namespace is:
#
#   "operator_supersedes_agentic" — operator emit took authority away
#                                   from agentic (logged via the
#                                   transitions counter, mirrored here
#                                   so action-keyed dashboards can graph
#                                   it without joining two series).
#   "no_op_agentic_dropped"       — agentic emit dropped because the
#                                   operator is authoritative.
#   "ttl_reassign_to_agentic"     — agentic emit reclaimed authority
#                                   after the operator's TTL expired.
INGESTION_DEDUP_ACTION_TOTAL: Counter = Counter(
    name="omniscience_ingestion_dedup_total",
    documentation=(
        "Coarse-grained dedup actions (issue #164). One increment per "
        "decision; the kind label is the K8s kind parsed from the "
        "external_id when known. action is one of "
        "operator_supersedes_agentic / no_op_agentic_dropped / "
        "ttl_reassign_to_agentic / no_op_idempotent_refresh / "
        "first_authority_assigned."
    ),
    labelnames=["kind", "action"],
)


# Authority gauge: per-emitter row counts on the entity_emitter table.
# Sampled by an admin endpoint or on-demand probe — NOT updated on every
# event, since the natural value-of-truth is a single SELECT count(*) and
# that's where the gauge gets refreshed.  Cardinality is bounded by
# kind * emitter, both small finite sets.
INGESTION_DEDUP_AUTHORITY_COUNT: Gauge = Gauge(
    name="omniscience_ingestion_dedup_authority_count",
    documentation=(
        "Number of entity_emitter rows currently held by each emitter, "
        "sampled by the dedup admin probe (issue #164). Updated lazily; "
        "do not rely on per-event freshness."
    ),
    labelnames=["kind", "emitter"],
)


__all__ = [
    "INGESTION_DEDUP_ACTION_TOTAL",
    "INGESTION_DEDUP_AUTHORITY_COUNT",
    "INGESTION_DEDUP_AUTHORITY_TRANSITIONS_TOTAL",
    "INGESTION_DEDUP_DROP_TOTAL",
    "INGESTION_DOCUMENTS_PROCESSED_TOTAL",
    "INGESTION_ERRORS_TOTAL",
    "INGESTION_STAGE_DURATION_SECONDS",
]
