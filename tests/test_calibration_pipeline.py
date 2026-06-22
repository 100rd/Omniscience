import math
from datetime import UTC, datetime, timedelta

from omniscience_retrieval.incidents.calibration import (
    CalibrationPipeline,
    calculate_watermark_weight,
    collect_labeled_incidents,
    compute_brier_score,
    compute_ece,
    fit_isotonic_regression,
    bootstrap_metrics,
    get_out_of_fold_predictions,
    apply_isotonic,
)
from omniscience_retrieval.probabilistic_scoring import LAMBDA_TIME_DECAY


def test_calculate_watermark_weight():
    as_of = datetime(2026, 1, 10, tzinfo=UTC)
    # Future/current
    assert calculate_watermark_weight(as_of, as_of) == 1.0

    # 10 days ago
    valid_from = as_of - timedelta(days=10)
    expected_weight = math.exp(-LAMBDA_TIME_DECAY * 10)
    assert math.isclose(calculate_watermark_weight(valid_from, as_of), expected_weight)


def test_collect_labeled_incidents():
    raw_data = [
        {
            "incident_id": "INC-001",
            "predicted_confidence": 0.8,
            "true_label": 1,
            "timestamp": "2026-01-01T12:00:00Z"
        },
        {
            "incident_id": "INC-002",
            "predicted_confidence": 0.3,
            "true_label": 0,
            "timestamp": datetime(2026, 1, 2, tzinfo=UTC)
        },
        {
            "incident_id": "INC-INVALID",
            # missing predicted_confidence
            "true_label": 1
        }
    ]

    incidents = collect_labeled_incidents(raw_data)
    assert len(incidents) == 2
    assert incidents[0].incident_id == "INC-001"
    assert incidents[0].predicted_confidence == 0.8
    assert incidents[0].true_label == 1

    assert incidents[1].incident_id == "INC-002"
    assert incidents[1].timestamp == datetime(2026, 1, 2, tzinfo=UTC)


def test_compute_brier_score():
    predictions = [0.8, 0.2]
    labels = [1, 0]

    # Unweighted Brier: (0.8 - 1)^2 = 0.04; (0.2 - 0)^2 = 0.04
    # Mean = 0.04
    brier = compute_brier_score(predictions, labels)
    assert math.isclose(brier, 0.04)

    # Weighted Brier
    weights = [2.0, 1.0]
    # (2 * 0.04 + 1 * 0.04) / 3 = 0.04
    brier_weighted = compute_brier_score(predictions, labels, weights)
    assert math.isclose(brier_weighted, 0.04)


def test_compute_ece():
    # 10 bins, boundaries at 0.1, 0.2, ...
    # Bin 0.8: contains 0.8 -> avg conf 0.8. avg acc = 1.0. Error = 0.2
    # Bin 0.2: contains 0.2 -> avg conf 0.2. avg acc = 0.0. Error = 0.2
    predictions = [0.8, 0.2]
    labels = [1, 0]

    ece = compute_ece(predictions, labels, num_bins=10)
    assert math.isclose(ece, 0.2)


def test_fit_isotonic_regression():
    # Test PAVA
    predictions = [0.1, 0.4, 0.8]
    labels = [0, 0, 1]

    thresholds, values = fit_isotonic_regression(predictions, labels)
    # The isotonic mapping should perfectly fit this since it's monotonic
    assert thresholds == [0.0, 0.1, 0.4, 0.8, 1.0]
    assert values == [0.0, 0.0, 0.0, 1.0, 1.0]

    # Non-monotonic
    predictions = [0.1, 0.4, 0.8]
    labels = [0, 1, 0]
    # 0.1 -> 0
    # 0.4 -> 1
    # 0.8 -> 0
    # PAVA will merge 0.4 and 0.8 to avg value 0.5
    thresholds, values = fit_isotonic_regression(predictions, labels)
    assert values[1] == 0.0 # for 0.1
    # For 0.4 and 0.8 it should be 0.5
    idx_0_4 = thresholds.index(0.4)
    assert values[idx_0_4] == 0.5


def test_bootstrap_metrics():
    predictions = [0.8, 0.2, 0.9, 0.1]
    labels = [1, 0, 1, 0]
    weights = [1.0, 1.0, 1.0, 1.0]
    metrics = bootstrap_metrics(predictions, labels, weights, num_bootstraps=50)
    assert "brier_ci" in metrics
    assert "ece_ci" in metrics
    assert metrics["brier_ci"][0] <= metrics["brier_ci"][1]


def test_get_out_of_fold_predictions():
    predictions = [0.1, 0.4, 0.8, 0.9, 0.2]
    labels = [0, 0, 1, 1, 0]
    weights = [1.0, 1.0, 1.0, 1.0, 1.0]
    oof_preds = get_out_of_fold_predictions(predictions, labels, weights, k_folds=2)
    assert len(oof_preds) == 5
    for p in oof_preds:
        assert 0.0 <= p <= 1.0


def test_calibration_pipeline_fallback():
    raw_data = [
        {"incident_id": "INC-001", "predicted_confidence": 0.9, "true_label": 1, "timestamp": "2026-01-01T12:00:00Z"},
        {"incident_id": "INC-002", "predicted_confidence": 0.1, "true_label": 0, "timestamp": "2026-01-02T12:00:00Z"}
    ]

    pipeline = CalibrationPipeline(as_of=datetime(2026, 1, 3, tzinfo=UTC), min_samples=10)
    result = pipeline.run(raw_data)

    assert result["mode"] == "uncalibrated"
    assert result["isotonic_thresholds"] == [0.0, 1.0]
    assert result["isotonic_values"] == [0.0, 1.0]
    assert result["brier_score"] < 0.05


def test_calibration_pipeline_calibrated():
    raw_data = []
    # Create enough samples to trigger calibrated mode
    for i in range(15):
        raw_data.append({"incident_id": f"INC-A{i}", "predicted_confidence": 0.8, "true_label": 1})
        raw_data.append({"incident_id": f"INC-B{i}", "predicted_confidence": 0.2, "true_label": 0})

    pipeline = CalibrationPipeline(as_of=datetime(2026, 1, 3, tzinfo=UTC), min_samples=20, k_folds=3)
    result = pipeline.run(raw_data)

    assert result["mode"] == "calibrated"
    assert "brier_score" in result
    assert "brier_ci" in result
    assert "ece" in result
    assert "ece_ci" in result
    assert len(result["isotonic_thresholds"]) >= 2
