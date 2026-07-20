"""Unit tests for the horizontal-read qualification harness (issue #350,
AC-SCALE-5). Every input here is a synthetic/fixture measurement — a real
2+-replica measured run is a decision-return
(docs/runbooks/read-admission.md "Blocked on external — decision returns").
"""

from __future__ import annotations

from omniscience_core.config import Settings
from omniscience_server.admission.qualification import (
    LaneObservation,
    OverloadResponseObservation,
    TenantObservation,
    build_qualification_report,
    score_fairness,
    score_fairness_from_settings,
    score_fallback,
    score_isolation,
    score_queue_depth,
    score_queue_depth_from_settings,
    score_skew,
    validate_report_json,
)

_FULLY_EVIDENCED_ADMISSION_SETTINGS_KWARGS: dict[str, object] = {
    "admission_backend": "shared-redis-lease",
    "admission_backend_identity": "redis://admission.internal:6379/0",
    "admission_lane_incident_budget": 120,
    "admission_lane_background_budget": 60,
    "admission_queue_depth_max": 500,
    "admission_latency_threshold_ms": 250.0,
    "admission_error_rate_threshold": 0.01,
    "admission_failure_reserve_fraction": 0.2,
}

# ---------------------------------------------------------------------------
# Skew
# ---------------------------------------------------------------------------


def test_skew_balanced_replicas_is_green() -> None:
    result = score_skew(
        replica_admitted_counts={"replica-a": 100, "replica-b": 98, "replica-c": 101},
        max_skew_ratio=0.10,
    )
    assert result.is_green


def test_skew_imbalanced_replicas_is_red() -> None:
    result = score_skew(
        replica_admitted_counts={"replica-a": 200, "replica-b": 20},
        max_skew_ratio=0.10,
    )
    assert not result.is_green
    assert any("skew_ratio_exceeded" in r for r in result.reasons)


def test_skew_single_replica_is_red() -> None:
    result = score_skew(replica_admitted_counts={"replica-a": 100}, max_skew_ratio=0.10)
    assert not result.is_green
    assert any("fewer_than_2_replicas_measured" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_isolation_noisy_tenant_does_not_affect_others_is_green() -> None:
    observations = [
        TenantObservation(tenant_id="noisy", requests_sent=1000, requests_admitted=100),
        TenantObservation(tenant_id="quiet-a", requests_sent=100, requests_admitted=99),
        TenantObservation(tenant_id="quiet-b", requests_sent=100, requests_admitted=100),
    ]
    result = score_isolation(
        observations=observations, noisy_tenant_id="noisy", max_cross_tenant_rejection_rate=0.05
    )
    assert result.is_green


def test_isolation_cross_tenant_interference_is_red() -> None:
    observations = [
        TenantObservation(tenant_id="noisy", requests_sent=1000, requests_admitted=100),
        TenantObservation(tenant_id="quiet-a", requests_sent=100, requests_admitted=50),
    ]
    result = score_isolation(
        observations=observations, noisy_tenant_id="noisy", max_cross_tenant_rejection_rate=0.05
    )
    assert not result.is_green
    assert any("cross_tenant_interference" in r for r in result.reasons)


def test_isolation_missing_noisy_tenant_is_red() -> None:
    observations = [
        TenantObservation(tenant_id="quiet-a", requests_sent=100, requests_admitted=99)
    ]
    result = score_isolation(
        observations=observations, noisy_tenant_id="noisy", max_cross_tenant_rejection_rate=0.05
    )
    assert not result.is_green
    assert result.reasons == ("noisy_tenant_not_observed",)


# ---------------------------------------------------------------------------
# Fairness (background degrades first — AC-SCALE-2)
# ---------------------------------------------------------------------------


def test_fairness_background_degrades_first_is_green() -> None:
    incident = LaneObservation(
        lane="incident",
        requests_sent=100,
        requests_admitted=99,
        p99_latency_ms=120,
        error_rate=0.0,
    )
    background = LaneObservation(
        lane="background",
        requests_sent=1000,
        requests_admitted=400,
        p99_latency_ms=800,
        error_rate=0.1,
    )
    result = score_fairness(
        incident=incident,
        background=background,
        incident_latency_threshold_ms=250,
        incident_error_rate_threshold=0.01,
    )
    assert result.is_green


def test_fairness_incident_lane_breached_is_red() -> None:
    incident = LaneObservation(
        lane="incident",
        requests_sent=100,
        requests_admitted=99,
        p99_latency_ms=500,
        error_rate=0.0,
    )
    background = LaneObservation(
        lane="background",
        requests_sent=1000,
        requests_admitted=400,
        p99_latency_ms=800,
        error_rate=0.1,
    )
    result = score_fairness(
        incident=incident,
        background=background,
        incident_latency_threshold_ms=250,
        incident_error_rate_threshold=0.01,
    )
    assert not result.is_green
    assert any("incident_latency_breached" in r for r in result.reasons)


def test_fairness_background_not_degrading_first_is_red() -> None:
    """Both lanes shed equally — background must degrade *more*, not just
    also-eventually."""
    incident = LaneObservation(
        lane="incident",
        requests_sent=100,
        requests_admitted=50,
        p99_latency_ms=100,
        error_rate=0.0,
    )
    background = LaneObservation(
        lane="background",
        requests_sent=100,
        requests_admitted=50,
        p99_latency_ms=100,
        error_rate=0.0,
    )
    result = score_fairness(
        incident=incident,
        background=background,
        incident_latency_threshold_ms=250,
        incident_error_rate_threshold=0.01,
    )
    assert not result.is_green
    assert any("background_did_not_degrade_first" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# Fairness sourced from Settings (issue #350 BP review HIGH #2 — real
# runtime consumer for admission_latency_threshold_ms/error_rate_threshold)
# ---------------------------------------------------------------------------


def _healthy_incident_observation() -> LaneObservation:
    return LaneObservation(
        lane="incident",
        requests_sent=100,
        requests_admitted=99,
        p99_latency_ms=120,
        error_rate=0.0,
    )


def _degrading_background_observation() -> LaneObservation:
    return LaneObservation(
        lane="background",
        requests_sent=1000,
        requests_admitted=400,
        p99_latency_ms=800,
        error_rate=0.1,
    )


def test_score_fairness_from_settings_reads_thresholds_from_settings() -> None:
    """The Settings-derived latency/error thresholds must actually drive
    the pass/fail decision — not just be threaded through unused."""
    settings = Settings(**_FULLY_EVIDENCED_ADMISSION_SETTINGS_KWARGS)
    result = score_fairness_from_settings(
        settings,
        incident=_healthy_incident_observation(),
        background=_degrading_background_observation(),
    )
    assert result.is_green


def test_score_fairness_from_settings_degrades_when_incident_breaches_settings_latency() -> None:
    """Lowering Settings.admission_latency_threshold_ms below the observed
    incident p99 must flip the same observation from GREEN to RED —
    proving the field is read, not decorative."""
    kwargs = dict(_FULLY_EVIDENCED_ADMISSION_SETTINGS_KWARGS)
    kwargs["admission_latency_threshold_ms"] = 50.0  # incident observed at 120ms
    settings = Settings(**kwargs)
    result = score_fairness_from_settings(
        settings,
        incident=_healthy_incident_observation(),
        background=_degrading_background_observation(),
    )
    assert not result.is_green
    assert any("incident_latency_breached" in r for r in result.reasons)


def test_score_fairness_from_settings_degrades_when_error_rate_threshold_unset() -> None:
    """admission_backend='disabled' leaves the thresholds unset (None) — a
    decision-return RED, never a silently-passing scoring call."""
    settings = Settings()
    result = score_fairness_from_settings(
        settings,
        incident=_healthy_incident_observation(),
        background=_degrading_background_observation(),
    )
    assert not result.is_green
    assert any("threshold_unset" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# Queue depth (issue #350 BP review HIGH #2 — real runtime consumer for
# admission_queue_depth_max)
# ---------------------------------------------------------------------------


def test_score_queue_depth_within_bound_is_green() -> None:
    result = score_queue_depth(observed_queue_depth=200, queue_depth_max=500)
    assert result.is_green


def test_score_queue_depth_exceeded_is_red() -> None:
    result = score_queue_depth(observed_queue_depth=600, queue_depth_max=500)
    assert not result.is_green
    assert any("queue_depth_exceeded" in r for r in result.reasons)


def test_score_queue_depth_from_settings_reads_bound_from_settings() -> None:
    """Raising the observed depth past Settings.admission_queue_depth_max
    must flip GREEN to RED — proving the field is read, not decorative."""
    settings = Settings(**_FULLY_EVIDENCED_ADMISSION_SETTINGS_KWARGS)
    assert score_queue_depth_from_settings(settings, observed_queue_depth=100).is_green

    result = score_queue_depth_from_settings(settings, observed_queue_depth=999)
    assert not result.is_green
    assert any("queue_depth_exceeded" in r for r in result.reasons)


def test_score_queue_depth_from_settings_degrades_when_bound_unset() -> None:
    settings = Settings()
    result = score_queue_depth_from_settings(settings, observed_queue_depth=10)
    assert not result.is_green
    assert result.reasons == ("admission_queue_depth_max_unset",)


# ---------------------------------------------------------------------------
# Fallback (stable 429/503, no false-freshness success — AC-SCALE-3)
# ---------------------------------------------------------------------------


def test_fallback_stable_errors_only_is_green() -> None:
    observations = [
        OverloadResponseObservation(
            status_code=429,
            code="rate_limited",
            is_success_status=False,
            meta_freshness_status=None,
        ),
        OverloadResponseObservation(
            status_code=503,
            code="admission_backend_unavailable",
            is_success_status=False,
            meta_freshness_status="unknown",
        ),
    ]
    result = score_fallback(observations=observations)
    assert result.is_green


def test_fallback_success_status_during_overload_is_red() -> None:
    observations = [
        OverloadResponseObservation(
            status_code=200, code="ok", is_success_status=True, meta_freshness_status="fresh"
        )
    ]
    result = score_fallback(observations=observations)
    assert not result.is_green
    assert any("overload_returned_success_status" in r for r in result.reasons)


def test_fallback_unstable_status_code_is_red() -> None:
    observations = [
        OverloadResponseObservation(
            status_code=500, code="internal", is_success_status=False, meta_freshness_status=None
        )
    ]
    result = score_fallback(observations=observations)
    assert not result.is_green
    assert any("overload_status_not_stable" in r for r in result.reasons)


def test_fallback_false_freshness_claim_is_red() -> None:
    observations = [
        OverloadResponseObservation(
            status_code=503,
            code="admission_backend_unavailable",
            is_success_status=False,
            meta_freshness_status="fresh",
        )
    ]
    result = score_fallback(observations=observations)
    assert not result.is_green
    assert any("overload_claims_fresh_meta" in r for r in result.reasons)


def test_fallback_no_observations_is_red() -> None:
    result = score_fallback(observations=[])
    assert not result.is_green


# ---------------------------------------------------------------------------
# Content-addressed report
# ---------------------------------------------------------------------------


def _all_green_probes():
    skew = score_skew(replica_admitted_counts={"a": 100, "b": 100}, max_skew_ratio=0.1)
    isolation = score_isolation(
        observations=[
            TenantObservation("noisy", 1000, 100),
            TenantObservation("quiet", 100, 99),
        ],
        noisy_tenant_id="noisy",
        max_cross_tenant_rejection_rate=0.05,
    )
    fairness = score_fairness(
        incident=LaneObservation("incident", 100, 99, 100, 0.0),
        background=LaneObservation("background", 1000, 400, 800, 0.1),
        incident_latency_threshold_ms=250,
        incident_error_rate_threshold=0.01,
    )
    fallback = score_fallback(
        observations=[
            OverloadResponseObservation(429, "rate_limited", False, None),
        ]
    )
    return skew, isolation, fairness, fallback


def test_qualification_report_two_replicas_all_green_is_green() -> None:
    skew, isolation, fairness, fallback = _all_green_probes()
    report = build_qualification_report(
        replica_count=2, skew=skew, isolation=isolation, fairness=fairness, fallback=fallback
    )
    assert report.status == "GREEN"
    assert report.report_id.startswith("sha256:")
    assert report.reasons == ()


def test_qualification_report_single_replica_is_red_even_if_probes_green() -> None:
    skew, isolation, fairness, fallback = _all_green_probes()
    report = build_qualification_report(
        replica_count=1, skew=skew, isolation=isolation, fairness=fairness, fallback=fallback
    )
    assert report.status == "RED"
    assert any("replica_count_below_minimum" in r for r in report.reasons)


def test_qualification_report_is_content_addressed_and_deterministic() -> None:
    skew, isolation, fairness, fallback = _all_green_probes()
    report_a = build_qualification_report(
        replica_count=2, skew=skew, isolation=isolation, fairness=fairness, fallback=fallback
    )
    report_b = build_qualification_report(
        replica_count=2, skew=skew, isolation=isolation, fairness=fairness, fallback=fallback
    )
    assert report_a.report_id == report_b.report_id

    report_c = build_qualification_report(
        replica_count=3, skew=skew, isolation=isolation, fairness=fairness, fallback=fallback
    )
    assert report_c.report_id != report_a.report_id


def test_validate_report_json_accepts_a_valid_green_report() -> None:
    skew, isolation, fairness, fallback = _all_green_probes()
    report = build_qualification_report(
        replica_count=2, skew=skew, isolation=isolation, fairness=fairness, fallback=fallback
    )
    result = validate_report_json(report.to_dict())
    assert result.is_green


def test_validate_report_json_rejects_tampered_status_claim() -> None:
    """A report that claims GREEN overall but has a RED dimension must not
    validate — tamper-evidence, not just shape checking."""
    skew, isolation, fairness, fallback = _all_green_probes()
    report = build_qualification_report(
        replica_count=1, skew=skew, isolation=isolation, fairness=fairness, fallback=fallback
    )
    tampered = report.to_dict()
    tampered["status"] = "GREEN"  # lie about the overall status
    result = validate_report_json(tampered)
    assert not result.is_green


def test_validate_report_json_rejects_absent_report() -> None:
    result = validate_report_json(None)
    assert not result.is_green
    assert result.reasons == ("report_absent",)


# ---------------------------------------------------------------------------
# Optional queue_depth probe wired into the report (issue #350 BP review
# HIGH #2) — backward compatible: omitting it must reproduce the exact
# pre-existing four-probe report.
# ---------------------------------------------------------------------------


def test_qualification_report_without_queue_depth_leaves_it_unmeasured() -> None:
    """Backward compat: every existing caller that omits `queue_depth`
    (all four tests above) must keep getting `queue_depth_status=None`,
    with no effect on overall status."""
    skew, isolation, fairness, fallback = _all_green_probes()
    report = build_qualification_report(
        replica_count=2, skew=skew, isolation=isolation, fairness=fairness, fallback=fallback
    )
    assert report.queue_depth_status is None
    assert report.status == "GREEN"
    assert report.to_dict()["queue_depth_status"] is None


def test_qualification_report_green_queue_depth_stays_green() -> None:
    skew, isolation, fairness, fallback = _all_green_probes()
    queue_depth = score_queue_depth(observed_queue_depth=100, queue_depth_max=500)
    report = build_qualification_report(
        replica_count=2,
        skew=skew,
        isolation=isolation,
        fairness=fairness,
        fallback=fallback,
        queue_depth=queue_depth,
    )
    assert report.queue_depth_status == "GREEN"
    assert report.status == "GREEN"


def test_qualification_report_red_queue_depth_fails_the_whole_report() -> None:
    """A RED queue_depth probe must degrade the overall report even when
    every other probe is GREEN — the exceeded-bound observation is exactly
    the AC-SCALE-5 evidence category this field exists for."""
    skew, isolation, fairness, fallback = _all_green_probes()
    queue_depth = score_queue_depth(observed_queue_depth=999, queue_depth_max=500)
    report = build_qualification_report(
        replica_count=2,
        skew=skew,
        isolation=isolation,
        fairness=fairness,
        fallback=fallback,
        queue_depth=queue_depth,
    )
    assert report.queue_depth_status == "RED"
    assert report.status == "RED"
    assert any("queue_depth:queue_depth_exceeded" in r for r in report.reasons)
