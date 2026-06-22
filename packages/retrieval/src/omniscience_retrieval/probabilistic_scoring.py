"""Probabilistic scoring for Confidence and Impact in Omniscience (issue #315)."""

from __future__ import annotations

import math
from datetime import UTC, datetime
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
    """Apply isotonic calibration based on piecewise constant/linear mapping."""
    if thresholds is None:
        thresholds = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    if values is None:
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


def calculate_probabilistic_confidence(
    *,
    source: str | None,
    valid_from: datetime | None,
    depth: int = 1,
    as_of: datetime | None = None,
    score_type: str = "calibrated",
    calibration_method: str = "platt",
) -> float:
    """Calculate probabilistic confidence: P(Confidence) = P_source * P_time * P_topo.

    P_source: Source reliability
    P_time: Temporal decay
    P_topo: e^(-gamma * (depth - 1))
    """
    p_source = calculate_source_reliability(source)
    p_time = calculate_time_decay(valid_from, as_of)
    p_topo = math.exp(-GAMMA_CONF_DEPTH * max(depth - 1, 0))

    raw_confidence = p_source * p_time * p_topo
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
    """
    alert_source = alert.source if hasattr(alert, "source") else None
    alert_time = alert.valid_from if hasattr(alert, "valid_from") else None

    alert_rel = calculate_source_reliability(alert_source)
    alert_decay = calculate_time_decay(alert_time, as_of)

    if classified.responsible_pr is not None:
        pr_node = classified.responsible_pr
        pr_source = pr_node.source if hasattr(pr_node, "source") else None
        pr_time = pr_node.valid_from if hasattr(pr_node, "valid_from") else None

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

        res_rel = calculate_source_reliability(res_source)
        res_decay = calculate_time_decay(res_time, as_of)
        res_topo = math.exp(-GAMMA_CONF_DEPTH * max(res_depth - 1, 0))

        base_p = 0.45
        confidence = base_p * alert_rel * res_rel * alert_decay * res_decay * res_topo

    else:
        base_p = 0.15
        confidence = base_p * alert_rel * alert_decay

    raw_confidence = max(0.0, min(1.0, confidence))
    calibrated = calibrate_isotonic(
        raw_confidence,
        support_size=support_size,
        min_support=min_support,
    )
    is_provisional = support_size is not None and support_size < min_support
    return calibrated, is_provisional
