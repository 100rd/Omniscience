"""Server-side ``ManagementContextBoundary`` (SPEC-MCTX REQ-MCTX-1): a structurally
malformed request payload is denied before a request object is constructed or an
evidence source is touched.
"""

from __future__ import annotations

from omniscience_core.management import ManagementContextProducer
from omniscience_server.management import ManagementContextBoundary

from tests.management.fixtures import (
    AUTHORIZATION,
    CONSUMER_PIN_MATCHING,
    MANIFEST,
    PRODUCED_AT,
    REFERENCE_NOW,
    VALID_REQUEST,
    RaisingEvidenceSource,
    StaticEvidenceSource,
)

BOUNDARY = ManagementContextBoundary(
    producer=ManagementContextProducer(contract_manifest=MANIFEST)
)


def _handle(payload, evidence_source):
    return BOUNDARY.handle(
        payload,
        authorization=AUTHORIZATION,
        consumer_pin=CONSUMER_PIN_MATCHING,
        evidence_source=evidence_source,
        pii_policy_revision="1.0.0",
        reference_now=REFERENCE_NOW,
        produced_at=PRODUCED_AT,
    )


def test_malformed_payload_is_denied_without_touching_evidence_source() -> None:
    malformed_payload = {"request_id": "req-malformed"}  # missing every other required field
    source = RaisingEvidenceSource()

    result = _handle(malformed_payload, source)

    assert not result.is_success
    assert result.denial.reason == "request_schema_invalid"
    assert source.calls == []


def test_well_formed_payload_produces_a_bundle() -> None:
    result = _handle(VALID_REQUEST.to_schema_dict(), StaticEvidenceSource())

    assert result.is_success
    assert result.bundle.request_digest == VALID_REQUEST.digest()
