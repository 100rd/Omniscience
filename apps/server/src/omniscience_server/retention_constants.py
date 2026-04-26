"""Named constants for the retention worker (issue #135, ADR-0009).

Every value here is traced back to ``docs/decisions/0009-retention-tiering-policy.md``.
No magic numbers are allowed in ``retention_worker.py`` or the REST
endpoint; anything tunable that the ADR names explicitly lives here
with a docstring that cites the ADR section.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Tier boundaries (ADR-0009 §1, §10)
# ---------------------------------------------------------------------------

#: Hot tier retention boundary in days.  ADR-0009 §1: "Hot (0-90 days,
#: default)" + §10 ``omniscience.retention.hotDays: 90``.  Every tier
#: boundary is global-only in v1; per-tenant overrides are out of scope
#: per ADR-0009 §10.
RETENTION_HOT_DAYS_DEFAULT: Final[int] = 90

#: Warm tier retention boundary in days (cumulative — not warm window
#: length).  ADR-0009 §1 / §10 ``omniscience.retention.warmDays: 365``.
RETENTION_WARM_DAYS_DEFAULT: Final[int] = 365

#: Archive retention horizon in years.  ADR-0009 §1: "Archive retention:
#: 7 years by default, configurable per-deployment."
RETENTION_ARCHIVE_YEARS_DEFAULT: Final[int] = 7

# ---------------------------------------------------------------------------
# Worker shape (ADR-0009 §3)
# ---------------------------------------------------------------------------

#: Worker tick interval in seconds.  ADR-0009 §3: "default: every 6
#: hours" — 6 * 3600.  Lower values increase Neo4j write pressure
#: without improving SLO compliance materially under the 24h lag SLO.
RETENTION_TICK_SECONDS_DEFAULT: Final[int] = 6 * 3600

#: Maximum rows touched per Neo4j transaction during eviction.
#: ADR-0009 §3 read-then-mark-then-move: "select eligible record ids in
#: batches of 1000 per workspace".  We pick 500 as a more conservative
#: default to keep individual transactions under Neo4j's 30s tx timeout
#: on the v0.5 envelope; raise via Helm when the envelope grows.
RETENTION_BATCH_SIZE: Final[int] = 500

#: Number of distinct snapshot dates archived in one run.  Keeps the
#: archive phase bounded — each date becomes one parquet object plus
#: one Qdrant filter delete.  An over-aggressive value would block
#: ingestion behind a long write transaction; 32 days per run drains
#: the typical backlog within a week even if the worker runs every 6h.
RETENTION_ARCHIVE_DATES_PER_RUN: Final[int] = 32

# ---------------------------------------------------------------------------
# Tier / store labels (ADR-0009 §8 — Prometheus label values)
# ---------------------------------------------------------------------------

TIER_LABEL_HOT: Final[str] = "hot"
TIER_LABEL_WARM: Final[str] = "warm"
TIER_LABEL_ARCHIVE: Final[str] = "archive"

STORE_LABEL_NEO4J: Final[str] = "neo4j"
STORE_LABEL_QDRANT: Final[str] = "qdrant"

TRANSITION_HOT_TO_WARM: Final[str] = "hot_to_warm"
TRANSITION_WARM_TO_ARCHIVE: Final[str] = "warm_to_archive"

PHASE_READ: Final[str] = "read"
PHASE_MARK: Final[str] = "mark"
PHASE_MOVE: Final[str] = "move"

# ---------------------------------------------------------------------------
# Archive layout (ADR-0009 §7)
# ---------------------------------------------------------------------------

#: Object key template: ``{workspace_id}/{year}/{month}/{day}/snapshot.parquet``.
#: ADR-0009 §7: "workspace_id at top-level prefix gives operations a
#: single-bucket-prefix delete capability for tenant offboarding".
ARCHIVE_KEY_TEMPLATE: Final[str] = (
    "{workspace_id}/{year:04d}/{month:02d}/{day:02d}/snapshot.parquet"
)

#: KMS server-side encryption value for ADR-0009 §1 "KMS-managed keys
#: mandatory in non-dev environments".
S3_SSE_KMS: Final[str] = "aws:kms"

#: Fallback encryption when no KMS key is configured.  Acceptable in
#: dev only per ADR-0009 §1: "dev profile allows SSE-S3".
S3_SSE_AES256: Final[str] = "AES256"

# ---------------------------------------------------------------------------
# Dry-run reporting
# ---------------------------------------------------------------------------

#: Maximum sampled records emitted in the dry-run report per tier.
#: ADR-0009 §3: dry-run "produces a structured 'would-evict' report".
RETENTION_DRY_RUN_SAMPLE_DEFAULT: Final[int] = 20


__all__ = [
    "ARCHIVE_KEY_TEMPLATE",
    "PHASE_MARK",
    "PHASE_MOVE",
    "PHASE_READ",
    "RETENTION_ARCHIVE_DATES_PER_RUN",
    "RETENTION_ARCHIVE_YEARS_DEFAULT",
    "RETENTION_BATCH_SIZE",
    "RETENTION_DRY_RUN_SAMPLE_DEFAULT",
    "RETENTION_HOT_DAYS_DEFAULT",
    "RETENTION_TICK_SECONDS_DEFAULT",
    "RETENTION_WARM_DAYS_DEFAULT",
    "S3_SSE_AES256",
    "S3_SSE_KMS",
    "STORE_LABEL_NEO4J",
    "STORE_LABEL_QDRANT",
    "TIER_LABEL_ARCHIVE",
    "TIER_LABEL_HOT",
    "TIER_LABEL_WARM",
    "TRANSITION_HOT_TO_WARM",
    "TRANSITION_WARM_TO_ARCHIVE",
]
