"""Calibrated confidence scoring for ``resolve_incident`` (issue #155).

This module replaces the v0.1 fixed 4-rung ladder in
:mod:`omniscience_server.incidents` with a per-tenant tunable model.

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

Schema decision (issue #155 §E)
-------------------------------

Reuse the existing :attr:`Workspace.settings` JSONB column rather than
introducing a new ``WorkspaceSetting`` table. Rationale:

* The setting is a single small JSON document, not a key/value bag — a
  separate table adds ORM complexity without payoff.
* :attr:`Workspace.settings` already exists with ``server_default '{}'``
  and is the documented surface for per-workspace tuning.
* No Alembic migration required; backward-compatible with #184.

The shape stored is::

    workspaces.settings = {
        "incident_scoring": {
            "weights": {
                "recency": float,
                "graph_proximity": float,
                "evidence_count": float,
                "cross_ref_strength": float,
            },
            "confidence_threshold": float,
        },
    }

Other callers writing to ``workspaces.settings`` MUST merge rather than
overwrite — the helpers in this module follow that contract.
"""

from __future__ import annotations

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

#: Vision §11 default trust threshold — scores below this set the
#: ``meta.below_trust_threshold`` flag.
DEFAULT_CONFIDENCE_THRESHOLD: Final[float] = 0.6

#: Permitted floating-point slack on the sum-to-1.0 weight constraint.
#: Tighter than the calibrator's own tolerance so harness output passes
#: the API validator.
WEIGHT_SUM_TOLERANCE: Final[float] = 1e-3

#: JSONB key on ``workspaces.settings`` where the scoring config lives.
SETTINGS_KEY: Final[str] = "incident_scoring"

#: Equal default weights — used by the calibrated formula when the
#: tenant has no override.  Note: when weights are absent entirely the
#: caller falls back to the v0.1 ladder for byte-for-byte compatibility
#: with the #184 tests.
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
    """Per-component weights for the calibrated confidence score.

    All four components must be in ``[0, 1]`` and the four values must
    sum to ``1.0`` within :data:`WEIGHT_SUM_TOLERANCE`.
    """

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

    @field_validator("confidence_threshold")
    @classmethod
    def _check_threshold(cls, value: float) -> float:
        # Pydantic ge/le already covers the [0,1] bound; this slot is kept
        # so a future calibration harness can plug in tenant-specific
        # bounds without touching the API surface.
        return value


class IncidentScoringResponse(BaseModel):
    """GET response for the scoring admin endpoint."""

    weights: IncidentScoringWeights
    confidence_threshold: float = Field(ge=0.0, le=1.0)
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


# ---------------------------------------------------------------------------
# Component extraction — pure, deterministic, no IO
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ScoringInputs:
    """Bundled inputs for component derivation — keeps the surface explicit."""

    has_pr: bool
    pr_temporal_match: bool
    pr_has_merge_ts: bool
    has_resource: bool
    resource_depth: int  # 0 when no resource resolved
    thread_count: int
    max_depth: int


def _derive_inputs(
    *,
    alert_valid_from: datetime | None,
    pr_valid_from: datetime | None,
    has_pr: bool,
    has_resource: bool,
    resource_depth: int,
    thread_count: int,
    max_depth: int,
    pr_recency_window_seconds: int,
) -> _ScoringInputs:
    """Reduce the classified BFS view to the inputs the components need."""
    pr_has_merge_ts = pr_valid_from is not None
    pr_temporal_match = _is_temporal_match(
        alert_valid_from=alert_valid_from,
        pr_valid_from=pr_valid_from,
        window_seconds=pr_recency_window_seconds,
    )
    return _ScoringInputs(
        has_pr=has_pr,
        pr_temporal_match=pr_temporal_match,
        pr_has_merge_ts=pr_has_merge_ts,
        has_resource=has_resource,
        resource_depth=resource_depth,
        thread_count=thread_count,
        max_depth=max(1, max_depth),
    )


def _is_temporal_match(
    *,
    alert_valid_from: datetime | None,
    pr_valid_from: datetime | None,
    window_seconds: int,
) -> bool:
    """Mirror of :func:`incidents._is_temporally_correlated` for derivation."""
    if alert_valid_from is None or pr_valid_from is None:
        return False
    if pr_valid_from > alert_valid_from:
        return False
    delta = (alert_valid_from - pr_valid_from).total_seconds()
    return 0 <= delta <= window_seconds


def _component_recency(inputs: _ScoringInputs) -> float:
    """1.0 when the PR merged inside the recency window before alert.

    Returns 0.5 when a PR is present but lacks a merge timestamp (the
    "we know about a PR but cannot confirm timing" rung).  Returns 0.0
    when there is no PR.
    """
    if inputs.pr_temporal_match:
        return 1.0
    if inputs.has_pr and inputs.pr_has_merge_ts:
        # PR present, has timestamp, but outside the window or after fire.
        return 0.0
    if inputs.has_pr:
        # PR present, no timestamp -> midpoint signal.
        return 0.5
    return 0.0


def _component_graph_proximity(inputs: _ScoringInputs) -> float:
    """Closer-depth resources score higher; absent resources score 0."""
    if not inputs.has_resource:
        return 0.0
    # Depth 1 -> 1.0; depth N -> 1/N down to 1/MAX_DEPTH.
    depth = max(1, inputs.resource_depth)
    return 1.0 / depth


def _component_evidence_count(inputs: _ScoringInputs) -> float:
    """Normalised fan-in across (PR, resource, threads)."""
    pr_signal = 1.0 if inputs.has_pr else 0.0
    resource_signal = 1.0 if inputs.has_resource else 0.0
    # Cap thread contribution at 3 so a runaway Slack channel doesn't
    # dominate the signal.
    thread_signal = min(inputs.thread_count, 3) / 3.0
    return (pr_signal + resource_signal + thread_signal) / 3.0


def _component_cross_ref_strength(inputs: _ScoringInputs) -> float:
    """Mirror of the v0.1 ladder, scaled to ``[0, 1]``.

    Kept as a backstop signal so a workspace with weights ``{0,0,0,1}``
    reproduces the v0.1 ladder under the calibrated formula.
    """
    if inputs.pr_temporal_match:
        return 0.9
    if inputs.has_pr:
        return 0.6
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
    pr_recency_window_seconds: int,
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
        pr_recency_window_seconds=pr_recency_window_seconds,
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
    """Parse a ``workspaces.settings['incident_scoring']`` blob.

    Returns ``None`` when the blob is missing or malformed — falling back
    to the v0.1 ladder is safer than 500-ing live traffic on bad ops data.
    """
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
        return IncidentScoringConfig(weights=weights, confidence_threshold=threshold)
    except (ValueError, TypeError):
        return None


async def load_workspace_config(
    session: AsyncSession, workspace_id: uuid.UUID
) -> IncidentScoringConfig | None:
    """Return the workspace's scoring config or ``None`` when unset.

    ``None`` signals "use the v0.1 ladder + default threshold" — the
    caller (``incidents.mcp_resolve_incident``) interprets this as the
    legacy code path so #184 tests pass byte-for-byte.
    """
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
    }
    if config.weights is not None:
        payload["weights"] = config.weights.as_dict()
    current = dict(workspace.settings or {})
    current[SETTINGS_KEY] = payload
    workspace.settings = current
    # JSONB columns won't be flushed unless SQLAlchemy sees the attribute
    # mutated — flag_modified handles the in-place dict-merge case.
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
