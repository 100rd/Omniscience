"""Same-incident clustering domain logic (issue #233, Wave 2 of epic #230).

Pure domain layer for "we saw this 3 days ago": composes the
:mod:`omniscience_index.clustering` primitives with bitemporal
``GraphStore`` traversal to surface ranked past similar incidents for a
given seed alert.

This module contains no FastAPI dependency.  The async
``mcp_find_similar_incidents`` orchestrator lives in
``apps/server/src/omniscience_server/similar_incidents.py`` and calls
:func:`discover_candidates` and :func:`build_features` from here.

ACL invariant (non-negotiable)
------------------------------

- ``workspace_id`` is taken from the caller's bearer token; every
  ``GraphStore`` call carries it.  Past-incident discovery never
  widens across workspaces — the resource pivot is workspace-scoped
  by construction (issue #117 / ADR-0005).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import structlog
from omniscience_core.storage import EntityNodeView, GraphStore
from omniscience_embeddings.base import EmbeddingProvider
from omniscience_index.clustering import (
    DEFAULT_LIMIT,
    DEFAULT_SINCE_DAYS,
    IncidentFeatures,
    SimilarIncidentCandidate,
    compute_signature,
    extract_alert_type,
    rank_candidates,
)
from pydantic import BaseModel, Field, field_validator

from omniscience_retrieval.incidents.resolution import (
    _ALERT_PREFIX,
    ALERT_NOT_FOUND_CODE,
    validate_alert_id,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Public bounds
# ---------------------------------------------------------------------------

MIN_LIMIT: Final[int] = 1
MAX_LIMIT: Final[int] = 50
MIN_SINCE_DAYS: Final[int] = 1
MAX_SINCE_DAYS: Final[int] = 365 * 3

#: Traversal depth from the seed alert.  Depth 2 covers
#: alert -> resource -> alert (other alerts on the same resource).
_DISCOVERY_MAX_DEPTH: Final[int] = 2

# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class SimilarIncident(BaseModel):
    """A single ranked past incident in the clustering response."""

    incident_id: str = Field(description="Canonical alert URI of the past incident.")
    score: float = Field(ge=0.0, le=1.0)
    signature: str = Field(description="Stable hash of (alert_type, service, symptom).")
    alert_type: str = Field(description="Connector-provided alert type.")
    service: str | None = Field(default=None)
    summary: str | None = Field(default=None)
    fired_at: datetime | None = Field(default=None)
    signature_match: bool
    alert_type_match: float = Field(ge=0.0, le=1.0)
    service_match: float = Field(ge=0.0, le=1.0)
    symptom_cosine: float = Field(ge=0.0, le=1.0)
    recency_multiplier: float = Field(ge=0.0, le=1.0)


class FindSimilarIncidentsResponse(BaseModel):
    """Response envelope for ``find_similar_incidents`` (MCP + REST)."""

    incident_id: str
    signature: str = Field(description="Signature of the seed incident.")
    matches: list[SimilarIncident] = Field(default_factory=list)
    effective_as_of: datetime
    meta: dict[str, Any] | None = None


class SimilarIncidentRequest(BaseModel):
    """Request model used by the REST surface for parameter validation."""

    incident_id: str
    limit: int = DEFAULT_LIMIT
    since_days: int = DEFAULT_SINCE_DAYS

    @field_validator("incident_id")
    @classmethod
    def _validate_incident_id(cls, value: str) -> str:
        validate_alert_id(value)
        return value

    @field_validator("limit")
    @classmethod
    def _validate_limit(cls, value: int) -> int:
        if value < MIN_LIMIT or value > MAX_LIMIT:
            raise ValueError(f"limit must be between {MIN_LIMIT} and {MAX_LIMIT}, got {value}")
        return value

    @field_validator("since_days")
    @classmethod
    def _validate_since_days(cls, value: int) -> int:
        if value < MIN_SINCE_DAYS or value > MAX_SINCE_DAYS:
            raise ValueError(
                f"since_days must be between {MIN_SINCE_DAYS} and {MAX_SINCE_DAYS}, got {value}"
            )
        return value


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeededContext:
    """Bundle of seed + discovered candidates ready for scoring."""

    seed: EntityNodeView
    seed_service: str | None
    candidates: list[EntityNodeView]


def _is_alert_name(name: str) -> bool:
    """True iff ``name`` is an ``alert://...`` canonical URI."""
    if not name.startswith(_ALERT_PREFIX):
        return False
    suffix = name[len(_ALERT_PREFIX):]
    return "/" in suffix


def _service_from_neighbour(node: EntityNodeView) -> str | None:
    """Return the canonical service name iff ``node`` is a service-like resource."""
    if node.name.startswith("service://"):
        return node.name
    return None


def _resource_from_neighbour(node: EntityNodeView) -> bool:
    """Heuristic: a depth-1 neighbour reachable via FIRES_AGAINST is a target resource."""
    if node.depth != 1:
        return False
    return not _is_alert_name(node.name)


async def _embed_text(
    *,
    embedder: EmbeddingProvider | None,
    text: str | None,
) -> tuple[float, ...] | None:
    """Embed a single string via the configured provider, or return None."""
    if embedder is None or text is None or not text.strip():
        return None
    try:
        vectors = await embedder.embed([text])
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("similar_incidents_embed_failed", error=str(exc))
        return None
    if not vectors:
        return None
    return tuple(float(v) for v in vectors[0])


def _features_from_node(
    *,
    node: EntityNodeView,
    service: str | None,
    embedding: tuple[float, ...] | None,
) -> IncidentFeatures:
    """Lift an :class:`EntityNodeView` into the clustering feature shape."""
    return IncidentFeatures(
        incident_id=node.name,
        alert_type=extract_alert_type(canonical_name=node.name, kind=node.kind),
        service=service,
        symptom_text=node.chunk_text,
        symptom_embedding=embedding,
        fired_at=node.valid_from,
    )


# ---------------------------------------------------------------------------
# Public domain functions
# ---------------------------------------------------------------------------


async def discover_candidates(
    *,
    graph_store: GraphStore,
    seed: EntityNodeView,
    workspace_id: uuid.UUID,
    as_of: datetime | None,
    since_days: int,
    now: datetime,
) -> SeededContext:
    """Walk the seed -> resource -> peer alerts pivot and return candidates.

    Pure read-only.  ACL-safe: every ``find_related`` call carries
    ``workspace_id`` so cross-workspace nodes never appear in the
    candidate set.  Candidates older than ``since_days`` are filtered
    out here to bound the scoring loop.
    """
    seed_neighbours = await graph_store.find_related(
        entity_name=seed.name,
        workspace_id=workspace_id,
        as_of=as_of,
        max_depth=_DISCOVERY_MAX_DEPTH,
    )

    seed_service: str | None = None
    resource_pivots: list[EntityNodeView] = []
    for node in seed_neighbours.related:
        svc = _service_from_neighbour(node)
        if svc is not None and seed_service is None:
            seed_service = svc
        if _resource_from_neighbour(node):
            resource_pivots.append(node)

    seen: dict[str, EntityNodeView] = {}
    for node in seed_neighbours.related:
        if not _is_alert_name(node.name) or node.name == seed.name:
            continue
        seen.setdefault(node.name, node)

    for pivot in resource_pivots:
        try:
            pivot_result = await graph_store.find_related(
                entity_name=pivot.name,
                workspace_id=workspace_id,
                as_of=as_of,
                max_depth=1,
            )
        except ValueError:
            continue
        for node in pivot_result.related:
            if not _is_alert_name(node.name) or node.name == seed.name:
                continue
            seen.setdefault(node.name, node)

    cutoff = now - timedelta(days=since_days)
    candidates: list[EntityNodeView] = []
    for node in seen.values():
        if node.valid_from is not None and node.valid_from < cutoff:
            continue
        candidates.append(node)
    return SeededContext(
        seed=seed,
        seed_service=seed_service,
        candidates=candidates,
    )


async def build_features(
    *,
    embedder: EmbeddingProvider | None,
    seed: EntityNodeView,
    seed_service: str | None,
    candidates: list[EntityNodeView],
) -> tuple[IncidentFeatures, list[IncidentFeatures]]:
    """Embed the seed + candidate chunk texts and lift them into features."""
    seed_embedding = await _embed_text(embedder=embedder, text=seed.chunk_text)
    seed_features = _features_from_node(
        node=seed,
        service=seed_service,
        embedding=seed_embedding,
    )
    candidate_features: list[IncidentFeatures] = []
    for node in candidates:
        cand_embedding = await _embed_text(embedder=embedder, text=node.chunk_text)
        cand_service = seed_service
        candidate_features.append(
            _features_from_node(
                node=node,
                service=cand_service,
                embedding=cand_embedding,
            )
        )
    return seed_features, candidate_features


def candidate_to_similar_incident(
    ranked: SimilarIncidentCandidate,
    *,
    source_node: EntityNodeView | None,
) -> SimilarIncident:
    """Serialise a clustering result + its source node into the wire shape."""
    summary = source_node.chunk_text if source_node is not None else ranked.features.symptom_text
    return SimilarIncident(
        incident_id=ranked.features.incident_id,
        score=ranked.breakdown.score,
        signature=ranked.signature,
        alert_type=ranked.features.alert_type,
        service=ranked.features.service,
        summary=summary,
        fired_at=ranked.features.fired_at,
        signature_match=ranked.breakdown.signature_match,
        alert_type_match=ranked.breakdown.alert_type_match,
        service_match=ranked.breakdown.service_match,
        symptom_cosine=ranked.breakdown.symptom_cosine,
        recency_multiplier=ranked.breakdown.recency_multiplier,
    )


def rank_similar_incidents(
    *,
    seed_features: IncidentFeatures,
    candidate_features: list[IncidentFeatures],
    candidates: list[EntityNodeView],
    now: datetime,
    since_days: int,
    limit: int,
) -> list[SimilarIncident]:
    """Rank candidates and convert to wire-format :class:`SimilarIncident` objects."""
    ranked = rank_candidates(
        seed=seed_features,
        candidates=candidate_features,
        now=now,
        since_days=since_days,
        limit=limit,
    )
    node_by_name = {n.name: n for n in candidates}
    return [
        candidate_to_similar_incident(
            cand, source_node=node_by_name.get(cand.features.incident_id)
        )
        for cand in ranked
    ]


def build_similar_response(
    *,
    incident_id: str,
    seed_features: IncidentFeatures,
    matches: list[SimilarIncident],
    effective_as_of: datetime,
) -> FindSimilarIncidentsResponse:
    """Build the :class:`FindSimilarIncidentsResponse` envelope."""
    return FindSimilarIncidentsResponse(
        incident_id=incident_id,
        signature=compute_signature(
            alert_type=seed_features.alert_type,
            service=seed_features.service,
            symptom_text=seed_features.symptom_text,
        ),
        matches=matches,
        effective_as_of=effective_as_of,
        meta=None,
    )


__all__ = [
    "ALERT_NOT_FOUND_CODE",
    "MAX_LIMIT",
    "MAX_SINCE_DAYS",
    "MIN_LIMIT",
    "MIN_SINCE_DAYS",
    "FindSimilarIncidentsResponse",
    "SeededContext",
    "SimilarIncident",
    "SimilarIncidentRequest",
    "build_features",
    "build_similar_response",
    "candidate_to_similar_incident",
    "discover_candidates",
    "rank_similar_incidents",
]
