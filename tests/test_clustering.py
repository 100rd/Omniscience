"""Unit tests for the pure clustering primitives (issue #233).

These tests pin the deterministic behaviour of:

- :func:`compute_signature` — stable across runs / processes
- :func:`normalise_symptom` — collapses timestamps / IDs / pod suffixes
- :func:`cosine_similarity` — clipped to [0, 1], None-safe
- :func:`recency_multiplier` — exponential decay in (0, 1]
- :func:`score_similarity` — full formula at component boundaries
- :func:`rank_candidates` — exclude seed, drop below floor, sort stable
- :func:`extract_alert_type` — alert URI parsing + fallback

No I/O, no fixtures — pure-function contract tests so a future
algorithmic tweak shows up as a clear diff in the assertions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from omniscience_index.clustering import (
    DEFAULT_LIMIT,
    DEFAULT_SINCE_DAYS,
    SIGNATURE_DIGEST_BYTES,
    SIGNATURE_MATCH_BONUS,
    WEIGHT_ALERT_TYPE,
    WEIGHT_SERVICE,
    WEIGHT_SYMPTOM_COSINE,
    IncidentFeatures,
    compute_signature,
    cosine_similarity,
    extract_alert_type,
    normalise_symptom,
    rank_candidates,
    recency_multiplier,
    score_similarity,
)

_NOW = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# normalise_symptom
# ---------------------------------------------------------------------------


class TestNormaliseSymptom:
    """Symptom normalisation must collapse high-cardinality tokens."""

    def test_none_returns_empty_string(self) -> None:
        assert normalise_symptom(None) == ""

    def test_whitespace_only_returns_empty_string(self) -> None:
        assert normalise_symptom("   \n\t  ") == ""

    def test_iso_timestamps_collapse(self) -> None:
        a = normalise_symptom("CPU spike at 2026-04-12T19:30:00Z on api")
        b = normalise_symptom("CPU spike at 2026-05-22T08:15:42+00:00 on api")
        assert a == b

    def test_hex_blobs_collapse(self) -> None:
        a = normalise_symptom("trace_id=deadbeef1234 failed")
        b = normalise_symptom("trace_id=cafebabe5678 failed")
        assert a == b

    def test_long_numbers_collapse(self) -> None:
        a = normalise_symptom("error code 50012 occurred")
        b = normalise_symptom("error code 99999 occurred")
        assert a == b

    def test_three_digit_status_codes_preserved(self) -> None:
        # 503 and 500 are different signals — must NOT collapse.
        a = normalise_symptom("got 503 response")
        b = normalise_symptom("got 500 response")
        assert a != b

    def test_pod_suffix_collapses(self) -> None:
        a = normalise_symptom("pod api-7f9b3a restarted")
        b = normalise_symptom("pod api-2c4e1d restarted")
        assert a == b

    def test_lowercase_and_collapse_whitespace(self) -> None:
        assert normalise_symptom("CPU   SPIKE\n\non API") == "cpu spike on api"


# ---------------------------------------------------------------------------
# compute_signature
# ---------------------------------------------------------------------------


class TestComputeSignature:
    """The signature is the deterministic identity of a (type, service, symptom)."""

    def test_same_inputs_produce_same_signature(self) -> None:
        a = compute_signature(
            alert_type="datadog", service="service://api", symptom_text="cpu spike"
        )
        b = compute_signature(
            alert_type="datadog", service="service://api", symptom_text="cpu spike"
        )
        assert a == b

    def test_signature_digest_length(self) -> None:
        sig = compute_signature(alert_type="x", service="y", symptom_text="z")
        assert len(sig) == SIGNATURE_DIGEST_BYTES * 2  # hex doubles bytes

    def test_case_insensitive_inputs(self) -> None:
        lo = compute_signature(alert_type="datadog", service="API", symptom_text="OOM")
        up = compute_signature(alert_type="DATADOG", service="api", symptom_text="oom")
        assert lo == up

    def test_different_service_changes_signature(self) -> None:
        a = compute_signature(alert_type="datadog", service="api", symptom_text="oom")
        b = compute_signature(alert_type="datadog", service="auth", symptom_text="oom")
        assert a != b

    def test_normalised_symptom_collapses_to_same_signature(self) -> None:
        a = compute_signature(
            alert_type="dd", service="api", symptom_text="pod api-abc123 OOMKilled"
        )
        b = compute_signature(
            alert_type="dd", service="api", symptom_text="pod api-def456 OOMKilled"
        )
        assert a == b

    def test_none_symptom_is_safe(self) -> None:
        sig = compute_signature(alert_type="dd", service="api", symptom_text=None)
        assert isinstance(sig, str)
        assert len(sig) == SIGNATURE_DIGEST_BYTES * 2


# ---------------------------------------------------------------------------
# cosine_similarity
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_vectors(self) -> None:
        v = (1.0, 0.5, -0.2)
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        assert cosine_similarity((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)

    def test_opposite_vectors_clipped_to_zero(self) -> None:
        assert cosine_similarity((1.0, 0.0), (-1.0, 0.0)) == 0.0

    def test_none_returns_zero(self) -> None:
        assert cosine_similarity(None, (1.0,)) == 0.0
        assert cosine_similarity((1.0,), None) == 0.0
        assert cosine_similarity(None, None) == 0.0

    def test_length_mismatch_returns_zero(self) -> None:
        assert cosine_similarity((1.0, 0.0), (1.0,)) == 0.0

    def test_zero_norm_returns_zero(self) -> None:
        assert cosine_similarity((0.0, 0.0), (1.0, 0.0)) == 0.0


# ---------------------------------------------------------------------------
# recency_multiplier
# ---------------------------------------------------------------------------


class TestRecencyMultiplier:
    def test_now_returns_one(self) -> None:
        assert recency_multiplier(fired_at=_NOW, now=_NOW, since_days=90) == 1.0

    def test_at_window_boundary_returns_one_over_e(self) -> None:
        boundary = _NOW - timedelta(days=90)
        m = recency_multiplier(fired_at=boundary, now=_NOW, since_days=90)
        assert m == pytest.approx(1 / 2.718281828, rel=0.01)

    def test_none_fired_at_returns_one(self) -> None:
        assert recency_multiplier(fired_at=None, now=_NOW, since_days=90) == 1.0

    def test_future_fired_at_returns_one(self) -> None:
        future = _NOW + timedelta(days=1)
        assert recency_multiplier(fired_at=future, now=_NOW, since_days=90) == 1.0

    def test_decay_monotonic(self) -> None:
        m1 = recency_multiplier(fired_at=_NOW - timedelta(days=1), now=_NOW, since_days=90)
        m30 = recency_multiplier(fired_at=_NOW - timedelta(days=30), now=_NOW, since_days=90)
        m90 = recency_multiplier(fired_at=_NOW - timedelta(days=90), now=_NOW, since_days=90)
        assert 1.0 > m1 > m30 > m90 > 0.0


# ---------------------------------------------------------------------------
# extract_alert_type
# ---------------------------------------------------------------------------


class TestExtractAlertType:
    def test_pagerduty_provider(self) -> None:
        assert (
            extract_alert_type(canonical_name="alert://pagerduty/INC-123", kind="alert")
            == "pagerduty"
        )

    def test_datadog_provider(self) -> None:
        assert extract_alert_type(canonical_name="alert://datadog/9000001", kind=None) == "datadog"

    def test_non_alert_uri_falls_back_to_kind(self) -> None:
        assert (
            extract_alert_type(canonical_name="service://api", kind="datadog_event")
            == "datadog_event"
        )

    def test_no_signal_returns_unknown(self) -> None:
        assert extract_alert_type(canonical_name="service://api", kind=None) == "unknown"

    def test_case_normalised(self) -> None:
        assert (
            extract_alert_type(canonical_name="alert://PAGERDUTY/INC-123", kind=None)
            == "pagerduty"
        )


# ---------------------------------------------------------------------------
# score_similarity — full formula at component boundaries
# ---------------------------------------------------------------------------


def _features(
    *,
    incident_id: str = "alert://pagerduty/INC-1",
    alert_type: str = "pagerduty",
    service: str | None = "service://api",
    symptom_text: str | None = "CPU spike at 95%",
    embedding: tuple[float, ...] | None = (1.0, 0.0),
    fired_at: datetime | None = None,
) -> IncidentFeatures:
    return IncidentFeatures(
        incident_id=incident_id,
        alert_type=alert_type,
        service=service,
        symptom_text=symptom_text,
        symptom_embedding=embedding,
        fired_at=fired_at if fired_at is not None else _NOW,
    )


class TestScoreSimilarity:
    """Each axis is exercised in isolation; combinations are unit-checkable."""

    def test_identical_features_score_one(self) -> None:
        seed = _features(incident_id="a")
        cand = _features(incident_id="b")
        result = score_similarity(seed=seed, candidate=cand, now=_NOW)
        # Identical -> raw=1.0, decay=1.0, signature match adds bonus,
        # clamp to 1.0.
        assert result.score == 1.0
        assert result.signature_match is True
        assert result.alert_type_match == 1.0
        assert result.service_match == 1.0
        assert result.symptom_cosine == pytest.approx(1.0)

    def test_different_alert_type_only(self) -> None:
        seed = _features(incident_id="a", alert_type="pagerduty")
        cand = _features(
            incident_id="b",
            alert_type="datadog",
            embedding=(1.0, 0.0),
        )
        result = score_similarity(seed=seed, candidate=cand, now=_NOW)
        # raw = 0 * 0.4 + 1 * 0.3 + 1 * 0.3 = 0.6; decay 1.0; no signature
        # match because alert_type differs.
        assert result.alert_type_match == 0.0
        assert result.service_match == 1.0
        assert result.symptom_cosine == pytest.approx(1.0)
        assert result.signature_match is False
        assert result.score == pytest.approx(WEIGHT_SERVICE + WEIGHT_SYMPTOM_COSINE, rel=1e-6)

    def test_different_service_only(self) -> None:
        seed = _features(incident_id="a", service="service://api")
        cand = _features(incident_id="b", service="service://auth")
        result = score_similarity(seed=seed, candidate=cand, now=_NOW)
        assert result.service_match == 0.0
        assert result.alert_type_match == 1.0
        assert result.signature_match is False
        assert result.score == pytest.approx(WEIGHT_ALERT_TYPE + WEIGHT_SYMPTOM_COSINE, rel=1e-6)

    def test_no_embeddings_no_cosine_contribution(self) -> None:
        seed = _features(incident_id="a", embedding=None)
        cand = _features(incident_id="b", embedding=None)
        result = score_similarity(seed=seed, candidate=cand, now=_NOW)
        assert result.symptom_cosine == 0.0
        # raw = 0.4 + 0.3 + 0 = 0.7; decay 1; signature match -> +0.2
        # clamped at 1.0.
        assert result.signature_match is True
        assert result.score == pytest.approx(
            min(1.0, WEIGHT_ALERT_TYPE + WEIGHT_SERVICE + SIGNATURE_MATCH_BONUS),
            rel=1e-6,
        )

    def test_recency_decay_lowers_score(self) -> None:
        seed = _features(incident_id="a")
        old = _features(
            incident_id="b",
            fired_at=_NOW - timedelta(days=90),
        )
        result = score_similarity(seed=seed, candidate=old, now=_NOW, since_days=90)
        # Decay = 1/e ~= 0.368; identical structural so pre-decay = 1.0.
        # Then signature bonus +0.2; both contribute to capped final.
        assert 0.5 < result.score < 1.0


# ---------------------------------------------------------------------------
# rank_candidates — exclude seed, drop floor, stable ordering
# ---------------------------------------------------------------------------


class TestRankCandidates:
    def test_seed_excluded_from_candidates(self) -> None:
        seed = _features(incident_id="a")
        ranked = rank_candidates(
            seed=seed,
            candidates=[seed, _features(incident_id="b")],
            now=_NOW,
        )
        assert all(c.features.incident_id != "a" for c in ranked)
        assert len(ranked) == 1

    def test_below_floor_dropped(self) -> None:
        seed = _features(incident_id="a", alert_type="pagerduty", service="service://api")
        # Completely unrelated cand — different alert_type, service,
        # different embedding, very old.
        unrelated = _features(
            incident_id="b",
            alert_type="datadog",
            service="service://other",
            symptom_text="totally unrelated",
            embedding=(0.0, 1.0),
            fired_at=_NOW - timedelta(days=200),
        )
        ranked = rank_candidates(
            seed=seed,
            candidates=[unrelated],
            now=_NOW,
            since_days=90,
            min_score=0.5,
        )
        assert ranked == []

    def test_ordering_descending_by_score_then_id(self) -> None:
        seed = _features(incident_id="seed")
        a = _features(incident_id="b")  # identical -> max score
        b = _features(incident_id="a")  # identical, sorted by id tiebreak
        c = _features(incident_id="c", service="service://other")  # lower
        ranked = rank_candidates(seed=seed, candidates=[c, a, b], now=_NOW)
        ids = [r.features.incident_id for r in ranked]
        # 'a' and 'b' tied with score 1.0; tie-break by id asc => "a", "b", then "c".
        assert ids == ["a", "b", "c"]

    def test_limit_clamp(self) -> None:
        seed = _features(incident_id="seed")
        candidates = [_features(incident_id=f"i-{n}") for n in range(20)]
        ranked = rank_candidates(seed=seed, candidates=candidates, now=_NOW, limit=3)
        assert len(ranked) == 3

    def test_zero_limit_returns_empty(self) -> None:
        seed = _features(incident_id="seed")
        ranked = rank_candidates(
            seed=seed,
            candidates=[_features(incident_id="b")],
            now=_NOW,
            limit=0,
        )
        assert ranked == []


class TestDefaults:
    """Quick sanity check the public defaults are sensible bounds."""

    def test_default_limit_positive(self) -> None:
        assert DEFAULT_LIMIT >= 1

    def test_default_since_days_positive(self) -> None:
        assert DEFAULT_SINCE_DAYS >= 1
