"""AC-SP81-3 (P-MCTX-6): contract skew and severance select exact fallback-required
states instead of best-effort parsing or stale success.
"""

from __future__ import annotations

from omniscience_core.management import ManagementContextProducer, validate_fallback_result

from tests.management.fixtures import (
    AUTHORIZATION,
    CONSUMER_PIN_MAJOR_MISMATCH,
    CONSUMER_PIN_MANIFEST_MISMATCH,
    CONSUMER_PIN_MATCHING,
    CONSUMER_PIN_SCHEMA_MISMATCH,
    MANIFEST,
    PRODUCED_AT,
    REFERENCE_NOW,
    RaisingEvidenceSource,
    StaticEvidenceSource,
    make_request,
)

PRODUCER = ManagementContextProducer(contract_manifest=MANIFEST)


def _produce(
    *,
    consumer_pin=CONSUMER_PIN_MATCHING,
    evidence_source=None,
    request=None,
    max_age_seconds=3600,
):
    return PRODUCER.produce(
        request or make_request(max_age_seconds=max_age_seconds),
        authorization=AUTHORIZATION,
        consumer_pin=consumer_pin,
        evidence_source=evidence_source or StaticEvidenceSource(),
        pii_policy_revision="1.0.0",
        reference_now=REFERENCE_NOW,
        produced_at=PRODUCED_AT,
    )


def _assert_fallback(result, reason: str) -> None:
    assert not result.is_success
    assert result.bundle is None
    assert result.fallback is not None
    assert result.fallback.reason == reason
    assert validate_fallback_result(result.fallback) == []


def test_contract_major_mismatch_requires_fallback() -> None:
    result = _produce(consumer_pin=CONSUMER_PIN_MAJOR_MISMATCH)
    _assert_fallback(result, "contract_major_mismatch")


def test_schema_digest_mismatch_requires_fallback() -> None:
    result = _produce(consumer_pin=CONSUMER_PIN_SCHEMA_MISMATCH)
    _assert_fallback(result, "schema_digest_mismatch")


def test_producer_manifest_mismatch_requires_fallback() -> None:
    result = _produce(consumer_pin=CONSUMER_PIN_MANIFEST_MISMATCH)
    _assert_fallback(result, "producer_manifest_mismatch")


def test_unhealthy_source_requires_fallback_never_stale_success() -> None:
    source = StaticEvidenceSource(healthy=False, unhealthy_reason="dev-fixture-outage")
    result = _produce(evidence_source=source)
    _assert_fallback(result, "source_severed")


def test_health_check_failure_requires_fallback() -> None:
    result = _produce(evidence_source=RaisingEvidenceSource(fail_on_health=True))
    _assert_fallback(result, "producer_unavailable")


def test_fetch_failure_after_healthy_check_requires_fallback() -> None:
    result = _produce(evidence_source=RaisingEvidenceSource(fail_on_health=False))
    _assert_fallback(result, "source_severed")


def test_stale_evidence_beyond_max_age_requires_fallback() -> None:
    result = _produce(max_age_seconds=60)  # evidence_cut is 3600s before reference_now
    _assert_fallback(result, "evidence_stale")
