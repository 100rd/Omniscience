"""AC-SP86-2: selected read/readiness surfaces enforce exact tenant, workspace,
purpose, contract-freshness, and dependency health.

This is release evidence, not new behavior: it packages the already-implemented
SP-81 ``ManagementContextProducer`` (SPEC-MCTX REQ-MCTX-1/7/8) by exercising it
against independently-minted foreign/stale/future/skewed/incomplete/
disabled-dependency inputs and asserting every adverse case fails closed
without "field donation" -- ``ManagementContextResult`` is a discriminated union
(see ``severance.py``) so a denial/fallback can never carry a populated bundle.
"""

from __future__ import annotations

from omniscience_core.management import ManagementContextProducer

from tests.management.fixtures import (
    AUTHORIZATION,
    CONSUMER_PIN_MAJOR_MISMATCH,
    CONSUMER_PIN_MANIFEST_MISMATCH,
    CONSUMER_PIN_MATCHING,
    CONSUMER_PIN_SCHEMA_MISMATCH,
    EVIDENCE_CUT_FUTURE,
    MANIFEST,
    PRODUCED_AT,
    REFERENCE_NOW,
    VALID_REQUEST,
    RaisingEvidenceSource,
    StaticEvidenceSource,
    make_request,
)

PRODUCER = ManagementContextProducer(contract_manifest=MANIFEST)


def _produce(request, *, evidence_source=None, consumer_pin=CONSUMER_PIN_MATCHING):
    return PRODUCER.produce(
        request,
        authorization=AUTHORIZATION,
        consumer_pin=consumer_pin,
        evidence_source=evidence_source or StaticEvidenceSource(),
        pii_policy_revision="1.0.0",
        reference_now=REFERENCE_NOW,
        produced_at=PRODUCED_AT,
    )


def _assert_fails_closed_without_donation(result) -> None:
    """Neither success path holds -- bundle stays absent (no partial citations/
    quality leaking out on a denial or fallback), per REQ-MCTX-1's "denied
    without retrieval" and REQ-MCTX-7/8's "no partially-favorable state"."""
    assert not result.is_success
    assert result.bundle is None
    assert (result.denial is not None) != (result.fallback is not None)


def test_positive_read_passes() -> None:
    result = _produce(VALID_REQUEST)
    assert result.is_success
    assert result.bundle is not None
    assert result.denial is None
    assert result.fallback is None


def test_foreign_workspace_denied_without_retrieval() -> None:
    request = make_request(workspace_id="dev-fixture-workspace-FOREIGN")
    result = _produce(request)
    _assert_fails_closed_without_donation(result)
    assert result.denial.reason == "workspace_missing_or_foreign"


def test_unauthorized_purpose_denied() -> None:
    request = make_request(purpose="dev-fixture-purpose-UNAUTHORIZED")
    result = _produce(request)
    _assert_fails_closed_without_donation(result)
    assert result.denial.reason == "purpose_unknown_or_unauthorized"


def test_widened_field_class_denied() -> None:
    request = make_request(requested_field_classes=("citations", "coverage"))
    result = _produce(request)
    _assert_fails_closed_without_donation(result)
    assert result.denial.reason == "field_class_widened"


def test_future_evidence_cut_denied() -> None:
    request = make_request(evidence_cut=EVIDENCE_CUT_FUTURE)
    result = _produce(request)
    _assert_fails_closed_without_donation(result)
    assert result.denial.reason == "evidence_cut_in_future"


def test_stale_evidence_requires_fallback() -> None:
    # REFERENCE_NOW - EVIDENCE_CUT is 3600s (fixtures.py); a 60s budget is well
    # within AUTHORIZATION.max_allowed_age_seconds (7200) so scope passes, but
    # the producer's own staleness check must still fail closed.
    request = make_request(max_age_seconds=60)
    result = _produce(request)
    _assert_fails_closed_without_donation(result)
    assert result.fallback.reason == "evidence_stale"


def test_disabled_dependency_reports_unhealthy_requires_fallback() -> None:
    source = StaticEvidenceSource(
        healthy=False, unhealthy_reason="dev-fixture-disabled-dependency"
    )
    result = _produce(VALID_REQUEST, evidence_source=source)
    _assert_fails_closed_without_donation(result)
    assert result.fallback.reason == "source_severed"


def test_source_loss_on_exception_requires_fallback() -> None:
    source = RaisingEvidenceSource(fail_on_health=True)
    result = _produce(VALID_REQUEST, evidence_source=source)
    _assert_fails_closed_without_donation(result)
    assert result.fallback.reason == "producer_unavailable"
    assert source.calls == ["health"]  # never reached fetch_citations/fetch_quality


def test_contract_major_skew_requires_fallback() -> None:
    result = _produce(VALID_REQUEST, consumer_pin=CONSUMER_PIN_MAJOR_MISMATCH)
    _assert_fails_closed_without_donation(result)
    assert result.fallback.reason == "contract_major_mismatch"


def test_contract_schema_skew_requires_fallback() -> None:
    result = _produce(VALID_REQUEST, consumer_pin=CONSUMER_PIN_SCHEMA_MISMATCH)
    _assert_fails_closed_without_donation(result)
    assert result.fallback.reason == "schema_digest_mismatch"


def test_contract_manifest_skew_requires_fallback() -> None:
    result = _produce(VALID_REQUEST, consumer_pin=CONSUMER_PIN_MANIFEST_MISMATCH)
    _assert_fails_closed_without_donation(result)
    assert result.fallback.reason == "producer_manifest_mismatch"


def test_incomplete_purpose_scope_denied_before_evidence_source_touched() -> None:
    """A request for an unknown purpose is denied before the evidence source is
    ever called -- proves REQ-MCTX-1's "denied without retrieval", not merely
    "denied before returning a bundle"."""
    source = RaisingEvidenceSource()
    request = make_request(purpose="dev-fixture-purpose-INCOMPLETE")
    result = _produce(request, evidence_source=source)
    _assert_fails_closed_without_donation(result)
    assert source.calls == []
