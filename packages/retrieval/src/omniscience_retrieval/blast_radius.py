"""Blast-radius domain logic (issue #234, Wave 2 of epic #230 / H5 Track 1).

Pure domain layer for the change-management blast-radius analysis.
Contains validation, scoring, path reconstruction, and response
building.  The FastAPI orchestration (``mcp_blast_radius``) lives in
``apps/server`` and calls these functions.

Action-type semantics (see ``docs/api/blast-radius.md``)
--------------------------------------------------------

* ``restart``    — transient unavailability; follows runtime edges.
* ``delete``     — permanent removal; widest blast (all causal edges).
* ``scale_down`` — degraded capacity; same as ``restart`` plus load edges.
* ``cordon``     — quarantine; follows scheduling edges only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from omniscience_core.storage import EntityNodeView, GraphEdgeView, GraphResultView
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Public action-type contract
# ---------------------------------------------------------------------------

ActionType = Literal["restart", "delete", "scale_down", "cordon"]
ACTION_TYPES: Final[tuple[ActionType, ...]] = ("restart", "delete", "scale_down", "cordon")

# ---------------------------------------------------------------------------
# Depth bounds
# ---------------------------------------------------------------------------

DEFAULT_MAX_DEPTH: Final[int] = 3
MIN_MAX_DEPTH: Final[int] = 1
MAX_MAX_DEPTH: Final[int] = 5

# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------

ENTITY_NOT_FOUND_CODE: Final[str] = "entity_not_found"
INVALID_ACTION_TYPE_CODE: Final[str] = "invalid_action_type"
INVALID_ACTION_TYPE_MESSAGE: Final[str] = "action_type must be one of: " + ", ".join(ACTION_TYPES)
INVALID_ENTITY_ID_CODE: Final[str] = "invalid_entity_id"
INVALID_ENTITY_ID_MESSAGE: Final[str] = "entity_id must be a non-empty string"

# ---------------------------------------------------------------------------
# Action-type -> edge-type allowlist
# ---------------------------------------------------------------------------

_RUNTIME_EDGES: Final[frozenset[str]] = frozenset({"DEPENDS_ON", "ROUTES_TO", "CALLS"})
_LOAD_EDGES: Final[frozenset[str]] = frozenset({"LOAD_BALANCED_BY"})
_OWNERSHIP_EDGES: Final[frozenset[str]] = frozenset({"OWNED_BY", "DEPLOYED_BY"})
_SCHEDULING_EDGES: Final[frozenset[str]] = frozenset({"SCHEDULED_ON", "RUNS_ON"})

ACTION_EDGE_ALLOWLIST: Final[dict[ActionType, frozenset[str]]] = {
    "restart": _RUNTIME_EDGES,
    "delete": _RUNTIME_EDGES | _LOAD_EDGES | _OWNERSHIP_EDGES | _SCHEDULING_EDGES,
    "scale_down": _RUNTIME_EDGES | _LOAD_EDGES,
    "cordon": _SCHEDULING_EDGES,
}

# ---------------------------------------------------------------------------
# Impact-score heuristic constants
# ---------------------------------------------------------------------------

_ACTION_MULTIPLIER: Final[dict[ActionType, float]] = {
    "delete": 1.0,
    "restart": 0.8,
    "scale_down": 0.6,
    "cordon": 0.5,
}

_EDGE_WEIGHT: Final[dict[str, float]] = {
    "DEPENDS_ON": 1.0,
    "CALLS": 1.0,
    "ROUTES_TO": 0.9,
    "LOAD_BALANCED_BY": 0.7,
    "SCHEDULED_ON": 0.7,
    "RUNS_ON": 0.7,
    "DEPLOYED_BY": 0.5,
    "OWNED_BY": 0.4,
}

_DEFAULT_EDGE_WEIGHT: Final[float] = 0.6
_DEPTH_DECAY: Final[float] = 0.6
_CONFIDENCE_FULL: Final[float] = 1.0
_CONFIDENCE_PARTIAL: Final[float] = 0.5


# ---------------------------------------------------------------------------
# Pydantic response schemas
# ---------------------------------------------------------------------------


class DependencyPathStep(BaseModel):
    """One hop in the dependency path from seed to impacted entity."""

    from_entity: str = Field(description="Canonical name of the upstream entity.")
    to_entity: str = Field(description="Canonical name of the downstream entity.")
    edge_type: str = Field(description="Causal edge type connecting the two entities.")


class BlastRadiusImpact(BaseModel):
    """Single impacted entity in the blast-radius response."""

    entity_id: str = Field(description="Canonical name of the impacted entity.")
    entity_type: str = Field(description="Entity kind (e.g. 'service', 'pod', 'function').")
    dependency_path: list[DependencyPathStep] = Field(default_factory=list)
    impact_score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)


class BlastRadiusResponse(BaseModel):
    """Top-level response envelope for ``blast_radius``."""

    seed_entity_id: str = Field(description="Canonical name of the seed entity.")
    action_type: ActionType
    max_depth: int = Field(ge=MIN_MAX_DEPTH, le=MAX_MAX_DEPTH)
    impacted: list[BlastRadiusImpact] = Field(default_factory=list)
    effective_as_of: datetime
    meta: dict[str, Any] | None = None
    calibrated: bool = Field(
        default=False,
        description=(
            "True when impact_score/confidence come from a fitted calibration "
            "artifact. False on the v0.1 deterministic heuristic — automated "
            "clients should gate on this flag before trusting the scores."
        ),
    )


# ---------------------------------------------------------------------------
# Internal dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ScoredImpact:
    entity_id: str
    entity_type: str
    depth: int
    edge_type: str
    impact_score: float
    confidence: float
    path: tuple[DependencyPathStep, ...]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def validate_entity_id(entity_id: str) -> None:
    """Reject empty / non-string seeds at the API boundary."""
    if not isinstance(entity_id, str):  # pragma: no cover - typing barrier
        raise ValueError(f"{INVALID_ENTITY_ID_CODE}:{INVALID_ENTITY_ID_MESSAGE}")
    if not entity_id.strip():
        raise ValueError(f"{INVALID_ENTITY_ID_CODE}:{INVALID_ENTITY_ID_MESSAGE}")


def validate_action_type(action_type: str) -> None:
    """Reject action_type values that fall outside :data:`ACTION_TYPES`."""
    if action_type not in ACTION_TYPES:
        raise ValueError(f"{INVALID_ACTION_TYPE_CODE}:{INVALID_ACTION_TYPE_MESSAGE}")


# ---------------------------------------------------------------------------
# Scoring + path reconstruction — pure functions
# ---------------------------------------------------------------------------


def score_impacts(
    *,
    result: GraphResultView,
    action_type: ActionType,
    as_of: datetime | None,
) -> list[_ScoredImpact]:
    """Score and rank the impacted entities."""
    edge_index = _build_edge_index(result.edges, seed_name=result.seed.name)
    multiplier = _ACTION_MULTIPLIER[action_type]
    best: dict[str, _ScoredImpact] = {}
    for neighbour in result.related:
        if neighbour.name == result.seed.name:
            continue
        edge_type = neighbour.edge_type or _infer_edge_type(neighbour, edge_index)
        if edge_type is None:
            continue
        score = _impact_score(
            depth=neighbour.depth,
            edge_type=edge_type,
            multiplier=multiplier,
        )
        confidence = _confidence(
            seed_recorded=result.seed.recorded_at is not None, neighbour=neighbour, as_of=as_of
        )
        path = _path_for(
            neighbour=neighbour,
            edge_index=edge_index,
            seed_name=result.seed.name,
            edge_type=edge_type,
        )
        candidate = _ScoredImpact(
            entity_id=neighbour.name,
            entity_type=neighbour.kind,
            depth=neighbour.depth,
            edge_type=edge_type,
            impact_score=score,
            confidence=confidence,
            path=path,
        )
        prior = best.get(neighbour.name)
        if prior is None or candidate.impact_score > prior.impact_score:
            best[neighbour.name] = candidate
    return sorted(
        best.values(),
        key=lambda imp: (-imp.impact_score, imp.depth, imp.entity_id),
    )


def _build_edge_index(
    edges: list[GraphEdgeView],
    *,
    seed_name: str,
) -> dict[str, GraphEdgeView]:
    index: dict[str, GraphEdgeView] = {}
    for edge in edges:
        index.setdefault(edge.to_entity, edge)
        index.setdefault(edge.from_entity, edge)
    index.pop(seed_name, None)
    return index


def _infer_edge_type(
    neighbour: EntityNodeView,
    edge_index: dict[str, GraphEdgeView],
) -> str | None:
    edge = edge_index.get(neighbour.name)
    return edge.edge_type if edge else None


def _impact_score(*, depth: int, edge_type: str, multiplier: float) -> float:
    """Compute the v0.1 deterministic impact score."""
    decay = _DEPTH_DECAY ** max(depth - 1, 0)
    weight = _EDGE_WEIGHT.get(edge_type, _DEFAULT_EDGE_WEIGHT)
    raw = multiplier * decay * weight
    if raw < 0.0:
        return 0.0
    if raw > 1.0:
        return 1.0
    return raw


def _confidence(
    *,
    seed_recorded: bool,
    neighbour: EntityNodeView,
    as_of: datetime | None,
) -> float:
    if as_of is None:
        return _CONFIDENCE_FULL
    neighbour_recorded = neighbour.recorded_at is not None
    if seed_recorded and neighbour_recorded:
        return _CONFIDENCE_FULL
    return _CONFIDENCE_PARTIAL


def _path_for(
    *,
    neighbour: EntityNodeView,
    edge_index: dict[str, GraphEdgeView],
    seed_name: str,
    edge_type: str,
) -> tuple[DependencyPathStep, ...]:
    edge = edge_index.get(neighbour.name)
    if edge is None:
        return (
            DependencyPathStep(
                from_entity=seed_name,
                to_entity=neighbour.name,
                edge_type=edge_type,
            ),
        )
    return (
        DependencyPathStep(
            from_entity=edge.from_entity,
            to_entity=edge.to_entity,
            edge_type=edge.edge_type,
        ),
    )


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


def build_blast_meta(
    *,
    action_type: ActionType,
    edge_allowlist: list[str],
    depth: int,
) -> dict[str, Any]:
    """Attach the explainability hints expected by ops dashboards."""
    return {
        "action_type": action_type,
        "edge_allowlist": list(edge_allowlist),
        "max_depth": depth,
        "scoring_model": "v0.1-deterministic",
        "calibrated": False,
    }


def build_blast_response(
    *,
    seed: EntityNodeView,
    action_type: ActionType,
    max_depth: int,
    impacts: list[_ScoredImpact],
    as_of: datetime | None,
    effective_as_of: datetime,
    meta: dict[str, Any],
    degraded_pre_history: str,
) -> BlastRadiusResponse:
    """Convert internal :class:`_ScoredImpact` list into the wire format."""
    impacted = [
        BlastRadiusImpact(
            entity_id=imp.entity_id,
            entity_type=imp.entity_type,
            dependency_path=list(imp.path),
            impact_score=imp.impact_score,
            confidence=imp.confidence,
        )
        for imp in impacts
    ]
    if as_of is not None and not impacts:
        meta = {**meta, "degraded_response": degraded_pre_history}
    return BlastRadiusResponse(
        seed_entity_id=seed.name,
        action_type=action_type,
        max_depth=max_depth,
        impacted=impacted,
        effective_as_of=effective_as_of,
        meta=meta,
        # v0.1 deterministic scorer — flips to True only when a fitted
        # calibration artifact backs impact_score/confidence (#155).
        calibrated=False,
    )


__all__ = [
    "ACTION_EDGE_ALLOWLIST",
    "ACTION_TYPES",
    "DEFAULT_MAX_DEPTH",
    "ENTITY_NOT_FOUND_CODE",
    "INVALID_ACTION_TYPE_CODE",
    "INVALID_ACTION_TYPE_MESSAGE",
    "INVALID_ENTITY_ID_CODE",
    "INVALID_ENTITY_ID_MESSAGE",
    "MAX_MAX_DEPTH",
    "MIN_MAX_DEPTH",
    "ActionType",
    "BlastRadiusImpact",
    "BlastRadiusResponse",
    "DependencyPathStep",
    "build_blast_meta",
    "build_blast_response",
    "score_impacts",
    "validate_action_type",
    "validate_entity_id",
]
