"""AC-SP61-1 (P-PII-1+2+3): PW0 blocks/sanitizes before every protected sink.

Seeded personal, sensitive, prohibited, and unknown fixtures must never reach a
protected sink (ordinary store, parse, chunk, embed, projection). Only public,
internal_non_personal, or deterministically redacted content is admitted.
"""

from __future__ import annotations

import pytest
from omniscience_core.privacy import GateDisposition, IngestPIIGate, InMemoryQuarantineStore
from omniscience_server.privacy import Pw0Boundary, Pw0BoundaryDeniedError

from tests.privacy.fixtures import (
    CONSUMER_PIN_MATCHING,
    REFERENCE_NOW_VALID,
    SEEDED_ENVELOPES,
    RecordingSink,
    StaticDetector,
    policy_bundle_fixture,
)

NEVER_ADMITTED_SEEDS = (
    "personal-raw",
    "sensitive-raw",
    "prohibited-raw",
    "prohibited-redacted",
    "unknown",
)
ALWAYS_ADMITTED_SEEDS = ("public-raw", "internal-raw", "personal-redacted")


def _detector_for(seed_name: str) -> StaticDetector:
    envelope = SEEDED_ENVELOPES[seed_name]
    return StaticDetector(data_class=envelope.data_class, transform_state=envelope.transform_state)


def _make_gate() -> IngestPIIGate:
    return IngestPIIGate(
        quarantine_store=InMemoryQuarantineStore(), consumer_pin=CONSUMER_PIN_MATCHING
    )


@pytest.mark.parametrize("seed_name", NEVER_ADMITTED_SEEDS)
def test_seeded_protected_fixtures_never_reach_any_sink(seed_name: str) -> None:
    envelope = SEEDED_ENVELOPES[seed_name]
    gate = _make_gate()
    decision = gate.evaluate(
        envelope=envelope,
        policy_bundle=policy_bundle_fixture(),
        detector=_detector_for(seed_name),
        reference_now=REFERENCE_NOW_VALID,
        receipt_id=f"receipt-{seed_name}",
        produced_at=REFERENCE_NOW_VALID,
    )

    assert decision.disposition != GateDisposition.ADMIT

    boundary = Pw0Boundary()
    for guard, sink_attr in (
        (boundary.guard_store, "store"),
        (boundary.guard_parse, "parse"),
        (boundary.guard_chunk, "chunk"),
        (boundary.guard_embed, "embed"),
        (boundary.guard_project, "project"),
    ):
        sink = RecordingSink()
        with pytest.raises(Pw0BoundaryDeniedError):
            guard(decision, sink, envelope)
        assert sink.calls == [], f"{sink_attr} sink was reached by {seed_name}"


@pytest.mark.parametrize("seed_name", ALWAYS_ADMITTED_SEEDS)
def test_safe_fixtures_are_admitted_and_reach_the_sink(seed_name: str) -> None:
    envelope = SEEDED_ENVELOPES[seed_name]
    gate = _make_gate()
    decision = gate.evaluate(
        envelope=envelope,
        policy_bundle=policy_bundle_fixture(),
        detector=_detector_for(seed_name),
        reference_now=REFERENCE_NOW_VALID,
        receipt_id=f"receipt-{seed_name}",
        produced_at=REFERENCE_NOW_VALID,
    )

    assert decision.disposition == GateDisposition.ADMIT

    boundary = Pw0Boundary()
    sink = RecordingSink()
    boundary.guard_store(decision, sink, envelope)
    assert sink.calls == [envelope]


def test_unknown_class_is_quarantined_not_admitted() -> None:
    envelope = SEEDED_ENVELOPES["unknown"]
    gate = _make_gate()
    decision = gate.evaluate(
        envelope=envelope,
        policy_bundle=policy_bundle_fixture(),
        detector=StaticDetector(data_class="unknown", transform_state="raw"),
        reference_now=REFERENCE_NOW_VALID,
        receipt_id="receipt-unknown",
        produced_at=REFERENCE_NOW_VALID,
    )

    assert decision.disposition == GateDisposition.QUARANTINE
    assert decision.quarantine_id is not None
