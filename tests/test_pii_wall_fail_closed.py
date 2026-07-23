"""AC-SP61-2 (P-PII-7+8): policy skew, detector failure, quarantine failure all park.

Every failure path must resolve to PARK with a non-identifying receipt -- never to an
unsafe admit fallback -- and the protected sink must never be reached.
"""

from __future__ import annotations

import pytest
from omniscience_core.privacy import (
    FailingQuarantineStore,
    GateDisposition,
    IngestPIIGate,
    InMemoryQuarantineStore,
    scan_receipt_for_raw_leakage,
)
from omniscience_server.privacy import Pw0Boundary, Pw0BoundaryDeniedError

from tests.privacy.fixtures import (
    CONSUMER_PIN_MATCHING,
    CONSUMER_PIN_SCHEMA_MAJOR_MISMATCH,
    CONSUMER_PIN_SIGNER_MISMATCH,
    REFERENCE_NOW_AFTER_EXPIRY,
    REFERENCE_NOW_VALID,
    SEEDED_ENVELOPES,
    RaisingDetector,
    RecordingSink,
    StaticDetector,
    policy_bundle_fixture,
)


def _assert_parked_and_unreachable(decision, envelope, expected_reason: str) -> None:
    assert decision.disposition == GateDisposition.PARK
    assert decision.reason == expected_reason
    assert scan_receipt_for_raw_leakage(decision.receipt.to_dict()) == []

    boundary = Pw0Boundary()
    sink = RecordingSink()
    with pytest.raises(Pw0BoundaryDeniedError):
        boundary.guard_store(decision, sink, envelope)
    assert sink.calls == []


def test_signer_trust_skew_parks_before_classification() -> None:
    envelope = SEEDED_ENVELOPES["public-raw"]
    gate = IngestPIIGate(
        quarantine_store=InMemoryQuarantineStore(), consumer_pin=CONSUMER_PIN_SIGNER_MISMATCH
    )
    decision = gate.evaluate(
        envelope=envelope,
        policy_bundle=policy_bundle_fixture(),
        detector=StaticDetector(data_class="public", transform_state="raw"),
        reference_now=REFERENCE_NOW_VALID,
        receipt_id="receipt-signer-skew",
        produced_at=REFERENCE_NOW_VALID,
    )
    _assert_parked_and_unreachable(decision, envelope, "policy_skew")


def test_schema_major_and_stale_revision_and_expiry_skew_parks() -> None:
    envelope = SEEDED_ENVELOPES["public-raw"]
    gate = IngestPIIGate(
        quarantine_store=InMemoryQuarantineStore(), consumer_pin=CONSUMER_PIN_SCHEMA_MAJOR_MISMATCH
    )
    decision = gate.evaluate(
        envelope=envelope,
        policy_bundle=policy_bundle_fixture(),
        detector=StaticDetector(data_class="public", transform_state="raw"),
        reference_now=REFERENCE_NOW_AFTER_EXPIRY,
        receipt_id="receipt-multi-skew",
        produced_at=REFERENCE_NOW_VALID,
    )
    _assert_parked_and_unreachable(decision, envelope, "policy_skew")
    assert len(decision.receipt.detail) >= 3  # schema_major + revision + expiry all flagged


def test_detector_failure_parks_instead_of_admitting() -> None:
    envelope = SEEDED_ENVELOPES["public-raw"]
    gate = IngestPIIGate(
        quarantine_store=InMemoryQuarantineStore(), consumer_pin=CONSUMER_PIN_MATCHING
    )
    decision = gate.evaluate(
        envelope=envelope,
        policy_bundle=policy_bundle_fixture(),
        detector=RaisingDetector(),
        reference_now=REFERENCE_NOW_VALID,
        receipt_id="receipt-detector-failure",
        produced_at=REFERENCE_NOW_VALID,
    )
    _assert_parked_and_unreachable(decision, envelope, "detector_failure")


def test_quarantine_outage_parks_instead_of_falling_back_to_admit() -> None:
    envelope = SEEDED_ENVELOPES["sensitive-raw"]
    gate = IngestPIIGate(
        quarantine_store=FailingQuarantineStore(), consumer_pin=CONSUMER_PIN_MATCHING
    )
    decision = gate.evaluate(
        envelope=envelope,
        policy_bundle=policy_bundle_fixture(),
        detector=StaticDetector(data_class="sensitive_personal", transform_state="raw"),
        reference_now=REFERENCE_NOW_VALID,
        receipt_id="receipt-quarantine-outage",
        produced_at=REFERENCE_NOW_VALID,
    )
    _assert_parked_and_unreachable(decision, envelope, "quarantine_unavailable")
    assert decision.quarantine_id is None
