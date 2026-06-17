"""Calibrated confidence scoring for ``resolve_incident`` (issue #155).

This module replaces the v0.1 fixed 4-rung ladder in
:mod:`omniscience_retrieval.incidents.resolution` with a per-tenant
tunable model.

Design summary
--------------

The v0.1 ladder (0.9 / 0.6 / 0.4 / 0.1) is a placeholder; epic #99 vision
§11 calls out identity-resolution accuracy as the key calibration gate.
This module introduces:

1. **Four scoring components** in ``[0, 1]``:
   - ``recency``           — PR-merge / alert-fire temporal alignment.
   - ``graph_proximity``   — closeness of the resolved resource (BFS depth).
   - ``evidence_count``    — PR + resource + Slack-thread presence fan-in.
   - ``cross_ref_strength``— v0.1 ladder rung, kept as a backstop signal.

2. **Per-tenant weights** persisted in ``workspaces.settings`` JSONB under
   the ``incident_scoring`` key.  Sum-to-1.0 (± 1e-3); each in ``[0, 1]``.

3. **Trust threshold** (default 0.6 per vision §11). Scores below the
   threshold do NOT mutate ``confidence_score`` — they only flip the
   ``meta.below_trust_threshold`` flag so callers degrade gracefully.

4. **Default behaviour**: when a workspace has no ``incident_scoring``
   row in ``settings`` the v0.1 ladder is preserved EXACTLY (issue #155
   acceptance criterion: existing #184 tests must pass unmodified).
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from omniscience_core.db.models import Workspace
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

DEFAULT_CONFIDENCE_THRESHOLD: Final[float] = 0.6
WEIGHT_SUM_TOLERANCE: Final[float] = 1e-3
SETTINGS_KEY: Final[str] = "incident_scoring"
DEFAULT_WEIGHTS: Final[dict[str, float]] = {
    "recency": 0.25,
    "graph_proximity": 0.25,
    "evidence_count": 0.25,
    "cross_ref_strength": 0.25,
}


# ---------------------------------------------------------------------------
# Pydantic models — REST surface contracts
# ---------------------------------------------------------------------------


class IncidentScoringWeights(BaseModel):
    """Per-component weights for the calibrated confidence score."""

    recency: float = Field(ge=0.0, le=1.0)
    graph_proximity: float = Field(ge=0.0, le=1.0)
    evidence_count: float = Field(ge=0.0, le=1.0)
    cross_ref_strength: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _sum_to_one(self) -> IncidentScoringWeights:
        total = self.recency + self.graph_proximity + self.evidence_count + self.cross_ref_strength
        if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
            raise ValueError(
                f"weights must sum to 1.0 (± {WEIGHT_SUM_TOLERANCE}); got {total:.6f}"
            )
        return self

    def as_dict(self) -> dict[str, float]:
        """Return a plain ``dict`` for JSON persistence."""
        return {
            "recency": self.recency,
            "graph_proximity": self.graph_proximity,
            "evidence_count": self.evidence_count,
            "cross_ref_strength": self.cross_ref_strength,
        }


class IncidentScoringConfig(BaseModel):
    """The full per-workspace scoring configuration.

    ``weights`` is optional — when absent the workspace falls back to
    the v0.1 ladder (preserving the #184 contract).  ``confidence_threshold``
    is always present and defaults to :data:`DEFAULT_CONFIDENCE_THRESHOLD`.
    """

    weights: IncidentScoringWeights | None = None
    confidence_threshold: float = Field(default=DEFAULT_CONFIDENCE_THRESHOLD, ge=0.0, le=1.0)
    temporal_decay_half_life_seconds: float = Field(default=43200.0, gt=0.0)

    @field_validator("confidence_threshold")
    @classmethod
    def _check_threshold(cls, value: float) -> float:
        return value


class IncidentScoringResponse(BaseModel):
    """GET response for the scoring admin endpoint."""

    weights: IncidentScoringWeights
    confidence_threshold: float = Field(ge=0.0, le=1.0)
    temporal_decay_half_life_seconds: float = Field(gt=0.0)
    weights_source: str = Field(
        description=(
            "'workspace' if the workspace has a stored override, 'default' "
            "if the response reflects the equal-weights fallback."
        ),
    )


class IncidentScoringUpdateRequest(BaseModel):
    """PUT request body for the scoring admin endpoint."""

    weights: IncidentScoringWeights
    confidence_threshold: float = Field(default=DEFAULT_CONFIDENCE_THRESHOLD, ge=0.0, le=1.0)
    temporal_decay_half_life_seconds: float = Field(default=43200.0, gt=0.0)


# ---------------------------------------------------------------------------
# Component extraction — pure, deterministic, no IO
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ScoringInputs:
    """Bundled inputs for component derivation."""

    has_pr: bool
    pr_has_merge_ts: bool
    pr_delta_seconds: float | None
    has_resource: bool
    resource_depth: int
    thread_count: int
    max_depth: int
    temporal_decay_half_life_seconds: float


def _derive_inputs(
    *,
    alert_valid_from: datetime | None,
    pr_valid_from: datetime | None,
    has_pr: bool,
    has_resource: bool,
    resource_depth: int,
    thread_count: int,
    max_depth: int,
    temporal_decay_half_life_seconds: float,
) -> _ScoringInputs:
    """Reduce the classified BFS view to the inputs the components need."""
    pr_has_merge_ts = pr_valid_from is not None
    pr_delta_seconds = None
    if alert_valid_from is not None and pr_valid_from is not None:
        pr_delta_seconds = (alert_valid_from - pr_valid_from).total_seconds()

    return _ScoringInputs(
        has_pr=has_pr,
        pr_has_merge_ts=pr_has_merge_ts,
        pr_delta_seconds=pr_delta_seconds,
        has_resource=has_resource,
        resource_depth=resource_depth,
        thread_count=thread_count,
        max_depth=max(1, max_depth),
        temporal_decay_half_life_seconds=temporal_decay_half_life_seconds,
    )


def _component_recency(inputs: _ScoringInputs) -> float:
    """Continuous exponential decay based on time difference between PR merge and alert firing."""
    if not inputs.has_pr:
        return 0.0
    if not inputs.pr_has_merge_ts or inputs.pr_delta_seconds is None:
        return 0.5
    if inputs.pr_delta_seconds < 0:
        return 0.0
    decay_constant = math.log(2.0) / inputs.temporal_decay_half_life_seconds
    return math.exp(-decay_constant * inputs.pr_delta_seconds)


def _component_graph_proximity(inputs: _ScoringInputs) -> float:
    """Closer-depth resources score higher; absent resources score 0."""
    if not inputs.has_resource:
        return 0.0
    depth = max(1, inputs.resource_depth)
    return 1.0 / depth


def _component_evidence_count(inputs: _ScoringInputs) -> float:
    """Normalised fan-in across (PR, resource, threads)."""
    pr_signal = 1.0 if inputs.has_pr else 0.0
    resource_signal = 1.0 if inputs.has_resource else 0.0
    thread_signal = min(inputs.thread_count, 3) / 3.0
    return (pr_signal + resource_signal + thread_signal) / 3.0


def _component_cross_ref_strength(inputs: _ScoringInputs) -> float:
    """Mirror of the v0.1 ladder, now continuous, scaled to ``[0, 1]``."""
    if inputs.has_pr:
        recency = _component_recency(inputs)
        return 0.6 + 0.3 * recency
    if inputs.has_resource:
        return 0.4
    return 0.1


def compute_components(
    *,
    alert_valid_from: datetime | None,
    pr_valid_from: datetime | None,
    has_pr: bool,
    has_resource: bool,
    resource_depth: int,
    thread_count: int,
    max_depth: int,
    temporal_decay_half_life_seconds: float = 43200.0,
) -> dict[str, float]:
    """Public entry point for component derivation.

    Pure function: same inputs -> same outputs.  Used by both the live
    scoring path and the offline calibration harness.
    """
    inputs = _derive_inputs(
        alert_valid_from=alert_valid_from,
        pr_valid_from=pr_valid_from,
        has_pr=has_pr,
        has_resource=has_resource,
        resource_depth=resource_depth,
        thread_count=thread_count,
        max_depth=max_depth,
        temporal_decay_half_life_seconds=temporal_decay_half_life_seconds,
    )
    return {
        "recency": _component_recency(inputs),
        "graph_proximity": _component_graph_proximity(inputs),
        "evidence_count": _component_evidence_count(inputs),
        "cross_ref_strength": _component_cross_ref_strength(inputs),
    }


def apply_weights(components: dict[str, float], weights: IncidentScoringWeights) -> float:
    """Linear combination of components by weights.  Output clamped to ``[0, 1]``."""
    raw = (
        components["recency"] * weights.recency
        + components["graph_proximity"] * weights.graph_proximity
        + components["evidence_count"] * weights.evidence_count
        + components["cross_ref_strength"] * weights.cross_ref_strength
    )
    if raw < 0.0:
        return 0.0
    if raw > 1.0:
        return 1.0
    return raw


# ---------------------------------------------------------------------------
# Persistence helpers — read/write workspaces.settings JSONB
# ---------------------------------------------------------------------------


def _parse_config(raw: Any) -> IncidentScoringConfig | None:
    """Parse a ``workspaces.settings['incident_scoring']`` blob."""
    if not isinstance(raw, dict):
        return None
    try:
        weights_raw = raw.get("weights")
        weights: IncidentScoringWeights | None
        if isinstance(weights_raw, dict):
            weights = IncidentScoringWeights.model_validate(weights_raw)
        else:
            weights = None
        threshold_raw = raw.get("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD)
        threshold = float(threshold_raw)
        decay_raw = raw.get("temporal_decay_half_life_seconds", 43200.0)
        decay = float(decay_raw)
        return IncidentScoringConfig(
            weights=weights,
            confidence_threshold=threshold,
            temporal_decay_half_life_seconds=decay,
        )
    except (ValueError, TypeError):
        return None


async def load_workspace_config(
    session: AsyncSession, workspace_id: uuid.UUID
) -> IncidentScoringConfig | None:
    """Return the workspace's scoring config or ``None`` when unset."""
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        return None
    settings = workspace.settings
    if not isinstance(settings, dict):
        return None
    return _parse_config(settings.get(SETTINGS_KEY))


async def save_workspace_config(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    config: IncidentScoringConfig,
) -> None:
    """Persist the workspace's scoring config.

    Merges into ``workspaces.settings`` rather than replacing it, so
    other settings keys are preserved.  Raises ``ValueError`` when the
    workspace does not exist; the REST handler maps that to 404.
    """
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError(f"workspace_not_found:{workspace_id}")
    payload: dict[str, Any] = {
        "confidence_threshold": float(config.confidence_threshold),
        "temporal_decay_half_life_seconds": float(config.temporal_decay_half_life_seconds),
    }
    if config.weights is not None:
        payload["weights"] = config.weights.as_dict()
    current = dict(workspace.settings or {})
    current[SETTINGS_KEY] = payload
    workspace.settings = current
    flag_modified(workspace, "settings")
    await session.flush()


__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_WEIGHTS",
    "SETTINGS_KEY",
    "WEIGHT_SUM_TOLERANCE",
    "IncidentScoringConfig",
    "IncidentScoringResponse",
    "IncidentScoringUpdateRequest",
    "IncidentScoringWeights",
    "apply_weights",
    "compute_components",
    "load_workspace_config",
    "save_workspace_config",
]
