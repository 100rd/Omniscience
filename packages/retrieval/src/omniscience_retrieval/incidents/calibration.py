"""Pipeline for confidence calibration using Isotonic Regression with temporal discounting."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from omniscience_retrieval.probabilistic_scoring import LAMBDA_TIME_DECAY


@dataclass
class IncidentLabel:
    """Represents a labeled incident for calibration."""

    incident_id: str
    predicted_confidence: float
    true_label: int  # 1 for correct, 0 for incorrect
    timestamp: datetime


def calculate_watermark_weight(valid_from: datetime, as_of: datetime | None = None) -> float:
    """Calculate sample weight based on exponential time decay (watermark)."""
    if as_of is None:
        as_of = datetime.now(UTC)

    if valid_from.tzinfo is None:
        valid_from = valid_from.replace(tzinfo=UTC)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)

    if valid_from > as_of:
        return 1.0

    delta_days = (as_of - valid_from).total_seconds() / 86400.0
    return math.exp(-LAMBDA_TIME_DECAY * delta_days)


def collect_labeled_incidents(raw_data: list[dict[str, Any]]) -> list[IncidentLabel]:
    """Collect and parse labeled incidents from raw data sources."""
    incidents = []
    for row in raw_data:
        try:
            inc_id = str(row["incident_id"])
            conf = float(row["predicted_confidence"])
            label = int(row["true_label"])
            if "timestamp" in row and isinstance(row["timestamp"], str):
                ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            elif "timestamp" in row and isinstance(row["timestamp"], datetime):
                ts = row["timestamp"]
            else:
                ts = datetime.now(UTC)
            incidents.append(IncidentLabel(inc_id, conf, label, ts))
        except (KeyError, ValueError, TypeError):
            continue
    return incidents


def compute_brier_score(
    predictions: list[float], labels: list[int], weights: list[float] | None = None
) -> float:
    """Compute weighted Brier score."""
    if not predictions:
        return 0.0
    if weights is None:
        weights = [1.0] * len(predictions)

    total_weight = sum(weights)
    if total_weight == 0:
        return 0.0

    zipped = zip(predictions, labels, weights, strict=False)
    weighted_sq_err = sum(w * (p - y) ** 2 for p, y, w in zipped)
    return weighted_sq_err / total_weight


def compute_ece(
    predictions: list[float],
    labels: list[int],
    weights: list[float] | None = None,
    num_bins: int = 10,
) -> float:
    """Compute weighted Expected Calibration Error (ECE)."""
    if not predictions:
        return 0.0
    if weights is None:
        weights = [1.0] * len(predictions)

    bins: list[list[float]] = [[] for _ in range(num_bins)]
    bin_weights: list[list[float]] = [[] for _ in range(num_bins)]
    bin_labels: list[list[int]] = [[] for _ in range(num_bins)]

    for p, y, w in zip(predictions, labels, weights, strict=False):
        b = int(p * num_bins)
        if b == num_bins:
            b = num_bins - 1
        bins[b].append(p)
        bin_labels[b].append(y)
        bin_weights[b].append(w)

    total_weight = sum(weights)
    if total_weight == 0:
        return 0.0

    ece = 0.0
    for b in range(num_bins):
        bw_sum = sum(bin_weights[b])
        if bw_sum == 0:
            continue

        bin_acc = sum(w * y for w, y in zip(bin_weights[b], bin_labels[b], strict=False)) / bw_sum
        bin_conf = sum(w * p for w, p in zip(bin_weights[b], bins[b], strict=False)) / bw_sum

        ece += (bw_sum / total_weight) * abs(bin_acc - bin_conf)

    return ece


def fit_isotonic_regression(
    predictions: list[float], labels: list[int], weights: list[float] | None = None
) -> tuple[list[float], list[float]]:
    """
    Fit isotonic regression using Pool Adjacent Violators Algorithm (PAVA).
    Returns (thresholds, values) suitable for piecewise linear interpolation.
    """
    if not predictions:
        return [0.0, 1.0], [0.0, 1.0]

    if weights is None:
        weights = [1.0] * len(predictions)

    # Sort by predictions
    sorted_indices = sorted(range(len(predictions)), key=lambda i: predictions[i])
    x = [predictions[i] for i in sorted_indices]
    y = [labels[i] for i in sorted_indices]
    w = [weights[i] for i in sorted_indices]

    # Blocks for PAVA: dict with weight, value, start_idx, end_idx
    blocks: list[dict[str, Any]] = []

    for i in range(len(x)):
        blocks.append({"weight": w[i], "value": float(y[i]), "start": i, "end": i})

        # Merge backwards if decreasing
        while len(blocks) > 1 and blocks[-1]["value"] < blocks[-2]["value"]:
            b2 = blocks.pop()
            b1 = blocks.pop()

            new_weight = b1["weight"] + b2["weight"]
            if new_weight > 0:
                new_value = (b1["value"] * b1["weight"] + b2["value"] * b2["weight"]) / new_weight
            else:
                new_value = 0.0

            blocks.append(
                {"weight": new_weight, "value": new_value, "start": b1["start"], "end": b2["end"]}
            )

    # Construct thresholds and values
    thresholds: list[float] = []
    values: list[float] = []

    for b in blocks:
        start_x = x[b["start"]]
        end_x = x[b["end"]]

        if not thresholds or start_x > thresholds[-1]:
            thresholds.append(start_x)
            values.append(b["value"])

        if end_x > thresholds[-1]:
            thresholds.append(end_x)
            values.append(b["value"])

    if not thresholds:
        return [0.0, 1.0], [0.0, 1.0]

    # Ensure it spans 0 to 1
    if thresholds[0] > 0.0:
        thresholds.insert(0, 0.0)
        values.insert(0, values[0])
    if thresholds[-1] < 1.0:
        thresholds.append(1.0)
        values.append(values[-1])

    # Deduplicate thresholds
    final_thresholds = [thresholds[0]]
    final_values = [values[0]]

    for t, v in zip(thresholds[1:], values[1:], strict=False):
        if t > final_thresholds[-1]:
            final_thresholds.append(t)
            final_values.append(v)
        elif t == final_thresholds[-1]:
            final_values[-1] = v

    return final_thresholds, final_values


def apply_isotonic(p: float, thresholds: list[float], values: list[float]) -> float:
    """Apply isotonic piecewise linear interpolation."""
    for i in range(len(thresholds) - 1):
        if thresholds[i] <= p <= thresholds[i + 1]:
            if thresholds[i + 1] == thresholds[i]:
                return values[i]
            t = (p - thresholds[i]) / (thresholds[i + 1] - thresholds[i])
            return values[i] * (1 - t) + values[i + 1] * t
    if p < thresholds[0]:
        return values[0]
    return values[-1]


def get_out_of_fold_predictions(
    predictions: list[float], labels: list[int], weights: list[float], k_folds: int = 5
) -> list[float]:
    """Compute out-of-fold isotonic predictions."""
    n = len(predictions)
    if n < k_folds:
        k_folds = n
    if k_folds <= 1:
        thresholds, values = fit_isotonic_regression(predictions, labels, weights)
        return [apply_isotonic(p, thresholds, values) for p in predictions]

    indices = list(range(n))
    random.seed(42)  # For reproducibility
    random.shuffle(indices)

    oof_preds = [0.0] * n
    fold_sizes = [n // k_folds + (1 if i < n % k_folds else 0) for i in range(k_folds)]

    current = 0
    for i in range(k_folds):
        fold_size = fold_sizes[i]
        val_indices = indices[current : current + fold_size]
        train_indices = [idx for idx in indices if idx not in val_indices]
        current += fold_size

        train_p = [predictions[idx] for idx in train_indices]
        train_y = [labels[idx] for idx in train_indices]
        train_w = [weights[idx] for idx in train_indices]

        thresholds, values = fit_isotonic_regression(train_p, train_y, train_w)

        for idx in val_indices:
            oof_preds[idx] = apply_isotonic(predictions[idx], thresholds, values)

    return oof_preds


def bootstrap_metrics(
    predictions: list[float],
    labels: list[int],
    weights: list[float],
    num_bootstraps: int = 200,
    confidence_level: float = 0.95,
) -> dict[str, tuple[float, float]]:
    """Compute bootstrap confidence intervals for Brier and ECE scores."""
    n = len(predictions)
    if n == 0:
        return {"brier_ci": (0.0, 0.0), "ece_ci": (0.0, 0.0)}

    briers = []
    eces = []
    random.seed(42)

    for _ in range(num_bootstraps):
        idx = [random.randint(0, n - 1) for _ in range(n)]  # noqa: S311
        boot_p = [predictions[i] for i in idx]
        boot_y = [labels[i] for i in idx]
        boot_w = [weights[i] for i in idx]

        briers.append(compute_brier_score(boot_p, boot_y, boot_w))
        eces.append(compute_ece(boot_p, boot_y, boot_w))

    briers.sort()
    eces.sort()

    lower_idx = int((1 - confidence_level) / 2 * num_bootstraps)
    upper_idx = int((1 + confidence_level) / 2 * num_bootstraps)

    if upper_idx >= num_bootstraps:
        upper_idx = num_bootstraps - 1

    return {
        "brier_ci": (briers[lower_idx], briers[upper_idx]),
        "ece_ci": (eces[lower_idx], eces[upper_idx]),
    }


class CalibrationPipeline:
    """End-to-end pipeline for running confidence calibration."""

    def __init__(self, as_of: datetime | None = None, min_samples: int = 50, k_folds: int = 5):
        self.as_of = as_of or datetime.now(UTC)
        if self.as_of.tzinfo is None:
            self.as_of = self.as_of.replace(tzinfo=UTC)
        self.min_samples = min_samples
        self.k_folds = k_folds

    def run(self, raw_data: list[dict[str, Any]]) -> dict[str, Any]:
        """
        Run the full calibration pipeline on raw incident data.
        Returns metrics and isotonic mapping.
        """
        incidents = collect_labeled_incidents(raw_data)
        if not incidents:
            raise ValueError("No valid labeled incidents provided")

        predictions = [inc.predicted_confidence for inc in incidents]
        labels = [inc.true_label for inc in incidents]
        weights = [calculate_watermark_weight(inc.timestamp, self.as_of) for inc in incidents]

        num_samples = len(incidents)

        if num_samples < self.min_samples:
            # Uncalibrated fallback (fixed prior)
            thresholds, values = [0.0, 1.0], [0.0, 1.0]
            eval_predictions = predictions
        else:
            # Out-of-fold predictions for unbiased evaluation
            eval_predictions = get_out_of_fold_predictions(
                predictions, labels, weights, self.k_folds
            )
            # Final isotonic thresholds fitted on full data
            thresholds, values = fit_isotonic_regression(predictions, labels, weights)

        brier = compute_brier_score(eval_predictions, labels, weights)
        ece = compute_ece(eval_predictions, labels, weights)
        ci_metrics = bootstrap_metrics(eval_predictions, labels, weights)

        return {
            "brier_score": brier,
            "brier_ci": ci_metrics["brier_ci"],
            "ece": ece,
            "ece_ci": ci_metrics["ece_ci"],
            "isotonic_thresholds": thresholds,
            "isotonic_values": values,
            "num_samples": num_samples,
            "discounted_weight_sum": sum(weights),
            "mode": "calibrated" if num_samples >= self.min_samples else "uncalibrated",
        }
