"""Pure incident-resolution domain logic (issue #153, Wave 2 of epic #99).

Contains the classification, confidence-scoring, and response-building
primitives that power ``resolve_incident``.  The FastAPI orchestration
layer (``mcp_resolve_incident``) lives in ``apps/server`` and calls into
these functions; this module has no dependency on FastAPI or any server
state.

Composition shape (caller responsibility)
-----------------------------------------

1. Validate ``alert_id`` with :func:`validate_alert_id`.
2. Fetch seed entity and ``GraphResultView`` from ``GraphStore``.
3. Call :func:`classify_neighbours` to bucket BFS results.
4. Call :func:`score_incident` for the confidence heuristic.
5. Build the response with :func:`build_resolve_response`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from omniscience_core.storage import EntityNodeView, GraphResultView
from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Module constants — no magic numbers in the algorithm body
# ---------------------------------------------------------------------------

#: Default BFS depth when the caller does not override ``max_depth``.
DEFAULT_MAX_DEPTH: Final[int] = 2

#: Lower bound for ``max_depth``.
MIN_MAX_DEPTH: Final[int] = 1

#: Upper bound for ``max_depth``.
MAX_MAX_DEPTH: Final[int] = 5

#: Soft cap on the number of Slack threads carried in the response.
MAX_SLACK_THREADS_RETURNED: Final[int] = 10

#: Window inside which a PR merge is considered "temporally correlated"
#: with an alert firing — issue #153 §C.
PR_RECENCY_WINDOW_SECONDS: Final[int] = 24 * 60 * 60

# ---------------------------------------------------------------------------
# Confidence-score heuristic (v0.1 placeholder; #155 replaces it)
# ---------------------------------------------------------------------------

CONFIDENCE_PR_TEMPORAL_MATCH: Final[float] = 0.9
CONFIDENCE_PR_NO_TEMPORAL_MATCH: Final[float] = 0.6
CONFIDENCE_RESOURCE_ONLY: Final[float] = 0.4
CONFIDENCE_ALERT_ONLY: Final[float] = 0.1
CONFIDENCE_NONE: Final[float] = 0.0

# ---------------------------------------------------------------------------
# Canonical-name prefix discriminators
# ---------------------------------------------------------------------------

_ALERT_PREFIX: Final[str] = "alert://"
_TRACE_PREFIX: Final[str] = "trace://"
_SLACK_THREAD_PREFIX: Final[str] = "slack://channel/"
_SERVICE_PREFIX: Final[str] = "service://"
_ARN_PREFIX: Final[str] = "arn:"
_GITHUB_PR_RE: Final[re.Pattern[str]] = re.compile(
    r"^https?://github\.com/[^/]+/[^/]+/pull/(\d+)(?:[/?#].*)?$"
)
_POD_RE: Final[re.Pattern[str]] = re.compile(r"^pod/[A-Za-z0-9._-]+$")
_K8S_NAMESPACED_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

# ---------------------------------------------------------------------------
# Error codes (mirrored on the MCP / REST surfaces)
# ---------------------------------------------------------------------------

INVALID_ALERT_ID_CODE: Final[str] = "invalid_alert_id"
INVALID_ALERT_ID_MESSAGE: Final[str] = (
    "alert_id must be of the form 'alert://{provider}/{provider_alert_id}'"
)
ALERT_NOT_FOUND_CODE: Final[str] = "alert_not_found"


# ---------------------------------------------------------------------------
# Pydantic response schemas
# ---------------------------------------------------------------------------


class AlertSummary(BaseModel):
    """Minimal alert payload returned in the recommendation bundle."""

    name: str = Field(description="Canonical alert name (alert://provider/id).")
    kind: str | None = None
    source: str | None = None
    chunk_text: str | None = None
    valid_from: datetime | None = None


class ResourceSummary(BaseModel):
    """Minimal resource payload (pod / ARN / service / k8s name)."""

    name: str = Field(description="Canonical resource identifier.")
    kind: str | None = None
    source: str | None = None
    edge_type: str | None = Field(
        default=None,
        description="The edge_type by which the alert reached this resource.",
    )


class PrSummary(BaseModel):
    """Minimal PR payload returned as the responsible PR (when any)."""

    name: str = Field(description="Canonical PR URL.")
    kind: str | None = None
    source: str | None = None
    edge_type: str | None = None
    merged_at: datetime | None = Field(
        default=None,
        description=(
            "Best-effort merge timestamp; v0.1 reads it from the entity's "
            "bitemporal ``valid_from`` when populated by the connector "
            "(GitHub PR #150).  Calibrated extraction lands in #155."
        ),
    )


class SlackThreadSummary(BaseModel):
    """Minimal Slack thread payload — name plus optional chunk text."""

    name: str = Field(description="Canonical Slack thread URI.")
    kind: str | None = None
    source: str | None = None
    chunk_text: str | None = None
    edge_type: str | None = None


class ResolveIncidentResponse(BaseModel):
    """The recommendation bundle returned by ``resolve_incident``.

    ``confidence_score`` is a v0.1 placeholder per the architect memo on
    epic #99; the calibrated model lands in #155.  Callers should treat
    the score as a stable schema slot, not a calibrated probability.

    ``similar_past`` (issue #233, additive) carries the ranked list of
    past incidents that the same-incident clustering primitive matched
    for this seed.  Each entry follows the
    :class:`omniscience_retrieval.incidents.similar.SimilarIncident`
    schema (serialised as a JSON dict for transport-agnostic delivery).
    Empty list when the clustering call returned no matches or could
    not run (best-effort, never breaks the primary bundle).
    """

    alert: AlertSummary
    target_resource: ResourceSummary | None = None
    responsible_pr: PrSummary | None = None
    slack_threads: list[SlackThreadSummary] = Field(default_factory=list)
    confidence_score: float = Field(ge=0.0, le=1.0)
    effective_as_of: datetime
    meta: dict[str, Any] | None = None
    similar_past: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Ranked past incidents similar to this one (issue #233). "
            "Shape matches SimilarIncident; empty when clustering yields "
            "no matches or is unavailable."
        ),
    )


# ---------------------------------------------------------------------------
# Internal classification dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClassifiedNeighbours:
    """Bucketed view of BFS traversal neighbours."""

    target_resource: EntityNodeView | None
    responsible_pr: EntityNodeView | None
    slack_threads: tuple[EntityNodeView, ...]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def validate_alert_id(alert_id: str) -> None:
    """Reject alert ids that are not of the form ``alert://provider/id``."""
    if not isinstance(alert_id, str):  # pragma: no cover - typing barrier
        raise ValueError(f"{INVALID_ALERT_ID_CODE}:{INVALID_ALERT_ID_MESSAGE}")
    if not alert_id.startswith(_ALERT_PREFIX):
        raise ValueError(f"{INVALID_ALERT_ID_CODE}:{INVALID_ALERT_ID_MESSAGE}")
    suffix = alert_id[len(_ALERT_PREFIX) :]
    if "/" not in suffix:
        raise ValueError(f"{INVALID_ALERT_ID_CODE}:{INVALID_ALERT_ID_MESSAGE}")
    provider, _, provider_alert_id = suffix.partition("/")
    if not provider.strip() or not provider_alert_id.strip():
        raise ValueError(f"{INVALID_ALERT_ID_CODE}:{INVALID_ALERT_ID_MESSAGE}")


# ---------------------------------------------------------------------------
# Classification — pure, easy to unit test
# ---------------------------------------------------------------------------


def classify_neighbours(result: GraphResultView) -> ClassifiedNeighbours:
    """Bucket BFS neighbours by canonical-name prefix.

    Resolves a single ``target_resource`` (closest-depth wins; ties
    broken by traversal order), a single ``responsible_pr`` (most
    recently merged wins), and a list of Slack threads (capped at
    ``MAX_SLACK_THREADS_RETURNED``).
    """
    target_candidates: list[EntityNodeView] = []
    pr_candidates: list[EntityNodeView] = []
    thread_candidates: list[EntityNodeView] = []
    for node in result.related:
        if _is_resource(node.name):
            target_candidates.append(node)
        elif _is_pr(node.name):
            pr_candidates.append(node)
        elif _is_slack_thread(node.name):
            thread_candidates.append(node)
    target_resource = pick_target_resource(target_candidates)
    responsible_pr = pick_responsible_pr(pr_candidates)
    threads = tuple(thread_candidates[:MAX_SLACK_THREADS_RETURNED])
    return ClassifiedNeighbours(
        target_resource=target_resource,
        responsible_pr=responsible_pr,
        slack_threads=threads,
    )


def _is_pr(name: str) -> bool:
    """Return ``True`` iff ``name`` is a GitHub PR canonical URL."""
    return bool(_GITHUB_PR_RE.match(name))


def _is_slack_thread(name: str) -> bool:
    return name.startswith(_SLACK_THREAD_PREFIX)


def _is_resource(name: str) -> bool:
    """Return ``True`` for pod / ARN / service / k8s namespaced names."""
    if name.startswith(_ALERT_PREFIX):
        return False
    if name.startswith(_TRACE_PREFIX):
        return False
    if name.startswith(_SLACK_THREAD_PREFIX):
        return False
    if _GITHUB_PR_RE.match(name):
        return False
    if name.startswith(_ARN_PREFIX):
        return True
    if name.startswith(_SERVICE_PREFIX):
        return True
    if _POD_RE.match(name):
        return True
    return bool(_K8S_NAMESPACED_RE.match(name))


def pick_target_resource(
    candidates: list[EntityNodeView],
) -> EntityNodeView | None:
    """Closest-depth wins; ties broken by traversal order (input order)."""
    if not candidates:
        return None
    return min(candidates, key=lambda n: n.depth)


def pick_responsible_pr(
    candidates: list[EntityNodeView],
) -> EntityNodeView | None:
    """Most recently merged wins; falls back to closest-depth."""
    if not candidates:
        return None
    with_merge = [c for c in candidates if c.valid_from is not None]
    if with_merge:
        return max(with_merge, key=lambda n: n.valid_from or datetime.min)
    return min(candidates, key=lambda n: n.depth)


# ---------------------------------------------------------------------------
# Confidence-score heuristic
# ---------------------------------------------------------------------------


def compute_confidence(
    *,
    alert: EntityNodeView,
    classified: ClassifiedNeighbours,
    half_life_seconds: float = 43200.0,
) -> float:
    """Compute continuous probabilistic confidence using exponential temporal decay.

    Incorporates a configurable half-life to decay the PR correlation probability.
    """
    if classified.responsible_pr is not None:
        pr = classified.responsible_pr
        pr_merged_at = pr.valid_from
        alert_fired_at = alert.valid_from

        if pr_merged_at is None or alert_fired_at is None:
            # PR exists but no temporal data: assign neutral 0.5 recency probability
            return CONFIDENCE_PR_NO_TEMPORAL_MATCH + (
                CONFIDENCE_PR_TEMPORAL_MATCH - CONFIDENCE_PR_NO_TEMPORAL_MATCH
            ) * 0.5

        delta = (alert_fired_at - pr_merged_at).total_seconds()
        if delta < 0:
            # PR merged after alert fired: causality violation, return base PR score
            return CONFIDENCE_PR_NO_TEMPORAL_MATCH

        # Exponential decay: P = exp(-lambda * delta) where lambda = ln(2) / half_life
        decay_constant = math.log(2.0) / half_life_seconds
        p_recency = math.exp(-decay_constant * delta)

        return CONFIDENCE_PR_NO_TEMPORAL_MATCH + (
            CONFIDENCE_PR_TEMPORAL_MATCH - CONFIDENCE_PR_NO_TEMPORAL_MATCH
        ) * p_recency

    if classified.target_resource is not None:
        return CONFIDENCE_RESOURCE_ONLY
    return CONFIDENCE_ALERT_ONLY


def _is_temporally_correlated(
    *,
    alert: EntityNodeView,
    pr: EntityNodeView,
) -> bool:
    """Return ``True`` iff PR merge time precedes alert by < window."""
    pr_merged_at = pr.valid_from
    alert_fired_at = alert.valid_from
    if pr_merged_at is None or alert_fired_at is None:
        return False
    if pr_merged_at > alert_fired_at:
        return False
    delta = (alert_fired_at - pr_merged_at).total_seconds()
    return 0 <= delta <= PR_RECENCY_WINDOW_SECONDS


# ---------------------------------------------------------------------------
# Calibrated scoring path
# ---------------------------------------------------------------------------


def score_incident(
    *,
    alert: EntityNodeView,
    classified: ClassifiedNeighbours,
    max_depth: int,
    config: Any,  # IncidentScoringConfig | None — avoid circular import at type level
) -> float:
    """Pick the calibrated path or fall back to the v0.1 ladder.

    ``config`` is an :class:`~omniscience_retrieval.incidents.scoring.IncidentScoringConfig`
    or ``None``.  When ``None`` (or weights absent) the v0.1 ladder is used.
    """
    # Import here to avoid module-level circular import within the incidents package.
    from omniscience_retrieval.incidents.scoring import apply_weights, compute_components

    half_life = config.temporal_decay_half_life_seconds if config is not None else 43200.0
    if config is None or config.weights is None:
        return compute_confidence(alert=alert, classified=classified, half_life_seconds=half_life)
    components = compute_components(
        alert_valid_from=alert.valid_from,
        pr_valid_from=(
            classified.responsible_pr.valid_from if classified.responsible_pr is not None else None
        ),
        has_pr=classified.responsible_pr is not None,
        has_resource=classified.target_resource is not None,
        resource_depth=(
            classified.target_resource.depth if classified.target_resource is not None else 0
        ),
        thread_count=len(classified.slack_threads),
        max_depth=max_depth,
        temporal_decay_half_life_seconds=half_life,
    )
    return apply_weights(components, config.weights)


def build_meta(
    *,
    confidence: float,
    config: Any,  # IncidentScoringConfig | None
) -> dict[str, Any] | None:
    """Populate ``meta.below_trust_threshold`` when the score is below threshold."""
    from omniscience_retrieval.incidents.scoring import DEFAULT_CONFIDENCE_THRESHOLD

    threshold = config.confidence_threshold if config is not None else DEFAULT_CONFIDENCE_THRESHOLD
    if confidence < threshold:
        return {"below_trust_threshold": True, "confidence_threshold": threshold}
    return None


# ---------------------------------------------------------------------------
# Response assembly
# ---------------------------------------------------------------------------


def build_resolve_response(
    *,
    alert: EntityNodeView,
    classified: ClassifiedNeighbours,
    confidence: float,
    as_of: datetime | None,
    effective_as_of: datetime,
    meta: dict[str, Any] | None = None,
    similar_past: list[dict[str, Any]] | None = None,
) -> ResolveIncidentResponse:
    """Produce the bounded :class:`ResolveIncidentResponse` payload."""
    return ResolveIncidentResponse(
        alert=AlertSummary(
            name=alert.name,
            kind=alert.kind,
            source=alert.source,
            chunk_text=alert.chunk_text,
            valid_from=alert.valid_from,
        ),
        target_resource=_summarise_resource(classified.target_resource),
        responsible_pr=_summarise_pr(classified.responsible_pr),
        slack_threads=[_summarise_thread(t) for t in classified.slack_threads],
        confidence_score=confidence,
        effective_as_of=effective_as_of,
        meta=meta,
        similar_past=list(similar_past or []),
    )


def _summarise_resource(node: EntityNodeView | None) -> ResourceSummary | None:
    if node is None:
        return None
    return ResourceSummary(
        name=node.name,
        kind=node.kind,
        source=node.source,
        edge_type=node.edge_type,
    )


def _summarise_pr(node: EntityNodeView | None) -> PrSummary | None:
    if node is None:
        return None
    return PrSummary(
        name=node.name,
        kind=node.kind,
        source=node.source,
        edge_type=node.edge_type,
        merged_at=node.valid_from,
    )


def _summarise_thread(node: EntityNodeView) -> SlackThreadSummary:
    return SlackThreadSummary(
        name=node.name,
        kind=node.kind,
        source=node.source,
        chunk_text=node.chunk_text,
        edge_type=node.edge_type,
    )


# ---------------------------------------------------------------------------
# Pydantic request validator (used by REST surface)
# ---------------------------------------------------------------------------


class AlertIdRequest(BaseModel):
    """Lightweight wrapper used by the REST surface to validate ``alert_id``."""

    alert_id: str

    @field_validator("alert_id")
    @classmethod
    def _validate(cls, value: str) -> str:
        validate_alert_id(value)
        return value


__all__ = [
    "ALERT_NOT_FOUND_CODE",
    "CONFIDENCE_ALERT_ONLY",
    "CONFIDENCE_NONE",
    "CONFIDENCE_PR_NO_TEMPORAL_MATCH",
    "CONFIDENCE_PR_TEMPORAL_MATCH",
    "CONFIDENCE_RESOURCE_ONLY",
    "DEFAULT_MAX_DEPTH",
    "INVALID_ALERT_ID_CODE",
    "INVALID_ALERT_ID_MESSAGE",
    "MAX_MAX_DEPTH",
    "MAX_SLACK_THREADS_RETURNED",
    "MIN_MAX_DEPTH",
    "PR_RECENCY_WINDOW_SECONDS",
    "AlertIdRequest",
    "AlertSummary",
    "ClassifiedNeighbours",
    "PrSummary",
    "ResolveIncidentResponse",
    "ResourceSummary",
    "SlackThreadSummary",
    "build_meta",
    "build_resolve_response",
    "classify_neighbours",
    "compute_confidence",
    "pick_responsible_pr",
    "pick_target_resource",
    "score_incident",
    "validate_alert_id",
]
