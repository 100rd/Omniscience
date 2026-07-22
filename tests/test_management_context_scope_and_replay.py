"""AC-SP81-1 (P-MCTX-1+2+4+7): pinned fixtures reproduce exact envelopes, citations
and quality conditions; scope is denied before any retrieval.
"""

from __future__ import annotations

from omniscience_core.management import (
    ManagementContextProducer,
    validate_bundle_schema,
    validate_citation,
)

from tests.management.fixtures import (
    AUTHORIZATION,
    CONSUMER_PIN_MATCHING,
    EVIDENCE_CUT_FUTURE,
    MANIFEST,
    PARTIAL_QUALITY,
    PRODUCED_AT,
    REFERENCE_NOW,
    RaisingEvidenceSource,
    StaticEvidenceSource,
    make_request,
)

PRODUCER = ManagementContextProducer(contract_manifest=MANIFEST)


def _produce(request, evidence_source):
    return PRODUCER.produce(
        request,
        authorization=AUTHORIZATION,
        consumer_pin=CONSUMER_PIN_MATCHING,
        evidence_source=evidence_source,
        pii_policy_revision="1.0.0",
        reference_now=REFERENCE_NOW,
        produced_at=PRODUCED_AT,
    )


def test_valid_request_produces_schema_valid_bundle_with_citations() -> None:
    result = _produce(make_request(), StaticEvidenceSource())

    assert result.is_success
    assert validate_bundle_schema(result.bundle) == []
    assert len(result.bundle.citations) == 2
    for citation in result.bundle.citations:
        assert validate_citation(citation) == []
        assert citation.evidence_fitness == "known"


def test_replay_is_deterministic_across_identical_calls() -> None:
    first = _produce(make_request(), StaticEvidenceSource())
    second = _produce(make_request(), StaticEvidenceSource())

    assert first.is_success and second.is_success
    assert first.bundle.replay_digest_payload() == second.bundle.replay_digest_payload()


def test_partial_evidence_yields_unknown_axes_never_a_favorable_default() -> None:
    result = _produce(make_request(), StaticEvidenceSource(quality=PARTIAL_QUALITY))

    assert result.is_success
    quality = result.bundle.quality
    assert quality.provenance_status == "unknown"
    assert quality.conformance_status == "unknown"
    assert quality.projection_status == "unknown"
    # Axes the source did supply are preserved verbatim, not overwritten.
    assert quality.freshness_status == "stale"
    assert quality.coverage_status == "partial"


def test_foreign_workspace_is_denied_without_touching_evidence_source() -> None:
    request = make_request(workspace_id="dev-fixture-workspace-FOREIGN")
    source = RaisingEvidenceSource()

    result = _produce(request, source)

    assert not result.is_success
    assert result.denial.reason == "workspace_missing_or_foreign"
    assert source.calls == []


def test_unknown_domain_is_denied_without_retrieval() -> None:
    request = make_request(management_domain="toil")  # valid taxonomy value, not authorized
    source = RaisingEvidenceSource()

    result = _produce(request, source)

    assert not result.is_success
    assert result.denial.reason == "domain_unknown"
    assert source.calls == []


def test_widened_field_class_is_denied_without_retrieval() -> None:
    request = make_request(requested_field_classes=("citations", "synthesis"))
    source = RaisingEvidenceSource()

    result = _produce(request, source)

    assert not result.is_success
    assert result.denial.reason == "field_class_widened"
    assert source.calls == []


def test_future_evidence_cut_is_denied_without_retrieval() -> None:
    request = make_request(evidence_cut=EVIDENCE_CUT_FUTURE)
    source = RaisingEvidenceSource()

    result = _produce(request, source)

    assert not result.is_success
    assert result.denial.reason == "evidence_cut_in_future"
    assert source.calls == []


def test_invalid_max_age_is_denied_without_retrieval() -> None:
    request = make_request(max_age_seconds=0)
    source = RaisingEvidenceSource()

    result = _produce(request, source)

    assert not result.is_success
    assert result.denial.reason == "max_age_invalid"
    assert source.calls == []
