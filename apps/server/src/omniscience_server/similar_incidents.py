"""Same-incident clustering MCP handler (issue #233, Wave 2 of epic #230).

Public surface for "we saw this 3 days ago": composes the pure
:mod:`omniscience_index.clustering` primitives with the bitemporal
``GraphStore`` traversal used by ``resolve_incident`` to surface a
ranked list of past similar incidents for a given seed alert.

Composition shape
-----------------

1. **Validate & resolve seed** — the seed ``incident_id`` is an
   ``alert://{provider}/{id}`` canonical name (the same shape the
   other incident tools accept).  We reuse the validation from
   :mod:`omniscience_server.incidents` so an invalid id raises the
   same ``invalid_alert_id`` error code across all surfaces.
2. **Discover candidates** — we walk the seed's neighbours
   (``find_related`` at depth 2), pick out the ``target service`` /
   ``target resource`` neighbour, then walk *its* neighbours back to
   discover the OTHER alerts that fired against the same resource.
   This is the only path that does NOT require a new ``GraphStore``
   primitive: every past alert that hit the same service is
   reachable via the resource pivot.
3. **Build feature vectors** — for the seed and each candidate we
   pull the alert chunk text and (when the embedding provider is
   wired on ``app.state``) embed it for cosine similarity.
4. **Score & rank** — delegate to
   :func:`omniscience_index.clustering.rank_candidates`.
5. **Filter by window** — drop candidates older than ``since_days``
   *before* scoring, so the multiplier doesn't have to swallow
   long tails.

ACL invariant (non-negotiable)
------------------------------

- ``workspace_id`` is taken from the caller's bearer token; every
  ``GraphStore`` call carries it.  Past-incident discovery never
  widens across workspaces — the resource pivot is workspace-scoped
  by construction (issue #117 / ADR-0005).
- Cross-workspace seeds return ``alert_not_found`` (same envelope
  ``resolve_incident`` uses), preserving the "no existence leak"
  contract.

Bitemporal predicate (ADR-0008 §5)
----------------------------------

``as_of`` is forwarded verbatim to ``GraphStore`` for the seed lookup
and the candidate-discovery traversal.  When set, only entities valid
at T are surfaced — historical clustering against pre-bitemporal
state.  Default (``None``) returns the current still-valid graph.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import structlog
from fastapi import FastAPI
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

from omniscience_server.as_of import (
    enforce_utc,
    record_request_duration,
    resolve_effective_as_of,
)
from omniscience_server.incidents import (
    _ALERT_PREFIX,
    ALERT_NOT_FOUND_CODE,
    _validate_alert_id,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Public bounds — keep the surface deterministic
# ---------------------------------------------------------------------------

#: Minimum value for the ``limit`` MCP parameter.
MIN_LIMIT: Final[int] = 1

#: Upper bound for ``limit`` to keep response payloads predictable.
MAX_LIMIT: Final[int] = 50

#: Minimum lookback window (1 day = 24h).
MIN_SINCE_DAYS: Final[int] = 1

#: Upper bound for lookback — ~3 years.  Beyond this the recency decay
#: is so heavy that older candidates contribute negligibly anyway, and
#: scan cost becomes the dominant concern.
MAX_SINCE_DAYS: Final[int] = 365 * 3

#: Traversal depth from the seed alert.  Depth 2 covers
#: alert -> resource -> alert (other alerts on the same resource).
#: Depth 1 only surfaces direct neighbours of the seed, which is the
#: resource pivot itself, not its other alerts.
_DISCOVERY_MAX_DEPTH: Final[int] = 2

# ---------------------------------------------------------------------------
# Response models — kept here so the wire schema is one import away
# ---------------------------------------------------------------------------


class SimilarIncident(BaseModel):
    """A single ranked past incident in the clustering response.

    Mirrors the fields callers care about: the canonical ``incident_id``
    so they can drill into ``resolve_incident``, the structural
    ``score`` and per-component ``breakdown`` for explainability, and
    the human-readable ``alert_type`` / ``service`` / ``summary`` for
    quick triage.  The signature is exposed so two incidents with
    identical signatures can be deduplicated downstream.
    """

    incident_id: str = Field(description="Canonical alert URI of the past incident.")
    score: float = Field(
        ge=0.0,
        le=1.0,
        description="Similarity score in [0.0, 1.0]; higher = more similar.",
    )
    signature: str = Field(description="Stable hash of (alert_type, service, symptom).")
    alert_type: str = Field(description="Connector-provided alert type (e.g. 'datadog').")
    service: str | None = Field(
        default=None,
        description="Affected service in canonical ``service://`` form (when known).",
    )
    summary: str | None = Field(
        default=None,
        description="Symptom text for the past incident (chunk text excerpt).",
    )
    fired_at: datetime | None = Field(
        default=None,
        description="When the past incident fired (entity ``valid_from``).",
    )
    signature_match: bool = Field(
        description="True iff the past incident shares the seed's signature."
    )
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
        _validate_alert_id(value)
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
class _SeededContext:
    """Bundle of seed + discovered candidates ready for scoring."""

    seed: EntityNodeView
    seed_service: str | None
    candidates: list[EntityNodeView]


def _is_alert_name(name: str) -> bool:
    """True iff ``name`` is an ``alert://...`` canonical URI."""
    if not name.startswith(_ALERT_PREFIX):
        return False
    suffix = name[len(_ALERT_PREFIX) :]
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
    """Embed a single string via the configured provider, or return None.

    Errors are swallowed and logged — the cosine component degrades to
    0.0 (per :func:`omniscience_index.clustering.cosine_similarity`)
    rather than failing the whole request.  Embedding is a soft signal;
    a misbehaving provider must not take down clustering.
    """
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


async def _discover_candidates(
    *,
    graph_store: GraphStore,
    seed: EntityNodeView,
    workspace_id: uuid.UUID,
    as_of: datetime | None,
    since_days: int,
    now: datetime,
) -> _SeededContext:
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

    # Identify the seed's target service / resource.  We prefer a
    # service:// canonical (most stable identity); when absent, any
    # depth-1 non-alert neighbour acts as the pivot.
    seed_service: str | None = None
    resource_pivots: list[EntityNodeView] = []
    for node in seed_neighbours.related:
        svc = _service_from_neighbour(node)
        if svc is not None and seed_service is None:
            seed_service = svc
        if _resource_from_neighbour(node):
            resource_pivots.append(node)

    # Peer-alert candidates surfaced directly by the depth-2 walk from
    # the seed.  Any other alert entity reached in the BFS that is not
    # the seed itself is a candidate.
    seen: dict[str, EntityNodeView] = {}
    for node in seed_neighbours.related:
        if not _is_alert_name(node.name) or node.name == seed.name:
            continue
        seen.setdefault(node.name, node)

    # Pivot back from each resource neighbour to find other alerts that
    # fired against the same resource (depth 1 from the resource).
    for pivot in resource_pivots:
        try:
            pivot_result = await graph_store.find_related(
                entity_name=pivot.name,
                workspace_id=workspace_id,
                as_of=as_of,
                max_depth=1,
            )
        except ValueError:
            # Resource disappeared between traversals — skip it.
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
    return _SeededContext(
        seed=seed,
        seed_service=seed_service,
        candidates=candidates,
    )


async def _build_features(
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
        # Candidates inherit the seed's service when the canonical name
        # itself doesn't carry one — they were reached via the same
        # resource pivot, after all.
        cand_service = seed_service
        candidate_features.append(
            _features_from_node(
                node=node,
                service=cand_service,
                embedding=cand_embedding,
            )
        )
    return seed_features, candidate_features


def _candidate_to_response(
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


# ---------------------------------------------------------------------------
# Public composition entry point
# ---------------------------------------------------------------------------


async def mcp_find_similar_incidents(
    app: FastAPI,
    *,
    incident_id: str,
    workspace_id: uuid.UUID,
    limit: int = DEFAULT_LIMIT,
    since_days: int = DEFAULT_SINCE_DAYS,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Return ranked similar past incidents for the given seed.

    Mirrors the shape of :func:`mcp_resolve_incident` — same ACL
    invariant, same ``as_of`` plumbing, same telemetry histogram
    (``omniscience_request_duration_seconds`` with
    ``tool="find_similar_incidents"``).

    Parameters
    ----------
    app:
        FastAPI app exposing ``app.state.graph_store`` and optionally
        ``app.state.embedding_provider``.
    incident_id:
        Canonical seed alert URI ``alert://{provider}/{provider_alert_id}``.
        Other forms raise ``ValueError("invalid_alert_id:...")``.
    workspace_id:
        REQUIRED.  Forwarded to every ``GraphStore`` call.
    limit:
        Max number of similar incidents to return.  Clamped to
        ``[MIN_LIMIT, MAX_LIMIT]``.
    since_days:
        Lookback window — candidates older than this are dropped.
        Clamped to ``[MIN_SINCE_DAYS, MAX_SINCE_DAYS]``.
    as_of:
        Optional ADR-0008 §5 bitemporal anchor.
    """
    _validate_alert_id(incident_id)
    normalised_as_of = enforce_utc(as_of)
    clamped_limit = max(MIN_LIMIT, min(limit, MAX_LIMIT))
    clamped_since = max(MIN_SINCE_DAYS, min(since_days, MAX_SINCE_DAYS))

    graph_store: GraphStore | None = getattr(app.state, "graph_store", None)
    if graph_store is None:
        raise RuntimeError("graph_store not available on app.state")
    embedder: EmbeddingProvider | None = getattr(app.state, "embedding_provider", None)

    with record_request_duration(
        surface="mcp", tool="find_similar_incidents", as_of=normalised_as_of
    ) as patcher:
        seed = await graph_store.get_entity(
            entity_name=incident_id,
            workspace_id=workspace_id,
            as_of=normalised_as_of,
        )
        if seed is None:
            if normalised_as_of is not None:
                patcher.mark_pre_history()
            raise ValueError(f"{ALERT_NOT_FOUND_CODE}:{incident_id}")

        now = datetime.now(tz=UTC)
        try:
            context = await _discover_candidates(
                graph_store=graph_store,
                seed=seed,
                workspace_id=workspace_id,
                as_of=normalised_as_of,
                since_days=clamped_since,
                now=now,
            )
        except ValueError as exc:
            # Seed disappeared between get_entity and find_related —
            # return the empty-match envelope rather than 404.
            if str(exc).startswith("entity_not_found:"):
                context = _SeededContext(seed=seed, seed_service=None, candidates=[])
            else:
                raise

        seed_features, candidate_features = await _build_features(
            embedder=embedder,
            seed=seed,
            seed_service=context.seed_service,
            candidates=context.candidates,
        )

        ranked = rank_candidates(
            seed=seed_features,
            candidates=candidate_features,
            now=now,
            since_days=clamped_since,
            limit=clamped_limit,
        )

        # Build a quick lookup so the wire payload carries the
        # source-node summary text (the embedded text is already
        # captured in the features, but we want the raw chunk).
        node_by_name = {n.name: n for n in context.candidates}
        matches = [
            _candidate_to_response(cand, source_node=node_by_name.get(cand.features.incident_id))
            for cand in ranked
        ]

        response = FindSimilarIncidentsResponse(
            incident_id=incident_id,
            signature=compute_signature(
                alert_type=seed_features.alert_type,
                service=seed_features.service,
                symptom_text=seed_features.symptom_text,
            ),
            matches=matches,
            effective_as_of=resolve_effective_as_of(normalised_as_of),
            meta=None,
        )
        return response.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Lightweight helper for resolve_incident composition (issue #233 acceptance)
# ---------------------------------------------------------------------------


async def collect_similar_past(
    app: FastAPI,
    *,
    incident_id: str,
    workspace_id: uuid.UUID,
    as_of: datetime | None,
    limit: int = DEFAULT_LIMIT,
    since_days: int = DEFAULT_SINCE_DAYS,
) -> list[dict[str, Any]]:
    """Return the ``matches`` portion of the similar-incidents response.

    Used by :func:`omniscience_server.incidents.mcp_resolve_incident` to
    populate its new ``similar_past`` field.  Failures are swallowed and
    logged — the augmentation is best-effort and must NEVER break the
    primary incident-resolution response (additive-field contract from
    the #233 spec).
    """
    try:
        payload = await mcp_find_similar_incidents(
            app=app,
            incident_id=incident_id,
            workspace_id=workspace_id,
            limit=limit,
            since_days=since_days,
            as_of=as_of,
        )
    except ValueError:
        # invalid_alert_id / alert_not_found here are unreachable in
        # practice (resolve_incident already validated + resolved the
        # seed) but be defensive — never bubble a clustering error
        # back through the primary call path.
        return []
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(
            "resolve_incident_similar_past_failed",
            incident_id=incident_id,
            error=str(exc),
        )
        return []
    matches = payload.get("matches", [])
    if not isinstance(matches, list):
        return []
    return [m for m in matches if isinstance(m, dict)]


__all__ = [
    "MAX_LIMIT",
    "MAX_SINCE_DAYS",
    "MIN_LIMIT",
    "MIN_SINCE_DAYS",
    "FindSimilarIncidentsResponse",
    "SimilarIncident",
    "SimilarIncidentRequest",
    "collect_similar_past",
    "mcp_find_similar_incidents",
]
