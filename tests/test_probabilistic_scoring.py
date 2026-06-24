"""Unit tests for the probabilistic scoring calculations."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from omniscience_retrieval.probabilistic_scoring import (
    calculate_probabilistic_confidence,
    calculate_probabilistic_impact,
    calculate_probabilistic_incident_confidence,
    calculate_source_reliability,
    calculate_time_decay,
    calculate_watermark_age,
    calibrate_isotonic,
    calibrate_platt,
)


def test_calculate_source_reliability() -> None:
    assert calculate_source_reliability("git") == 0.95
    assert calculate_source_reliability("k8s_operator") == 0.95
    assert calculate_source_reliability("slack") == 0.60
    assert calculate_source_reliability("unknown_source") == 0.70
    assert calculate_source_reliability(None) == 0.70


def test_calculate_time_decay() -> None:
    now = datetime.now(UTC)
    # No decay for None time
    assert calculate_time_decay(None, now) == 1.0

    # 0 days age -> 1.0
    assert calculate_time_decay(now, now) == 1.0

    # 10 days age -> e^(-0.01 * 10) = e^(-0.1)
    ten_days_ago = now - timedelta(days=10)
    expected = math.exp(-0.01 * 10)
    assert pytest.approx(calculate_time_decay(ten_days_ago, now), abs=1e-5) == expected


def test_calculate_probabilistic_confidence() -> None:
    now = datetime.now(UTC)
    # depth=1, age=0, score_type="raw" -> confidence should equal source reliability
    assert (
        calculate_probabilistic_confidence(
            source="git", valid_from=now, depth=1, as_of=now, score_type="raw"
        )
        == 0.95
    )

    # depth=2, age=0, score_type="raw" -> 0.95 * e^(-0.15 * 1)
    expected = 0.95 * math.exp(-0.15)
    result = calculate_probabilistic_confidence(
        source="git", valid_from=now, depth=2, as_of=now, score_type="raw"
    )
    assert pytest.approx(result, abs=1e-5) == expected


def test_calibration_mappings() -> None:
    # Test Platt scaling
    # calibrate_platt(0.5, a=-2.0, b=0.5) => 1 / (1 + exp(-2*0.5 + 0.5))
    # = 1 / (1 + exp(-0.5))
    expected_platt = 1.0 / (1.0 + math.exp(-0.5))
    assert pytest.approx(calibrate_platt(0.5), abs=1e-5) == expected_platt

    # Test Isotonic regression mapping
    # Default thresholds: [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    # Default values: [0.0, 0.15, 0.35, 0.60, 0.85, 1.0]
    # score = 0.3 -> i=1 (thresholds 0.2 to 0.4, values 0.15 to 0.35)
    # expected = 0.15 + (0.3 - 0.2) * (0.35 - 0.15) / (0.4 - 0.2) = 0.25
    assert pytest.approx(calibrate_isotonic(0.3), abs=1e-5) == 0.25

    # Test calibrated score_type in calculate_probabilistic_confidence
    now = datetime.now(UTC)
    raw_val = calculate_probabilistic_confidence(
        source="git", valid_from=now, depth=1, as_of=now, score_type="raw"
    )
    cal_platt_val = calculate_probabilistic_confidence(
        source="git",
        valid_from=now,
        depth=1,
        as_of=now,
        score_type="calibrated",
        calibration_method="platt",
    )
    assert cal_platt_val == calibrate_platt(raw_val)

    cal_isotonic_val = calculate_probabilistic_confidence(
        source="git",
        valid_from=now,
        depth=1,
        as_of=now,
        score_type="calibrated",
        calibration_method="isotonic",
    )
    assert cal_isotonic_val == calibrate_isotonic(raw_val)


def test_calculate_probabilistic_impact() -> None:
    now = datetime.now(UTC)
    # centrality=0 -> baseline topo=0.5. depth=1 -> depth decay=1.0.
    # source reliability for slack=0.6. age=0 -> 1.0.
    # Expected: 0.5 * 1.0 * 0.6 * 1.0 = 0.3
    result = calculate_probabilistic_impact(
        source="slack", valid_from=now, depth=1, centrality=0.0, as_of=now
    )
    assert pytest.approx(result, abs=1e-5) == 0.3

    # centrality=10 -> topo = 1.0 - e^(-0.15 * 10) = 1.0 - e^(-1.5)
    topo = 1.0 - math.exp(-1.5)
    expected = topo * 1.0 * 0.95 * 1.0
    result = calculate_probabilistic_impact(
        source="git", valid_from=now, depth=1, centrality=10.0, as_of=now
    )
    assert pytest.approx(result, abs=1e-5) == expected


def test_calculate_probabilistic_incident_confidence() -> None:
    now = datetime.now(UTC)
    alert = MagicMock()
    alert.source = "alerts-receiver"
    alert.valid_from = now

    classified = MagicMock()
    classified.responsible_pr = None
    classified.target_resource = None

    # Only alert exists (base_p = 0.15, alert_rel = 0.70 for alerts, alert_decay = 1.0)
    # Expected: 0.15 * 0.70 * 1.0 = 0.105
    raw_expected = 0.105
    expected = calibrate_isotonic(raw_expected, support_size=100)
    res, is_prov = calculate_probabilistic_incident_confidence(
        alert=alert, classified=classified, max_depth=3, as_of=now, support_size=100
    )
    assert not is_prov
    assert pytest.approx(res, abs=1e-5) == expected

    # With target resource (base_p = 0.45, depth=2, res_rel = 0.95 for k8s)
    res_node = MagicMock()
    res_node.source = "k8s"
    res_node.valid_from = now
    res_node.depth = 2
    classified.target_resource = res_node

    # Expected: 0.45 * 0.70 * 0.95 * 1.0 * 1.0 * e^(-0.15 * 1) = 0.29925 * e^(-0.15)
    raw_expected = 0.45 * 0.70 * 0.95 * math.exp(-0.15)
    expected = calibrate_isotonic(raw_expected, support_size=100)
    res, is_prov = calculate_probabilistic_incident_confidence(
        alert=alert, classified=classified, max_depth=3, as_of=now, support_size=100
    )
    assert not is_prov
    assert pytest.approx(res, abs=1e-5) == expected


def test_temporal_hold_out() -> None:
    now = datetime.now(UTC)
    # Test that watermark age correctly calculates hold out
    two_days_ago = now - timedelta(days=2)
    # the age should be 2.0 days
    age = calculate_watermark_age(two_days_ago, now)
    assert pytest.approx(age, abs=1e-5) == 2.0

    # Test hold out future dates (should be 0)
    future = now + timedelta(days=1)
    age_future = calculate_watermark_age(future, now)
    assert age_future == 0.0


def test_cold_start_fallback() -> None:
    score = 0.5
    # with min_support=30, support_size=15
    alpha = 15 / 30
    iso_val = calibrate_isotonic(score, support_size=100)  # no fallback
    platt_val = calibrate_platt(score)
    expected = alpha * iso_val + (1.0 - alpha) * platt_val

    actual = calibrate_isotonic(score, support_size=15, min_support=30)
    assert pytest.approx(actual, abs=1e-5) == expected

    # support_size=0 -> purely platt
    actual_cold = calibrate_isotonic(score, support_size=0, min_support=30)
    assert pytest.approx(actual_cold, abs=1e-5) == platt_val
