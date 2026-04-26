"""Pydantic response models for the stats sub-package (issue #111, #113).

These types are shared between :class:`omniscience_core.stats.StatsService`
/ :class:`omniscience_core.stats.ClientsStatsService` and the REST layer so
request / response payloads stay strictly typed end to end (no
``dict[str, Any]`` leaks).

The field naming mirrors the dashboard column labels in the admin UI (epic
#109) and is part of the public REST contract.  Adding a field is safe;
removing or renaming one is a breaking change.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class StatsOverview(BaseModel):
    """Single-call operator summary for the admin dashboard.

    Used by ``GET /api/v1/stats/overview``.  All counts are scoped to
    the caller's workspace.
    """

    sources: int = Field(..., ge=0, description="Total sources in workspace.")
    active_sources: int = Field(..., ge=0, description="Sources with status=='active'.")
    documents: int = Field(..., ge=0, description="Total document rows.")
    active_documents: int = Field(..., ge=0, description="Documents not tombstoned.")
    tombstoned_documents: int = Field(..., ge=0, description="Documents currently tombstoned.")
    chunks: int = Field(
        ..., ge=0, description="Active (non-tombstoned) chunk points across Qdrant."
    )
    entities: int = Field(..., ge=0, description="Entities in Neo4j.")
    edges_by_type: dict[str, int] = Field(
        default_factory=dict,
        description="Edge type histogram (edge_type -> count).",
    )
    total_indexed_bytes: int = Field(
        ...,
        ge=0,
        description=(
            "Sum of indexed text length across active chunks (bytes). "
            "Acts as a proxy for total indexed corpus size — Document.size_bytes "
            "is not stored post-cutover (#105)."
        ),
    )
    documents_added_24h: int = Field(
        ..., ge=0, description="Documents whose indexed_at is within last 24h."
    )
    documents_updated_24h: int = Field(
        ..., ge=0, description="Documents whose doc_version > 1 indexed in last 24h."
    )
    documents_tombstoned_24h: int = Field(
        ..., ge=0, description="Documents tombstoned within last 24h."
    )


class SourceStatsRow(BaseModel):
    """One row of the per-source stats table.

    Used by ``GET /api/v1/stats/sources``.
    """

    id: uuid.UUID
    name: str
    type: str
    status: str
    documents: int = Field(..., ge=0)
    chunks: int = Field(..., ge=0)
    entities: int = Field(..., ge=0)
    last_sync_at: str | None
    freshness_sla_seconds: int | None
    age_seconds: float
    is_stale: bool


class SourcesStatsResponse(BaseModel):
    """Wire format for ``GET /api/v1/stats/sources``."""

    sources: list[SourceStatsRow]
    total: int = Field(..., ge=0)


class KindHistogramEntry(BaseModel):
    """One bar of the entity-kind histogram."""

    kind: str
    count: int = Field(..., ge=0)


class EntitiesByKindResponse(BaseModel):
    """Wire format for ``GET /api/v1/stats/entities-by-kind``."""

    entries: list[KindHistogramEntry]
    total: int = Field(..., ge=0)


class EdgeTypeHistogramEntry(BaseModel):
    """One bar of the edge-type histogram."""

    edge_type: str
    count: int = Field(..., ge=0)


class EdgesByTypeResponse(BaseModel):
    """Wire format for ``GET /api/v1/stats/edges-by-type``."""

    entries: list[EdgeTypeHistogramEntry]
    total: int = Field(..., ge=0)


class TokenClientStats(BaseModel):
    """One row of the connected-clients table — per token (issue #113).

    Used by ``GET /api/v1/stats/clients``.  The ``token_id`` field is the
    canonical UUID of the :class:`ApiToken`.  ``last_seen_at`` is the
    timestamp of the most recent request the in-process telemetry has
    observed; ``None`` means the token has been issued but has not made a
    request since process start.

    The 24h figure is approximated from the in-process rolling-bucket
    state — see :mod:`omniscience_core.telemetry.clients` for the
    rationale (no external Prometheus / TSDB available).
    """

    token_id: uuid.UUID
    name: str
    last_seen_at: datetime | None
    requests_last_15m: int = Field(..., ge=0)
    requests_last_24h: int = Field(..., ge=0)


class ToolUsageEntry(BaseModel):
    """One bar of the top-tools-last-hour histogram (issue #113)."""

    tool_name: str
    invocations_last_hour: int = Field(..., ge=0)


class ClientsStatsResponse(BaseModel):
    """Wire format for ``GET /api/v1/stats/clients`` (issue #113).

    Workspace-scoped: ``tokens`` only contains tokens that belong to the
    caller's workspace.  ``mcp_sessions_active`` and ``last_hour`` are
    *process-global* (we cannot attribute a session to a workspace until
    its first authenticated tool call); the per-tool histogram is also
    process-global because tool names are not workspace-secret.
    """

    mcp_sessions_active: int = Field(..., ge=0)
    mcp_sessions_last_hour: int = Field(..., ge=0)
    tokens: list[TokenClientStats]
    top_tools_last_hour: list[ToolUsageEntry]


__all__ = [
    "ClientsStatsResponse",
    "EdgeTypeHistogramEntry",
    "EdgesByTypeResponse",
    "EntitiesByKindResponse",
    "KindHistogramEntry",
    "SourceStatsRow",
    "SourcesStatsResponse",
    "StatsOverview",
    "TokenClientStats",
    "ToolUsageEntry",
]
