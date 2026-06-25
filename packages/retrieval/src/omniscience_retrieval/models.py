"""Request / response models for the retrieval service."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class SearchRequest(BaseModel):
    """Parameters for a hybrid search query.

    The optional ``as_of`` field surfaces ADR-0008's bitemporal predicate
    through the public retrieval contract (issue #133).  When supplied,
    the GraphRAG composer forwards it to ``GraphStore.traverse`` /
    ``GraphStore.find_related`` so the anchor-stage subgraph is the one
    that was valid at ``as_of``; the vector stage is then scoped to
    those source IDs.  ``None`` (default) preserves the current-state
    contract exactly — see ADR-0008 §5 hot-path guarantee.

    ``as_of`` MUST be timezone-aware.  Naive datetimes are rejected at
    validation time with a ``ValueError`` carrying the structured
    ``invalid_timezone`` code (issue #133 §B).
    """

    query: str
    top_k: int = Field(default=10, ge=1, le=500)
    sources: list[str] | None = None
    types: list[str] | None = None
    max_age_seconds: int | None = Field(default=None, ge=1)
    filters: dict[str, Any] | None = None
    include_tombstoned: bool = False
    retrieval_strategy: Literal["hybrid", "keyword", "structural", "auto"] = "hybrid"
    as_of: datetime | None = Field(
        default=None,
        description=(
            "Optional ISO-8601 timezone-aware UTC datetime (e.g. "
            "'2026-04-12T19:25:00Z'). When set, the search is "
            "evaluated against the graph state that was valid at "
            "this time per ADR-0008 §5. Naive datetimes are rejected."
        ),
    )
    cursor: str | None = Field(
        default=None, description="Opaque pagination cursor to resume a prior search."
    )

    @field_validator("as_of", mode="after")
    @classmethod
    def _validate_as_of_utc(cls, value: datetime | None) -> datetime | None:
        """Reject naive datetimes; normalise non-UTC offsets to UTC.

        Mirrors :func:`omniscience_server.as_of.enforce_utc` — kept
        local to keep the retrieval package free of a server-side
        import.  The error code ``invalid_timezone`` is identical so
        the MCP/REST layer surfaces the same envelope.
        """
        if value is None:
            return None
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("invalid_timezone")
        return value.astimezone(UTC)


class Citation(BaseModel):
    """Provenance information for a retrieved chunk."""

    uri: str
    title: str | None
    indexed_at: datetime
    doc_version: int


class ChunkLineage(BaseModel):
    """Ingestion lineage metadata for a retrieved chunk."""

    ingestion_run_id: uuid.UUID | None
    embedding_model: str
    embedding_provider: str
    parser_version: str
    chunker_strategy: str


class SourceInfo(BaseModel):
    """Minimal source descriptor embedded in each hit."""

    id: uuid.UUID
    name: str
    type: str


class SearchHit(BaseModel):
    """A single result returned by the retrieval service.

    The ``source_instance`` field is ``None`` for results from the local
    instance and set to the :attr:`~FederatedInstance.name` of the remote
    peer for hits that arrived via federation.
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    score: float
    text: str
    source: SourceInfo
    citation: Citation
    lineage: ChunkLineage
    metadata: dict[str, Any]
    source_instance: str | None = Field(
        default=None,
        description=(
            "Name of the Omniscience instance that produced this hit.  "
            "None for the local instance; set to the peer name for federated results."
        ),
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Probabilistic confidence score for this search hit.",
    )
    score_type: Literal["calibrated", "heuristic", "raw"] | None = Field(
        default=None,
        description="Confidence score estimation type: calibrated, heuristic, or raw.",
    )
    impact: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Probabilistic impact score for this search hit.",
    )
    staleness: float | None = Field(
        default=None, description="Lag of this evidence from SoT or current time in seconds."
    )
    applied_version: int | None = Field(
        default=None, description="Version of this evidence projection."
    )


class QueryStats(BaseModel):
    """Diagnostic counters for a completed search query."""

    total_matches_before_filters: int
    vector_matches: int
    text_matches: int
    duration_ms: float


class SearchResult(BaseModel):
    """Top-level response returned by RetrievalService.search().

    ``effective_as_of`` echoes the timestamp the response reflects per
    issue #133.  For ``as_of=None`` requests this is the response
    generation time; for an explicit ``as_of`` it is the value the
    server processed (so callers can pin it for retries and detect
    pagination drift).

    ``meta`` carries optional response-side hints — currently only
    ``degraded_response="as_of_before_recorded_history"`` when the
    caller's ``as_of`` precedes any recorded history for the workspace
    (issue #133 §B).  ``None`` means "no hints to convey" and serialises
    as a plain absent field.

    ``degraded_subsystems`` lists store names (e.g. ``["neo4j", "qdrant"]``)
    that lagged behind the Postgres SoT watermark when this response was
    produced.  Non-empty means the result may reflect stale projections
    for those stores.  ``staleness_seconds`` is the age of the lagging
    projection relative to the SoT watermark (wall-clock seconds); ``None``
    when all stores are converged or the lag could not be measured.
    Callers MUST treat a non-empty ``degraded_subsystems`` as an indication
    that the result may be stale and should not be cached or used for
    time-sensitive decisions without a retry.

    ``pinned_watermark`` is the global minimum version across all sources
    and all three stores (Postgres, Neo4j, Qdrant) at the time this
    response was composed — for observability only.  Per-hit filtering is
    applied PER-SOURCE internally (v10-AP1): each hit is filtered against
    the watermark of its own source, so a cold/lagging source B does NOT
    decimate hits from healthy source A.  The scalar here is the worst-case
    floor.  ``None`` means no version information was available — no
    watermark filter was applied (AP3 / v10-AP1 consistent-stale PIN).
    """

    hits: list[SearchHit]
    query_stats: QueryStats
    effective_as_of: datetime | None = Field(
        default=None,
        description=(
            "Server-resolved bitemporal anchor for this response. "
            "Echoes back the request's ``as_of`` when explicit; "
            "otherwise the response generation time."
        ),
    )
    meta: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional response-side hints. May include "
            "``degraded_response`` when the requested ``as_of`` "
            "predates any recorded history for the workspace."
        ),
    )
    min_applied_version: int | None = Field(
        default=None,
        description="The minimum applied version among all evidence in this response.",
    )
    degraded_subsystems: list[str] = Field(
        default_factory=list,
        description=(
            "Store names (e.g. 'neo4j', 'qdrant') that were behind the Postgres SoT "
            "watermark when this response was produced.  Non-empty means the result "
            "may reflect stale projections."
        ),
    )
    staleness_seconds: float | None = Field(
        default=None,
        description=(
            "Age in wall-clock seconds of the lagging projection relative to the "
            "Postgres SoT watermark.  Set when degraded_subsystems is non-empty and "
            "the lag could be measured; None otherwise."
        ),
    )
    pinned_watermark: int | None = Field(
        default=None,
        description=(
            "Global minimum version across all sources/stores at composition time "
            "(observability scalar).  Per-hit filtering uses per-source watermarks "
            "internally (v10-AP1) — a cold source does not blackout healthy sources. "
            "None when no version information was available — no watermark filter applied. "
            "AP3 / v10-AP1 consistent-stale PIN."
        ),
    )
    snapshot_id: str | None = Field(
        default=None, description="ID of the graph snapshot used for this response."
    )
    next_cursor: str | None = Field(
        default=None, description="Opaque pagination cursor to fetch the next page of results."
    )
