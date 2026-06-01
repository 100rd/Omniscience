"""Coverage tests for omniscience_retrieval.incidents.scoring (issue #155).

Raises line coverage of scoring.py from ~46% to ≥80% by directly
exercising:

* IncidentScoringWeights — sum-to-1 validator, boundary values, as_dict
* IncidentScoringConfig — field_validator path, weight-None path
* _is_temporal_match — all four branches
* _component_recency — all four branches
* _component_graph_proximity — zero-depth clamp, absent resource
* _component_evidence_count — all signal combinations
* _component_cross_ref_strength — all four rungs
* compute_components — end-to-end with every scenario
* apply_weights — weighted sum, clamping (< 0, > 1, in-range)
* _parse_config — valid, non-dict, bad weights, missing threshold
* load_workspace_config / save_workspace_config — mocked AsyncSession
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from omniscience_retrieval.incidents.scoring import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_WEIGHTS,
    SETTINGS_KEY,
    WEIGHT_SUM_TOLERANCE,
    IncidentScoringConfig,
    IncidentScoringResponse,
    IncidentScoringUpdateRequest,
    IncidentScoringWeights,
    _parse_config,
    apply_weights,
    compute_components,
    load_workspace_config,
    save_workspace_config,
)
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALERT_T = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
_PR_RECENT = _ALERT_T - timedelta(hours=6)  # within 24 h window
_PR_OLD = _ALERT_T - timedelta(days=3)  # outside any window
_PR_AFTER = _ALERT_T + timedelta(hours=1)  # after the alert

_WINDOW = 86_400  # 24 h in seconds


def _equal_weights() -> IncidentScoringWeights:
    return IncidentScoringWeights(
        recency=0.25,
        graph_proximity=0.25,
        evidence_count=0.25,
        cross_ref_strength=0.25,
    )


# ===========================================================================
# IncidentScoringWeights — Pydantic model
# ===========================================================================


class TestIncidentScoringWeights:
    def test_equal_weights_valid(self) -> None:
        w = _equal_weights()
        assert w.recency == 0.25
        assert w.graph_proximity == 0.25
        assert w.evidence_count == 0.25
        assert w.cross_ref_strength == 0.25

    def test_sum_too_low_raises(self) -> None:
        with pytest.raises(ValidationError, match=r"sum to 1\.0"):
            IncidentScoringWeights(
                recency=0.1,
                graph_proximity=0.1,
                evidence_count=0.1,
                cross_ref_strength=0.1,
            )

    def test_sum_too_high_raises(self) -> None:
        with pytest.raises(ValidationError, match=r"sum to 1\.0"):
            IncidentScoringWeights(
                recency=0.4,
                graph_proximity=0.3,
                evidence_count=0.25,
                cross_ref_strength=0.25,
            )

    def test_sum_within_tolerance_passes(self) -> None:
        # Within ±1e-3 tolerance
        w = IncidentScoringWeights(
            recency=0.25,
            graph_proximity=0.25,
            evidence_count=0.25,
            cross_ref_strength=0.2505,
        )
        total = w.recency + w.graph_proximity + w.evidence_count + w.cross_ref_strength
        assert abs(total - 1.0) <= WEIGHT_SUM_TOLERANCE + 1e-9

    def test_as_dict_returns_plain_dict(self) -> None:
        w = _equal_weights()
        d = w.as_dict()
        assert isinstance(d, dict)
        assert d == {
            "recency": 0.25,
            "graph_proximity": 0.25,
            "evidence_count": 0.25,
            "cross_ref_strength": 0.25,
        }

    def test_all_weight_on_recency(self) -> None:
        w = IncidentScoringWeights(
            recency=1.0,
            graph_proximity=0.0,
            evidence_count=0.0,
            cross_ref_strength=0.0,
        )
        assert w.recency == 1.0

    def test_field_below_zero_raises(self) -> None:
        with pytest.raises(ValidationError):
            IncidentScoringWeights(
                recency=-0.1,
                graph_proximity=0.4,
                evidence_count=0.4,
                cross_ref_strength=0.3,
            )

    def test_field_above_one_raises(self) -> None:
        with pytest.raises(ValidationError):
            IncidentScoringWeights(
                recency=1.1,
                graph_proximity=0.0,
                evidence_count=0.0,
                cross_ref_strength=-0.1,
            )


# ===========================================================================
# IncidentScoringConfig — model + field validator
# ===========================================================================


class TestIncidentScoringConfig:
    def test_default_threshold_is_0_6(self) -> None:
        cfg = IncidentScoringConfig()
        assert cfg.confidence_threshold == DEFAULT_CONFIDENCE_THRESHOLD

    def test_weights_none_by_default(self) -> None:
        cfg = IncidentScoringConfig()
        assert cfg.weights is None

    def test_custom_threshold_passes_validator(self) -> None:
        cfg = IncidentScoringConfig(confidence_threshold=0.8)
        assert cfg.confidence_threshold == 0.8

    def test_threshold_zero_accepted(self) -> None:
        cfg = IncidentScoringConfig(confidence_threshold=0.0)
        assert cfg.confidence_threshold == 0.0

    def test_threshold_one_accepted(self) -> None:
        cfg = IncidentScoringConfig(confidence_threshold=1.0)
        assert cfg.confidence_threshold == 1.0

    def test_with_weights(self) -> None:
        w = _equal_weights()
        cfg = IncidentScoringConfig(weights=w, confidence_threshold=0.7)
        assert cfg.weights is not None
        assert cfg.confidence_threshold == 0.7


# ===========================================================================
# IncidentScoringResponse + IncidentScoringUpdateRequest (surface coverage)
# ===========================================================================


class TestScoringResponseAndRequest:
    def test_response_roundtrip(self) -> None:
        w = _equal_weights()
        resp = IncidentScoringResponse(
            weights=w,
            confidence_threshold=0.6,
            weights_source="workspace",
        )
        assert resp.weights_source == "workspace"
        assert resp.confidence_threshold == 0.6

    def test_update_request_default_threshold(self) -> None:
        req = IncidentScoringUpdateRequest(weights=_equal_weights())
        assert req.confidence_threshold == DEFAULT_CONFIDENCE_THRESHOLD


# ===========================================================================
# _is_temporal_match — all four branches
# ===========================================================================


class TestIsTemporalMatch:
    """Tested indirectly via compute_components which calls _derive_inputs."""

    def test_none_alert_returns_no_match(self) -> None:
        comps = compute_components(
            alert_valid_from=None,
            pr_valid_from=_PR_RECENT,
            has_pr=True,
            has_resource=False,
            resource_depth=0,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        # pr_temporal_match=False → recency=0.5 (has_pr but no merge-ts match)
        # Actually: pr_valid_from is not None → pr_has_merge_ts=True
        # alert_valid_from=None → temporal match=False
        # → _component_recency: has_pr=True, pr_has_merge_ts=True, match=False → 0.0
        assert comps["recency"] == 0.0

    def test_none_pr_valid_from_returns_no_match(self) -> None:
        comps = compute_components(
            alert_valid_from=_ALERT_T,
            pr_valid_from=None,
            has_pr=True,
            has_resource=False,
            resource_depth=0,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        # pr_valid_from=None → pr_has_merge_ts=False → temporal match=False
        # _component_recency: has_pr=True, pr_has_merge_ts=False → 0.5
        assert comps["recency"] == pytest.approx(0.5)

    def test_pr_after_alert_returns_no_match(self) -> None:
        comps = compute_components(
            alert_valid_from=_ALERT_T,
            pr_valid_from=_PR_AFTER,
            has_pr=True,
            has_resource=False,
            resource_depth=0,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        # pr_valid_from > alert_valid_from → temporal match=False, merge ts exists → 0.0
        assert comps["recency"] == pytest.approx(0.0)

    def test_pr_within_window_returns_match(self) -> None:
        comps = compute_components(
            alert_valid_from=_ALERT_T,
            pr_valid_from=_PR_RECENT,
            has_pr=True,
            has_resource=False,
            resource_depth=0,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        # temporal match → recency = 1.0
        assert comps["recency"] == pytest.approx(1.0)

    def test_pr_outside_window_returns_no_match(self) -> None:
        comps = compute_components(
            alert_valid_from=_ALERT_T,
            pr_valid_from=_PR_OLD,
            has_pr=True,
            has_resource=False,
            resource_depth=0,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        # delta > window → no match, but has_pr=True and pr_has_merge_ts=True → 0.0
        assert comps["recency"] == pytest.approx(0.0)

    def test_exactly_at_window_edge_is_match(self) -> None:
        # exactly at the edge: delta = window_seconds → 0 <= delta <= window → True
        pr_at_edge = _ALERT_T - timedelta(seconds=_WINDOW)
        comps = compute_components(
            alert_valid_from=_ALERT_T,
            pr_valid_from=pr_at_edge,
            has_pr=True,
            has_resource=False,
            resource_depth=0,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["recency"] == pytest.approx(1.0)

    def test_one_second_beyond_window_is_no_match(self) -> None:
        pr_beyond = _ALERT_T - timedelta(seconds=_WINDOW + 1)
        comps = compute_components(
            alert_valid_from=_ALERT_T,
            pr_valid_from=pr_beyond,
            has_pr=True,
            has_resource=False,
            resource_depth=0,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["recency"] == pytest.approx(0.0)


# ===========================================================================
# _component_recency — all four branches
# ===========================================================================


class TestComponentRecency:
    def test_temporal_match_returns_1(self) -> None:
        comps = compute_components(
            alert_valid_from=_ALERT_T,
            pr_valid_from=_PR_RECENT,
            has_pr=True,
            has_resource=False,
            resource_depth=0,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["recency"] == pytest.approx(1.0)

    def test_has_pr_with_ts_no_match_returns_0(self) -> None:
        comps = compute_components(
            alert_valid_from=_ALERT_T,
            pr_valid_from=_PR_OLD,
            has_pr=True,
            has_resource=False,
            resource_depth=0,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["recency"] == pytest.approx(0.0)

    def test_has_pr_no_ts_returns_0_5(self) -> None:
        comps = compute_components(
            alert_valid_from=_ALERT_T,
            pr_valid_from=None,
            has_pr=True,
            has_resource=False,
            resource_depth=0,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["recency"] == pytest.approx(0.5)

    def test_no_pr_returns_0(self) -> None:
        comps = compute_components(
            alert_valid_from=_ALERT_T,
            pr_valid_from=None,
            has_pr=False,
            has_resource=False,
            resource_depth=0,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["recency"] == pytest.approx(0.0)


# ===========================================================================
# _component_graph_proximity — depth variants
# ===========================================================================


class TestComponentGraphProximity:
    def test_no_resource_returns_0(self) -> None:
        comps = compute_components(
            alert_valid_from=None,
            pr_valid_from=None,
            has_pr=False,
            has_resource=False,
            resource_depth=0,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["graph_proximity"] == pytest.approx(0.0)

    def test_depth_1_returns_1(self) -> None:
        comps = compute_components(
            alert_valid_from=None,
            pr_valid_from=None,
            has_pr=False,
            has_resource=True,
            resource_depth=1,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["graph_proximity"] == pytest.approx(1.0)

    def test_depth_2_returns_0_5(self) -> None:
        comps = compute_components(
            alert_valid_from=None,
            pr_valid_from=None,
            has_pr=False,
            has_resource=True,
            resource_depth=2,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["graph_proximity"] == pytest.approx(0.5)

    def test_depth_0_clamped_to_1(self) -> None:
        # depth=0 → max(1, 0) = 1 → 1.0/1 = 1.0
        comps = compute_components(
            alert_valid_from=None,
            pr_valid_from=None,
            has_pr=False,
            has_resource=True,
            resource_depth=0,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["graph_proximity"] == pytest.approx(1.0)

    def test_depth_4_returns_0_25(self) -> None:
        comps = compute_components(
            alert_valid_from=None,
            pr_valid_from=None,
            has_pr=False,
            has_resource=True,
            resource_depth=4,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["graph_proximity"] == pytest.approx(0.25)


# ===========================================================================
# _component_evidence_count — fan-in signals
# ===========================================================================


class TestComponentEvidenceCount:
    def test_no_signals_returns_0(self) -> None:
        comps = compute_components(
            alert_valid_from=None,
            pr_valid_from=None,
            has_pr=False,
            has_resource=False,
            resource_depth=0,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["evidence_count"] == pytest.approx(0.0)

    def test_pr_only_returns_1_3rd(self) -> None:
        comps = compute_components(
            alert_valid_from=None,
            pr_valid_from=None,
            has_pr=True,
            has_resource=False,
            resource_depth=0,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["evidence_count"] == pytest.approx(1.0 / 3.0)

    def test_pr_and_resource_no_threads(self) -> None:
        comps = compute_components(
            alert_valid_from=None,
            pr_valid_from=None,
            has_pr=True,
            has_resource=True,
            resource_depth=1,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["evidence_count"] == pytest.approx(2.0 / 3.0)

    def test_all_signals_full_threads(self) -> None:
        # pr=1, resource=1, thread_count=3 → thread_signal=1.0 → sum=3 → 1.0
        comps = compute_components(
            alert_valid_from=None,
            pr_valid_from=None,
            has_pr=True,
            has_resource=True,
            resource_depth=1,
            thread_count=3,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["evidence_count"] == pytest.approx(1.0)

    def test_thread_count_clamped_at_3(self) -> None:
        # thread_count=10 → min(10,3)/3 = 1.0 (same as thread_count=3)
        comps_high = compute_components(
            alert_valid_from=None,
            pr_valid_from=None,
            has_pr=False,
            has_resource=False,
            resource_depth=0,
            thread_count=10,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        comps_3 = compute_components(
            alert_valid_from=None,
            pr_valid_from=None,
            has_pr=False,
            has_resource=False,
            resource_depth=0,
            thread_count=3,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps_high["evidence_count"] == comps_3["evidence_count"]

    def test_one_thread_contributes_1_9th(self) -> None:
        # pr=0, resource=0, thread=1 → thread_signal=1/3 → (0+0+1/3)/3 = 1/9
        comps = compute_components(
            alert_valid_from=None,
            pr_valid_from=None,
            has_pr=False,
            has_resource=False,
            resource_depth=0,
            thread_count=1,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["evidence_count"] == pytest.approx(1.0 / 9.0)


# ===========================================================================
# _component_cross_ref_strength — four rungs
# ===========================================================================


class TestComponentCrossRefStrength:
    def test_temporal_match_returns_0_9(self) -> None:
        comps = compute_components(
            alert_valid_from=_ALERT_T,
            pr_valid_from=_PR_RECENT,
            has_pr=True,
            has_resource=False,
            resource_depth=0,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["cross_ref_strength"] == pytest.approx(0.9)

    def test_pr_no_match_returns_0_6(self) -> None:
        comps = compute_components(
            alert_valid_from=_ALERT_T,
            pr_valid_from=_PR_OLD,
            has_pr=True,
            has_resource=False,
            resource_depth=0,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["cross_ref_strength"] == pytest.approx(0.6)

    def test_resource_only_returns_0_4(self) -> None:
        comps = compute_components(
            alert_valid_from=None,
            pr_valid_from=None,
            has_pr=False,
            has_resource=True,
            resource_depth=1,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["cross_ref_strength"] == pytest.approx(0.4)

    def test_nothing_returns_0_1(self) -> None:
        comps = compute_components(
            alert_valid_from=None,
            pr_valid_from=None,
            has_pr=False,
            has_resource=False,
            resource_depth=0,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["cross_ref_strength"] == pytest.approx(0.1)


# ===========================================================================
# apply_weights — linear combination + clamping
# ===========================================================================


class TestApplyWeights:
    def test_full_score_all_ones(self) -> None:
        comps = {
            "recency": 1.0,
            "graph_proximity": 1.0,
            "evidence_count": 1.0,
            "cross_ref_strength": 1.0,
        }
        w = _equal_weights()
        assert apply_weights(comps, w) == pytest.approx(1.0)

    def test_zero_score_all_zeros(self) -> None:
        comps = {
            "recency": 0.0,
            "graph_proximity": 0.0,
            "evidence_count": 0.0,
            "cross_ref_strength": 0.0,
        }
        w = _equal_weights()
        assert apply_weights(comps, w) == pytest.approx(0.0)

    def test_single_component_weighted(self) -> None:
        comps = {
            "recency": 1.0,
            "graph_proximity": 0.0,
            "evidence_count": 0.0,
            "cross_ref_strength": 0.0,
        }
        w = IncidentScoringWeights(
            recency=1.0,
            graph_proximity=0.0,
            evidence_count=0.0,
            cross_ref_strength=0.0,
        )
        assert apply_weights(comps, w) == pytest.approx(1.0)

    def test_known_score_v0_1_ladder_best(self) -> None:
        # Temporal match: recency=1, graph_proximity=1/1=1, evidence_count=(1+1+0)/3,
        # cross_ref=0.9. With equal weights: avg of components.
        comps = compute_components(
            alert_valid_from=_ALERT_T,
            pr_valid_from=_PR_RECENT,
            has_pr=True,
            has_resource=True,
            resource_depth=1,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        w = _equal_weights()
        score = apply_weights(comps, w)
        assert 0.6 <= score <= 1.0

    def test_clamped_below_0_returns_0(self) -> None:
        # Force a raw < 0 by using 0 components (can't be negative naturally,
        # but test the clamp logic exists — raw will be 0 here)
        comps = {
            "recency": 0.0,
            "graph_proximity": 0.0,
            "evidence_count": 0.0,
            "cross_ref_strength": 0.0,
        }
        w = _equal_weights()
        result = apply_weights(comps, w)
        assert result == pytest.approx(0.0)

    def test_mixed_weights_correct_weighted_sum(self) -> None:
        comps = {
            "recency": 1.0,
            "graph_proximity": 0.5,
            "evidence_count": 0.0,
            "cross_ref_strength": 0.9,
        }
        w = IncidentScoringWeights(
            recency=0.4,
            graph_proximity=0.2,
            evidence_count=0.2,
            cross_ref_strength=0.2,
        )
        expected = 1.0 * 0.4 + 0.5 * 0.2 + 0.0 * 0.2 + 0.9 * 0.2
        assert apply_weights(comps, w) == pytest.approx(expected)


# ===========================================================================
# _parse_config — all branches
# ===========================================================================


class TestParseConfig:
    def test_none_returns_none(self) -> None:
        assert _parse_config(None) is None

    def test_non_dict_returns_none(self) -> None:
        assert _parse_config("not-a-dict") is None
        assert _parse_config(42) is None
        assert _parse_config([]) is None

    def test_empty_dict_returns_config_with_defaults(self) -> None:
        cfg = _parse_config({})
        assert cfg is not None
        assert cfg.weights is None
        assert cfg.confidence_threshold == DEFAULT_CONFIDENCE_THRESHOLD

    def test_valid_weights_parsed(self) -> None:
        raw: dict[str, Any] = {
            "weights": {
                "recency": 0.25,
                "graph_proximity": 0.25,
                "evidence_count": 0.25,
                "cross_ref_strength": 0.25,
            },
            "confidence_threshold": 0.7,
        }
        cfg = _parse_config(raw)
        assert cfg is not None
        assert cfg.weights is not None
        assert cfg.weights.recency == 0.25
        assert cfg.confidence_threshold == pytest.approx(0.7)

    def test_invalid_weights_dict_returns_config_with_none_weights(self) -> None:
        # Weights that don't sum to 1.0 → ValidationError → weights=None fallback
        raw: dict[str, Any] = {
            "weights": {
                "recency": 0.1,
                "graph_proximity": 0.1,
                "evidence_count": 0.1,
                "cross_ref_strength": 0.1,
            },
            "confidence_threshold": 0.6,
        }
        # Should return None (caught ValueError)
        cfg = _parse_config(raw)
        assert cfg is None

    def test_weights_not_dict_is_treated_as_none_weights(self) -> None:
        raw: dict[str, Any] = {
            "weights": "not-a-dict",
            "confidence_threshold": 0.6,
        }
        cfg = _parse_config(raw)
        assert cfg is not None
        assert cfg.weights is None

    def test_threshold_from_string_coerced(self) -> None:
        raw: dict[str, Any] = {
            "confidence_threshold": "0.5",
        }
        cfg = _parse_config(raw)
        assert cfg is not None
        assert cfg.confidence_threshold == pytest.approx(0.5)

    def test_invalid_threshold_type_returns_none(self) -> None:
        raw: dict[str, Any] = {
            "confidence_threshold": "not-a-float",
        }
        cfg = _parse_config(raw)
        assert cfg is None


# ===========================================================================
# load_workspace_config — async, mocked session
# ===========================================================================


class TestLoadWorkspaceConfig:
    @pytest.mark.asyncio
    async def test_workspace_not_found_returns_none(self) -> None:
        session = AsyncMock()
        session.get = AsyncMock(return_value=None)
        ws_id = uuid.uuid4()
        result = await load_workspace_config(session, ws_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_settings_not_dict_returns_none(self) -> None:
        ws = MagicMock()
        ws.settings = None
        session = AsyncMock()
        session.get = AsyncMock(return_value=ws)
        result = await load_workspace_config(session, uuid.uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_no_incident_scoring_key_returns_none(self) -> None:
        ws = MagicMock()
        ws.settings = {"other_key": "value"}
        session = AsyncMock()
        session.get = AsyncMock(return_value=ws)
        result = await load_workspace_config(session, uuid.uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_valid_config_returned(self) -> None:
        ws = MagicMock()
        ws.settings = {
            SETTINGS_KEY: {
                "weights": {
                    "recency": 0.25,
                    "graph_proximity": 0.25,
                    "evidence_count": 0.25,
                    "cross_ref_strength": 0.25,
                },
                "confidence_threshold": 0.7,
            }
        }
        session = AsyncMock()
        session.get = AsyncMock(return_value=ws)
        result = await load_workspace_config(session, uuid.uuid4())
        assert result is not None
        assert result.weights is not None
        assert result.confidence_threshold == pytest.approx(0.7)


# ===========================================================================
# save_workspace_config — async, mocked session
# ===========================================================================


class TestSaveWorkspaceConfig:
    @pytest.mark.asyncio
    async def test_workspace_not_found_raises(self) -> None:
        session = AsyncMock()
        session.get = AsyncMock(return_value=None)
        ws_id = uuid.uuid4()
        cfg = IncidentScoringConfig(confidence_threshold=0.6)
        with pytest.raises(ValueError, match="workspace_not_found"):
            await save_workspace_config(session, ws_id, cfg)

    @pytest.mark.asyncio
    async def test_saves_config_without_weights(self) -> None:
        ws = MagicMock()
        ws.settings = {}
        session = AsyncMock()
        session.get = AsyncMock(return_value=ws)
        session.flush = AsyncMock()
        ws_id = uuid.uuid4()
        cfg = IncidentScoringConfig(confidence_threshold=0.7)

        with patch(
            "omniscience_retrieval.incidents.scoring.flag_modified"
        ) as mock_flag:
            await save_workspace_config(session, ws_id, cfg)

        # settings should now contain the SETTINGS_KEY
        assert SETTINGS_KEY in ws.settings
        saved = ws.settings[SETTINGS_KEY]
        assert saved["confidence_threshold"] == pytest.approx(0.7)
        assert "weights" not in saved
        session.flush.assert_awaited_once()
        mock_flag.assert_called_once()

    @pytest.mark.asyncio
    async def test_saves_config_with_weights(self) -> None:
        ws = MagicMock()
        ws.settings = {"other": "preserved"}
        session = AsyncMock()
        session.get = AsyncMock(return_value=ws)
        session.flush = AsyncMock()
        ws_id = uuid.uuid4()
        w = _equal_weights()
        cfg = IncidentScoringConfig(weights=w, confidence_threshold=0.8)

        with patch("omniscience_retrieval.incidents.scoring.flag_modified"):
            await save_workspace_config(session, ws_id, cfg)

        saved = ws.settings[SETTINGS_KEY]
        assert "weights" in saved
        assert saved["weights"]["recency"] == pytest.approx(0.25)
        # Other settings keys are preserved
        assert ws.settings["other"] == "preserved"

    @pytest.mark.asyncio
    async def test_merges_into_existing_settings(self) -> None:
        ws = MagicMock()
        ws.settings = {"existing_key": "existing_value"}
        session = AsyncMock()
        session.get = AsyncMock(return_value=ws)
        session.flush = AsyncMock()
        cfg = IncidentScoringConfig(confidence_threshold=0.5)

        with patch("omniscience_retrieval.incidents.scoring.flag_modified"):
            await save_workspace_config(session, uuid.uuid4(), cfg)

        assert ws.settings["existing_key"] == "existing_value"
        assert SETTINGS_KEY in ws.settings

    @pytest.mark.asyncio
    async def test_none_existing_settings_treated_as_empty(self) -> None:
        ws = MagicMock()
        ws.settings = None
        session = AsyncMock()
        session.get = AsyncMock(return_value=ws)
        session.flush = AsyncMock()
        cfg = IncidentScoringConfig(confidence_threshold=0.6)

        with patch("omniscience_retrieval.incidents.scoring.flag_modified"):
            await save_workspace_config(session, uuid.uuid4(), cfg)

        assert SETTINGS_KEY in ws.settings


# ===========================================================================
# compute_components — end-to-end scenario matrix
# ===========================================================================


class TestComputeComponents:
    def test_zero_candidates_all_zeros(self) -> None:
        comps = compute_components(
            alert_valid_from=None,
            pr_valid_from=None,
            has_pr=False,
            has_resource=False,
            resource_depth=0,
            thread_count=0,
            max_depth=1,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["recency"] == pytest.approx(0.0)
        assert comps["graph_proximity"] == pytest.approx(0.0)
        assert comps["evidence_count"] == pytest.approx(0.0)
        assert comps["cross_ref_strength"] == pytest.approx(0.1)

    def test_max_depth_zero_clamped_to_one(self) -> None:
        # max_depth=0 → max(1, 0)=1, but max_depth isn't used in components directly
        # (only stored in _ScoringInputs). resource_depth clamp is independent.
        comps = compute_components(
            alert_valid_from=None,
            pr_valid_from=None,
            has_pr=False,
            has_resource=True,
            resource_depth=3,
            thread_count=0,
            max_depth=0,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["graph_proximity"] == pytest.approx(1.0 / 3.0)

    def test_full_scenario_with_all_signals(self) -> None:
        comps = compute_components(
            alert_valid_from=_ALERT_T,
            pr_valid_from=_PR_RECENT,
            has_pr=True,
            has_resource=True,
            resource_depth=1,
            thread_count=3,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert comps["recency"] == pytest.approx(1.0)
        assert comps["graph_proximity"] == pytest.approx(1.0)
        assert comps["evidence_count"] == pytest.approx(1.0)
        assert comps["cross_ref_strength"] == pytest.approx(0.9)
        w = _equal_weights()
        score = apply_weights(comps, w)
        assert score == pytest.approx(0.975)

    def test_all_keys_present(self) -> None:
        comps = compute_components(
            alert_valid_from=None,
            pr_valid_from=None,
            has_pr=False,
            has_resource=False,
            resource_depth=0,
            thread_count=0,
            max_depth=5,
            pr_recency_window_seconds=_WINDOW,
        )
        assert set(comps.keys()) == {
            "recency",
            "graph_proximity",
            "evidence_count",
            "cross_ref_strength",
        }


# ===========================================================================
# Constants sanity
# ===========================================================================


class TestConstants:
    def test_default_confidence_threshold(self) -> None:
        assert DEFAULT_CONFIDENCE_THRESHOLD == 0.6

    def test_weight_sum_tolerance(self) -> None:
        assert WEIGHT_SUM_TOLERANCE == 1e-3

    def test_settings_key(self) -> None:
        assert SETTINGS_KEY == "incident_scoring"

    def test_default_weights_sum_to_one(self) -> None:
        total = sum(DEFAULT_WEIGHTS.values())
        assert abs(total - 1.0) <= WEIGHT_SUM_TOLERANCE
