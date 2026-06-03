"""Stats sub-package — workspace-scoped operator stats (issue #111).

Public surface:

* :class:`StatsService` — single entry point that aggregates rollups across
  Postgres (operational metadata), Neo4j (entities + edges), and Qdrant
  (chunk counts) for the admin UI dashboard.
* :class:`StatsOverview`, :class:`ActivityResponse`, :class:`SourceStatsRow`,
  :class:`KindHistogramEntry`, :class:`EdgeTypeHistogramEntry` — Pydantic
  response models shared with the REST layer
  (``apps/server/src/omniscience_server/rest/stats.py``).

Design rules
------------

1.  **Single source of truth for stats reads.**  REST endpoints call
    :class:`StatsService` only; they do not import SQLAlchemy models or
    backend adapters directly.  This keeps the admin-UI surface backend-
    neutral and lets us swap stores without touching the REST handlers.

2.  **Workspace is an invariant.**  Every method takes
    ``workspace_id: uuid.UUID`` as a keyword-only required parameter and
    forwards it to every store.  Cross-workspace bleed is a P0 bug
    (issues #117, #119); this layer is the second line of defence after
    the protocol-level invariants on ``GraphStore`` / ``VectorStore``.

3.  **In-process TTL cache.**  Hot rollups (overview, per-source table)
    are cached for :data:`STATS_CACHE_TTL_SECONDS` per ``(workspace_id,
    method)`` key.  No Redis dependency — the cache is per-process and
    fits the < 100ms latency budget on the 100k-chunk fixture.
"""

from __future__ import annotations

from omniscience_core.stats.cache import STATS_CACHE_TTL_SECONDS, TTLCache
from omniscience_core.stats.clients import (
    CLIENTS_STATS_CACHE_TTL_SECONDS,
    ClientsStatsService,
)
from omniscience_core.stats.models import (
    ActivityResponse,
    ClientsStatsResponse,
    EdgesByTypeResponse,
    EdgeTypeHistogramEntry,
    EntitiesByKindResponse,
    KindHistogramEntry,
    SourcesStatsResponse,
    SourceStatsRow,
    StatsOverview,
    TokenClientStats,
    ToolUsageEntry,
)
from omniscience_core.stats.service import StatsService

__all__ = [
    "CLIENTS_STATS_CACHE_TTL_SECONDS",
    "STATS_CACHE_TTL_SECONDS",
    "ActivityResponse",
    "ClientsStatsResponse",
    "ClientsStatsService",
    "EdgeTypeHistogramEntry",
    "EdgesByTypeResponse",
    "EntitiesByKindResponse",
    "KindHistogramEntry",
    "SourceStatsRow",
    "SourcesStatsResponse",
    "StatsOverview",
    "StatsService",
    "TTLCache",
    "TokenClientStats",
    "ToolUsageEntry",
]
