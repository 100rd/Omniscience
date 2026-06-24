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

#: Maximum number of entities re-emitted per reconcile tick (AP2 bounded
#: re-emit).  A cursor advances monotonically within each worker instance
#: so that large workspaces do not produce an unbounded single-transaction
#: flood of OutboxEvents.  Remainder is processed on the next tick.
#: Overridable via ``REEMIT_TICK_CAP`` environment variable.
REEMIT_TICK_CAP_DEFAULT: Final[int] = 1000

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
#: document.  Neo4j orphans are now actively healed (end-dated) by the
#: reconcile worker in addition to emitting the metric.
DRIFT_NEO4J_ORPHAN: Final[str] = "neo4j_orphan"

#: An edge in Neo4j has a different version than the Postgres SoT row.
#: The reconcile worker re-emits an ``edge.upsert`` OutboxEvent to repair.
DRIFT_EDGE: Final[str] = "edge_drift"

# ---------------------------------------------------------------------------
# Store labels (reuse from retention for consistency)
# ---------------------------------------------------------------------------

STORE_LABEL_QDRANT: Final[str] = "qdrant"
STORE_LABEL_NEO4J: Final[str] = "neo4j"
STORE_LABEL_POSTGRES: Final[str] = "postgres"


__all__ = [
    "DRIFT_EDGE",
    "DRIFT_NEO4J_ORPHAN",
    "DRIFT_PG_MISSING_QDRANT",
    "DRIFT_QDRANT_ORPHAN",
    "RECONCILE_ORPHAN_DELETE_LIMIT",
    "RECONCILE_TICK_SECONDS_DEFAULT",
    "REEMIT_TICK_CAP_DEFAULT",
    "STORE_LABEL_NEO4J",
    "STORE_LABEL_POSTGRES",
    "STORE_LABEL_QDRANT",
]
