"""AC-SP81-2 (P-MCTX-3+5): authority fields, active content, credentials and seeded
PII fail before response -- independent conformance runner over a seeded corpus.
"""

from __future__ import annotations

import pytest
from omniscience_core.management import ManagementContextProducer, conformance_scan, scan_text

from tests.management.fixtures import (
    ACTIVE_CONTENT_CITATION,
    AUTHORITY_LEAK_QUALITY,
    AUTHORIZATION,
    CONSUMER_PIN_MATCHING,
    CREDENTIAL_CITATION,
    MANIFEST,
    PII_RAW_CITATION,
    PRODUCED_AT,
    REFERENCE_NOW,
    SAFE_CITATION,
    StaticEvidenceSource,
    make_request,
)

PRODUCER = ManagementContextProducer(contract_manifest=MANIFEST)


def _produce(evidence_source):
    return PRODUCER.produce(
        make_request(),
        authorization=AUTHORIZATION,
        consumer_pin=CONSUMER_PIN_MATCHING,
        evidence_source=evidence_source,
        pii_policy_revision="1.0.0",
        reference_now=REFERENCE_NOW,
        produced_at=PRODUCED_AT,
    )


SEEDED_MALICIOUS_CITATIONS = (
    PII_RAW_CITATION,
    ACTIVE_CONTENT_CITATION,
    CREDENTIAL_CITATION,
)


@pytest.mark.parametrize("malicious_citation", SEEDED_MALICIOUS_CITATIONS)
def test_seeded_malicious_citation_never_reaches_the_bundle(malicious_citation) -> None:
    result = _produce(StaticEvidenceSource(citations=(SAFE_CITATION, malicious_citation)))

    assert result.is_success
    citation_ids = {citation.citation_id for citation in result.bundle.citations}
    assert malicious_citation.citation_id not in citation_ids
    assert any(malicious_citation.field_class in gap for gap in result.bundle.coverage_gaps)
    assert conformance_scan(result.bundle.to_schema_dict()) == []


def test_independent_conformance_runner_replays_full_seeded_corpus() -> None:
    """Replays every seeded attack citation together (not just one at a time) and
    independently rescans the resulting bundle -- the same scanner the producer uses
    internally, invoked here as an external runner over the whole corpus."""
    result = _produce(StaticEvidenceSource(citations=(SAFE_CITATION, *SEEDED_MALICIOUS_CITATIONS)))

    assert result.is_success
    surviving_ids = {citation.citation_id for citation in result.bundle.citations}
    assert surviving_ids == {SAFE_CITATION.citation_id}
    assert len(result.bundle.coverage_gaps) == len(SEEDED_MALICIOUS_CITATIONS)
    assert conformance_scan(result.bundle.to_schema_dict()) == []


def test_authority_field_leak_forces_fallback_never_a_bundle() -> None:
    result = _produce(
        StaticEvidenceSource(citations=(SAFE_CITATION,), quality=AUTHORITY_LEAK_QUALITY)
    )

    assert not result.is_success
    assert result.bundle is None
    assert result.fallback.reason == "authority_field_detected"
    assert any("risk" in detail for detail in result.fallback.detail)


def test_scanner_catches_each_seeded_pattern_directly() -> None:
    assert scan_text(PII_RAW_CITATION.text) != []
    assert scan_text(ACTIVE_CONTENT_CITATION.text) != []
    assert scan_text(CREDENTIAL_CITATION.text) != []
    assert scan_text(SAFE_CITATION.text) == []
