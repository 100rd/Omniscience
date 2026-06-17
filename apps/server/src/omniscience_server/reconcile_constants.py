"""Named constants for the reconcile worker.

The reconcile worker is the compensation mechanism for the non-atomic
triple-write (Postgres + Qdrant + Neo4j) in ``IndexWriter.upsert_document``
/ ``upsert_graph``.  On each tick it cross-checks the three stores and
repairs drift idempotently.

These constants follow the same conventions as ``retention_constants.py``:
every tuneable value lives here with a docstring that explains the rationale.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Worker shape
# ---------------------------------------------------------------------------

#: Worker tick interval in seconds.  The default of 1 hour (3600 s) is
#: aggressive enough to catch newly created orphans from a Qdrant restart
#: within an SLO-friendly window, without adding measurable load to
#: Qdrant or Neo4j.  Operators may lower this to 300 s in dev or raise it
#: to 6 h in large deployments (following the retention-worker pattern).
RECONCILE_TICK_SECONDS_DEFAULT: Final[int] = 3600

#: Maximum number of orphan Qdrant points deleted per reconcile tick.
#: Bounded so a single misbehaving run does not wipe thousands of points
#: without an operator noticing the ``omniscience_reconcile_drift_total``
#: spike first.  Remaining orphans are cleaned up on the next tick.
RECONCILE_ORPHAN_DELETE_LIMIT: Final[int] = 200

# ---------------------------------------------------------------------------
# Drift type labels (Prometheus counter label values)
# ---------------------------------------------------------------------------

#: Postgres document exists but no corresponding chunks in Qdrant.
#: Indicates PG write succeeded but Qdrant write failed or was skipped.
DRIFT_PG_MISSING_QDRANT: Final[str] = "pg_missing_qdrant"

#: Chunks in Qdrant reference a source_id that has no active Postgres
#: document.  Orphan from seed scripts, partial deletes, or tombstone
#: race conditions.
DRIFT_QDRANT_ORPHAN: Final[str] = "qdrant_orphan"

#: Entity nodes in Neo4j reference a source_id that has no active Postgres
#: document.  Neo4j cleanup is deferred to the retention worker
#: (end-dating / ADR-0009); this worker only records the metric.
DRIFT_NEO4J_ORPHAN: Final[str] = "neo4j_orphan"

# ---------------------------------------------------------------------------
# Store labels (reuse from retention for consistency)
# ---------------------------------------------------------------------------

STORE_LABEL_QDRANT: Final[str] = "qdrant"
STORE_LABEL_NEO4J: Final[str] = "neo4j"
STORE_LABEL_POSTGRES: Final[str] = "postgres"


__all__ = [
    "DRIFT_NEO4J_ORPHAN",
    "DRIFT_PG_MISSING_QDRANT",
    "DRIFT_QDRANT_ORPHAN",
    "RECONCILE_ORPHAN_DELETE_LIMIT",
    "RECONCILE_TICK_SECONDS_DEFAULT",
    "STORE_LABEL_NEO4J",
    "STORE_LABEL_POSTGRES",
    "STORE_LABEL_QDRANT",
]
