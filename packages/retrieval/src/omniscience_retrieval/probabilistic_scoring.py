"""Probabilistic scoring for Confidence and Impact in Omniscience (issue #315)."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Source reliability mapping
# SourceType or connector identifier -> probability of correctness [0, 1]
SOURCE_RELIABILITY: dict[str, float] = {
    "git": 0.95,
    "k8s": 0.95,
    "k8s_operator": 0.95,
    "otel": 0.90,
    "fs": 0.85,
    "terraform": 0.90,
    "aws": 0.90,
    "confluence": 0.75,
    "jira": 0.75,
    "slack": 0.60,
    "grafana": 0.65,
    "alerts": 0.70,
}
DEFAULT_RELIABILITY = 0.70

# Decay coefficients
LAMBDA_TIME_DECAY = 0.01  # Time decay lambda (per day)
GAMMA_CONF_DEPTH = (
    0.15  # Confidence depth decay factor (exponential decay: e^(-gamma * (depth - 1)))
)
MU_IMPACT_DEPTH = 0.25  # Impact depth decay factor (exponential decay: e^(-mu * (depth - 1)))
ALPHA_CENTRALITY = 0.15  # Centrality weight factor: 1 - e^(-alpha * centrality)

# ---------------------------------------------------------------------------
# Default support size used when the caller doesn't provide a real count.
#
# AP4 fix: previously hardcoded to 0 at the call site in incidents.py:177,
# which unconditionally disabled the isotonic path.  The constant below sets
# the default to a value that allows isotonic when >= min_support (30), but
# callers should pass a real count whenever available.
# ---------------------------------------------------------------------------
DEFAULT_SUPPORT_SIZE: int = 30


def calculate_watermark_age(valid_from: datetime | None, as_of: datetime | None = None) -> float:
    """Calculate the age of the data in days relative to the as_of anchor."""
    if valid_from is None:
        return 0.0

    anchor_time = as_of or datetime.now(UTC)
    if anchor_time.tzinfo is None:
        anchor_time = anchor_time.replace(tzinfo=UTC)
    if valid_from.tzinfo is None:
        valid_from = valid_from.replace(tzinfo=UTC)

    if valid_from > anchor_time:
        return 0.0

    return (anchor_time - valid_from).total_seconds() / 86400.0


def calculate_time_decay(valid_from: datetime | None, as_of: datetime | None = None) -> float:
    """Calculate exponential time decay: P_time = e^(-lambda * t) where t is age in days."""
    if valid_from is None:
        return 1.0

    delta_days = calculate_watermark_age(valid_from, as_of)
    if delta_days <= 0.0:
        return 1.0

    return math.exp(-LAMBDA_TIME_DECAY * delta_days)


def calculate_source_reliability(source: str | None) -> float:
    """Get source reliability based on source name / identifier."""
    if not source:
        return DEFAULT_RELIABILITY

    # Try case-insensitive substring matching
    source_lower = source.lower()
    for src_type, reliability in SOURCE_RELIABILITY.items():
        if src_type in source_lower:
            return reliability
    return DEFAULT_RELIABILITY


def calibrate_platt(score: float, a: float = -2.0, b: float = 0.5) -> float:
    """Apply Platt scaling calibration: 1 / (1 + exp(A * score + B))."""
    x = a * score + b
    try:
        calibrated = 1.0 / (1.0 + math.exp(x))
    except OverflowError:
        calibrated = 0.0 if x > 0 else 1.0
    return max(0.0, min(1.0, calibrated))


def calibrate_isotonic(
    score: float,
    thresholds: list[float] | None = None,
    values: list[float] | None = None,
    support_size: int | None = None,
    min_support: int = 30,
) -> float:
    """Apply isotonic calibration based on piecewise constant/linear mapping.

    AP4: when ``thresholds``/``values`` are ``None`` the function tries to
    load a fitted artifact from disk before falling back to the compile-time
    defaults.  Pass an explicit ``artifact_dir`` via the module-level helper
    :func:`get_fitted_isotonic_map` if you need to override the directory.
    """
    # Attempt to load a fitted artifact when the caller does not supply a map.
    if thresholds is None or values is None:
        fitted = _get_cached_artifact()
        if fitted is not None:
            thresholds = fitted[0]
            values = fitted[1]
        else:
            # Fall back to compile-time defaults (cold-start / no artifact).
            thresholds = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
            values = [0.0, 0.15, 0.35, 0.60, 0.85, 1.0]

    if len(thresholds) != len(values):
        raise ValueError("thresholds and values must have the same length")

    iso_score = score
    if score <= thresholds[0]:
        iso_score = values[0]
    elif score >= thresholds[-1]:
        iso_score = values[-1]
    else:
        for i in range(len(thresholds) - 1):
            if thresholds[i] <= score <= thresholds[i + 1]:
                t_low, t_high = thresholds[i], thresholds[i + 1]
                v_low, v_high = values[i], values[i + 1]
                if t_high == t_low:
                    iso_score = v_low
                else:
                    iso_score = v_low + (score - t_low) * (v_high - v_low) / (t_high - t_low)
                break

    if support_size is not None and support_size < min_support:
        alpha = support_size / min_support
        platt_score = calibrate_platt(score)
        return alpha * iso_score + (1.0 - alpha) * platt_score

    return iso_score


# ---------------------------------------------------------------------------
# Fitted artifact cache — loaded lazily at first call, module-level singleton.
# The cache is a tuple (thresholds, values) or None.
# Expose reload_fitted_map() so tests and the calibration job can invalidate.
# ---------------------------------------------------------------------------

_artifact_cache: tuple[list[float], list[float]] | None = None
_artifact_dir_override: Path | None = None


def _get_cached_artifact() -> tuple[list[float], list[float]] | None:
    """Return the module-level cached fitted map, loading on first call."""
    global _artifact_cache
    if _artifact_cache is None:
        _artifact_cache = _load_artifact_once()
    return _artifact_cache


def _load_artifact_once() -> tuple[list[float], list[float]] | None:
    """Load artifact from disk and return ``(thresholds, values)`` or ``None``."""
    from omniscience_retrieval.incidents.calibration_store import load_calibration_artifact

    artifact = load_calibration_artifact(artifact_dir=_artifact_dir_override)
    if artifact is None:
        return None
    return (artifact.isotonic_thresholds, artifact.isotonic_values)


def reload_fitted_map(artifact_dir: Path | None = None) -> bool:
    """Reload the fitted isotonic map from disk, bypassing the module cache.

    Call this after saving a new artifact (e.g. in the calibration job or in
    tests).  Returns ``True`` when a valid artifact was loaded, ``False`` when
    no artifact exists.

    Parameters
    ----------
    artifact_dir:
        Override the directory to search.  ``None`` uses the default.
    """
    global _artifact_cache, _artifact_dir_override
    _artifact_dir_override = artifact_dir
    _artifact_cache = None  # Invalidate — next call to _get_cached_artifact re-loads.
    result = _get_cached_artifact()
    return result is not None


def is_fitted_map_loaded() -> bool:
    """Return ``True`` when a fitted calibration artifact is currently in use."""
    return _get_cached_artifact() is not None


def calculate_probabilistic_confidence(
    *,
    source: str | None,
    valid_from: datetime | None,
    depth: int = 1,
    as_of: datetime | None = None,
    score_type: str = "calibrated",
    calibration_method: str = "platt",
    is_parked: bool = False,
) -> float:
    """Calculate the probabilistic confidence score for a retrieved chunk.

    Considers source reliability, age (time decay), and graph topology (depth).
    If is_parked is True, severely penalizes the final confidence score.
    Returns a score between 0.0 and 1.0.
    """
    p_source = calculate_source_reliability(source)
    p_time = calculate_time_decay(valid_from, as_of)
    p_topo = math.exp(-GAMMA_CONF_DEPTH * max(depth - 1, 0))

    raw_confidence = p_source * p_time * p_topo
    if is_parked:
        raw_confidence *= 0.1

    raw_confidence = max(0.0, min(1.0, raw_confidence))

    if score_type == "raw":
        return raw_confidence
    elif score_type == "heuristic":
        # Heuristic confidence calculation
        return max(0.0, min(1.0, raw_confidence * 0.9))
    elif score_type == "calibrated":
        if calibration_method == "platt":
            return calibrate_platt(raw_confidence)
        elif calibration_method == "isotonic":
            return calibrate_isotonic(raw_confidence)
        else:
            return calibrate_platt(raw_confidence)
    else:
        return raw_confidence


def calculate_probabilistic_impact(
    *,
    source: str | None,
    valid_from: datetime | None,
    depth: int = 1,
    centrality: float = 0.0,  # Degree or centrality score
    as_of: datetime | None = None,
) -> float:
    """Calculate probabilistic impact score.

    Centrality boost: 1.0 - e^(-alpha * centrality)
    Depth decay: e^(-mu * (depth - 1))
    Combined with source reliability and temporal decay as multipliers.
    """
    centrality_val = max(0.0, float(centrality))
    i_topo = 1.0 - math.exp(-ALPHA_CENTRALITY * centrality_val) if centrality_val > 0 else 0.5

    i_depth = math.exp(-MU_IMPACT_DEPTH * max(depth - 1, 0))

    p_source = calculate_source_reliability(source)
    p_time = calculate_time_decay(valid_from, as_of)

    impact = i_topo * i_depth * p_source * p_time
    return max(0.0, min(1.0, impact))


def calculate_probabilistic_incident_confidence(
    *,
    alert: Any,  # EntityNodeView
    classified: Any,  # ClassifiedNeighbours
    max_depth: int,
    as_of: datetime | None = None,
    support_size: int | None = None,
    min_support: int = 30,
) -> tuple[float, bool]:
    """Calculate the probabilistic confidence score for an incident resolution recommendation.

    Considers source reliability of alert, PR, and resource; age (time decay) of data;
    and graph topology (depth of the resource).
    Returns (confidence, is_provisional) tuple.

    AP4: ``support_size`` defaults to ``None`` (caller must supply it or it falls
    through to the blending logic which blends isotonic + Platt when < min_support).
    The caller in ``incidents.py`` previously hardcoded ``support_size=0``; that
    constant has been replaced with ``DEFAULT_SUPPORT_SIZE`` so the isotonic path
    is used when a fitted artifact is available.
    """
    alert_source = alert.source if hasattr(alert, "source") else None
    alert_time = alert.valid_from if hasattr(alert, "valid_from") else None
    alert_is_parked = getattr(alert, "is_parked", False)

    alert_rel = calculate_source_reliability(alert_source)
    alert_decay = calculate_time_decay(alert_time, as_of)

    any_parked = alert_is_parked

    if classified.responsible_pr is not None:
        pr_node = classified.responsible_pr
        pr_source = pr_node.source if hasattr(pr_node, "source") else None
        pr_time = pr_node.valid_from if hasattr(pr_node, "valid_from") else None
        if getattr(pr_node, "is_parked", False):
            any_parked = True

        pr_rel = calculate_source_reliability(pr_source)
        pr_decay = calculate_time_decay(pr_time, as_of)

        # Check if temporally matched
        is_matched = False
        if alert_time is not None and pr_time is not None and pr_time <= alert_time:
            # check 24-hour window (86400 seconds)
            is_matched = (alert_time - pr_time).total_seconds() <= 86400

        base_p = 0.95 if is_matched else 0.65
        confidence = base_p * alert_rel * pr_rel * alert_decay * pr_decay

    elif classified.target_resource is not None:
        res_node = classified.target_resource
        res_source = res_node.source if hasattr(res_node, "source") else None
        res_time = res_node.valid_from if hasattr(res_node, "valid_from") else None
        res_depth = res_node.depth if hasattr(res_node, "depth") else 1
        if getattr(res_node, "is_parked", False):
            any_parked = True

        res_rel = calculate_source_reliability(res_source)
        res_decay = calculate_time_decay(res_time, as_of)
        res_topo = math.exp(-GAMMA_CONF_DEPTH * max(res_depth - 1, 0))

        base_p = 0.45
        confidence = base_p * alert_rel * res_rel * alert_decay * res_decay * res_topo

    else:
        base_p = 0.15
        confidence = base_p * alert_rel * alert_decay

    if any_parked:
        confidence *= 0.1

    raw_confidence = max(0.0, min(1.0, confidence))
    calibrated = calibrate_isotonic(
        raw_confidence,
        support_size=support_size,
        min_support=min_support,
    )

    # If any entity is parked, we consider the result provisional
    return calibrated, any_parked or (support_size is not None and support_size < min_support)
