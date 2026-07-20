"""Unit tests for the admission fail-boot Settings validator (issue #350).

`Settings._validate_admission_backend_requirements` is the first
Python-side fail-boot pydantic validator in this codebase: when
`admission_backend != "disabled"`, every required input (backend identity,
lane budgets, queue bounds, latency/error thresholds, failure reserve) must
be explicitly set — no code defaults substitute for HA posture.
"""

from __future__ import annotations

import pytest
from omniscience_core.config import Settings
from pydantic import ValidationError

_FULLY_EVIDENCED_ADMISSION_KWARGS: dict[str, object] = {
    "admission_backend": "shared-redis-lease",
    "admission_backend_identity": "redis://admission.internal:6379/0",
    "admission_lane_incident_budget": 120,
    "admission_lane_background_budget": 60,
    "admission_queue_depth_max": 500,
    "admission_latency_threshold_ms": 250.0,
    "admission_error_rate_threshold": 0.01,
    "admission_failure_reserve_fraction": 0.2,
}


def test_default_boot_is_admission_disabled_and_unconstrained() -> None:
    """Constructing Settings with no overrides must not raise — the
    default posture is unaffected by the new admission fields."""
    settings = Settings()
    assert settings.admission_backend == "disabled"
    assert settings.admission_backend_identity is None
    assert settings.rate_limit_rpm == 60


def test_admission_enabled_with_full_evidence_boots_cleanly() -> None:
    settings = Settings(**_FULLY_EVIDENCED_ADMISSION_KWARGS)
    assert settings.admission_backend == "shared-redis-lease"
    assert settings.admission_lane_incident_budget == 120


@pytest.mark.parametrize(
    "missing_field",
    [
        "admission_backend_identity",
        "admission_lane_incident_budget",
        "admission_lane_background_budget",
        "admission_queue_depth_max",
        "admission_latency_threshold_ms",
        "admission_error_rate_threshold",
        "admission_failure_reserve_fraction",
    ],
)
def test_admission_enabled_missing_any_required_field_fails_boot(missing_field: str) -> None:
    kwargs = dict(_FULLY_EVIDENCED_ADMISSION_KWARGS)
    kwargs[missing_field] = None
    with pytest.raises(ValidationError, match=missing_field):
        Settings(**kwargs)


def test_admission_enabled_with_nothing_else_set_fails_boot_naming_all_missing() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(admission_backend="shared-redis-lease")
    message = str(exc_info.value)
    for field in (
        "admission_backend_identity",
        "admission_lane_incident_budget",
        "admission_lane_background_budget",
        "admission_queue_depth_max",
        "admission_latency_threshold_ms",
        "admission_error_rate_threshold",
        "admission_failure_reserve_fraction",
    ):
        assert field in message


def test_unsupported_admission_backend_literal_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(admission_backend="totally-made-up-backend")


# ---------------------------------------------------------------------------
# Boundary values — must agree with helm/omniscience/templates/
# _admission_guards.tpl's numeric floors (BP review HIGH #1). Only
# test_admission_enabled_missing_any_required_field_fails_boot above (the
# `None` case) was covered before; 0 is a distinct, previously-unguarded
# boundary that helm has always rejected but Python did not.
# ---------------------------------------------------------------------------


def test_admission_queue_depth_max_of_zero_fails_boot() -> None:
    """Helm's guard requires queueDepthMax >= 1 (_admission_guards.tpl);
    Python must reject the same boundary, not silently accept it."""
    kwargs = dict(_FULLY_EVIDENCED_ADMISSION_KWARGS)
    kwargs["admission_queue_depth_max"] = 0
    with pytest.raises(ValidationError, match="admission_queue_depth_max"):
        Settings(**kwargs)


def test_admission_queue_depth_max_of_one_boots_cleanly() -> None:
    kwargs = dict(_FULLY_EVIDENCED_ADMISSION_KWARGS)
    kwargs["admission_queue_depth_max"] = 1
    settings = Settings(**kwargs)
    assert settings.admission_queue_depth_max == 1


def test_admission_error_rate_threshold_of_zero_fails_boot() -> None:
    """Helm's guard requires errorRateThreshold > 0.0
    (_admission_guards.tpl); Python must reject the same boundary, not
    silently accept a zero SLO threshold as "present"."""
    kwargs = dict(_FULLY_EVIDENCED_ADMISSION_KWARGS)
    kwargs["admission_error_rate_threshold"] = 0.0
    with pytest.raises(ValidationError, match="admission_error_rate_threshold"):
        Settings(**kwargs)


def test_admission_error_rate_threshold_just_above_zero_boots_cleanly() -> None:
    kwargs = dict(_FULLY_EVIDENCED_ADMISSION_KWARGS)
    kwargs["admission_error_rate_threshold"] = 0.001
    settings = Settings(**kwargs)
    assert settings.admission_error_rate_threshold == 0.001


def test_admission_error_rate_threshold_of_one_boots_cleanly() -> None:
    """Upper bound (le=1) is unchanged by this fix — only the lower bound
    moved from ge=0 to gt=0."""
    kwargs = dict(_FULLY_EVIDENCED_ADMISSION_KWARGS)
    kwargs["admission_error_rate_threshold"] = 1.0
    settings = Settings(**kwargs)
    assert settings.admission_error_rate_threshold == 1.0
