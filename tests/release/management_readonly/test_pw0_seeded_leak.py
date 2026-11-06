"""AC-SP86-3: PW0 rejects unsafe data before every durable/derived/provider/
telemetry/archive/backup boundary this release exposes.

This is release evidence over the already-implemented SP-61 PW0 gate
(``omniscience_core.privacy.gate.IngestPIIGate``) and SP-81 management-context
producer's PW0-aware citation admission + conformance scan
(``omniscience_core.management.producer``/``conformance``). It replays a seeded
corpus of PII, unknown-class, reversible-token (pseudonymized personal, never
admission-eligible under PW0), and active-content values and proves none of it
reaches: (a) a protected ordinary-store/parse/chunk/embed/projection sink call,
(b) the non-identifying ``BoundaryReceipt`` PW0 emits per decision, or (c) the
management-context bundle this release's MCP read surface returns as its
response.
"""

from __future__ import annotations

import json

from omniscience_core.management import ManagementContextProducer
from omniscience_core.privacy import (
    DataEnvelope,
    GateDisposition,
    IngestPIIGate,
    InMemoryQuarantineStore,
)
from omniscience_core.privacy.receipts import compute_integrity
from omniscience_server.privacy.boundary import Pw0Boundary, Pw0BoundaryDeniedError

from tests.management.fixtures import (
    ACTIVE_CONTENT_CITATION,
    AUTHORIZATION,
    CONSUMER_PIN_MATCHING,
    CREDENTIAL_CITATION,
    INTERNAL_CITATION,
    MANIFEST,
    NORMAL_QUALITY,
    PII_RAW_CITATION,
    PRODUCED_AT,
    REFERENCE_NOW,
    SAFE_CITATION,
    RawCitation,
    make_request,
)
from tests.privacy.fixtures import CONSUMER_PIN_MATCHING as PW0_CONSUMER_PIN_MATCHING
from tests.privacy.fixtures import (
    SEEDED_ENVELOPES,
    RecordingSink,
    StaticDetector,
    policy_bundle_fixture,
)

REVERSIBLE_TOKEN_ENVELOPE_ID = "sev-reversible-token-personal-pseudonymized"


def _pseudonymized_personal_envelope() -> DataEnvelope:
    """A 'reversible token' fixture: personal data pseudonymized (not
    deterministically redacted). PW0_REDACTION_ELIGIBLE_CLASSES only admits
    personal/sensitive_personal once ``redacted`` -- pseudonymization is
    reversible and must never be admitted (ADR-0018 D3)."""
    payload = {
        "envelope_id": REVERSIBLE_TOKEN_ENVELOPE_ID,
        "tenant_id": "dev-fixture-tenant-a",
        "source_ref": "dev-fixture-source-reversible-token",
        "data_class": "personal",
        "transform_state": "pseudonymized",
        "field_manifest": ["title"],
        "allowed_fields": ["title"],
        "allowed_sinks": ["knowledge-store"],
        "policy_revision": "1.0.0",
        "produced_at": "2026-06-01T00:00:00Z",
    }
    return DataEnvelope(
        envelope_id=payload["envelope_id"],
        tenant_id=payload["tenant_id"],
        source_ref=payload["source_ref"],
        data_class=payload["data_class"],
        transform_state=payload["transform_state"],
        field_manifest=("title",),
        allowed_fields=("title",),
        allowed_sinks=("knowledge-store",),
        policy_revision="1.0.0",
        produced_at="2026-06-01T00:00:00Z",
        integrity=compute_integrity(payload),
    )


_SEEDED_UNSAFE_ENVELOPES: dict[str, DataEnvelope] = {
    "personal-raw": SEEDED_ENVELOPES["personal-raw"],
    "sensitive-raw": SEEDED_ENVELOPES["sensitive-raw"],
    "prohibited-raw": SEEDED_ENVELOPES["prohibited-raw"],
    "prohibited-redacted": SEEDED_ENVELOPES["prohibited-redacted"],
    "unknown": SEEDED_ENVELOPES["unknown"],
    "reversible-token": _pseudonymized_personal_envelope(),
}


def _gate() -> IngestPIIGate:
    return IngestPIIGate(
        quarantine_store=InMemoryQuarantineStore(), consumer_pin=PW0_CONSUMER_PIN_MATCHING
    )


def test_seeded_unsafe_data_never_admitted_by_the_gate() -> None:
    gate = _gate()
    bundle = policy_bundle_fixture()
    for label, envelope in _SEEDED_UNSAFE_ENVELOPES.items():
        detector = StaticDetector(
            data_class=envelope.data_class, transform_state=envelope.transform_state
        )
        decision = gate.evaluate(
            envelope=envelope,
            policy_bundle=bundle,
            detector=detector,
            reference_now="2026-06-15T00:00:00Z",
            receipt_id=f"receipt-{label}",
            produced_at="2026-06-15T00:00:00Z",
        )
        assert decision.disposition != GateDisposition.ADMIT, (
            f"{label} must never be admitted (data_class={envelope.data_class!r}, "
            f"transform_state={envelope.transform_state!r})"
        )


def test_seeded_unsafe_data_never_reaches_a_protected_sink() -> None:
    """A caller that (incorrectly) tries to invoke a protected sink on a
    non-ADMIT decision is refused by the boundary -- the sink itself is never
    called, proving no seeded value can reach the ordinary
    store/parse/chunk/embed/projection surface."""
    gate = _gate()
    bundle = policy_bundle_fixture()
    boundary = Pw0Boundary()
    for label, envelope in _SEEDED_UNSAFE_ENVELOPES.items():
        detector = StaticDetector(
            data_class=envelope.data_class, transform_state=envelope.transform_state
        )
        decision = gate.evaluate(
            envelope=envelope,
            policy_bundle=bundle,
            detector=detector,
            reference_now="2026-06-15T00:00:00Z",
            receipt_id=f"receipt-{label}",
            produced_at="2026-06-15T00:00:00Z",
        )
        sink = RecordingSink()
        for guard in (
            boundary.guard_store,
            boundary.guard_parse,
            boundary.guard_chunk,
            boundary.guard_embed,
            boundary.guard_project,
        ):
            try:
                guard(decision, sink, envelope)
            except Pw0BoundaryDeniedError:
                pass
            else:
                raise AssertionError(f"{label}: boundary admitted a non-ADMIT decision")
        assert sink.calls == [], f"{label}: sink was invoked despite non-ADMIT decision"


def test_boundary_receipts_carry_no_raw_envelope_content() -> None:
    """The BoundaryReceipt PW0 emits per decision is the artifact that would
    flow into logs/telemetry/traces -- it must carry only digests, counts, and
    closed-taxonomy reason codes, never the envelope's own identifying content."""
    gate = _gate()
    bundle = policy_bundle_fixture()
    for label, envelope in _SEEDED_UNSAFE_ENVELOPES.items():
        detector = StaticDetector(
            data_class=envelope.data_class, transform_state=envelope.transform_state
        )
        decision = gate.evaluate(
            envelope=envelope,
            policy_bundle=bundle,
            detector=detector,
            reference_now="2026-06-15T00:00:00Z",
            receipt_id=f"receipt-{label}",
            produced_at="2026-06-15T00:00:00Z",
        )
        receipt_json = json.dumps(
            {
                "receipt_id": decision.receipt.receipt_id,
                "disposition": decision.receipt.disposition,
                "reason": decision.receipt.reason,
                "envelope_digest": decision.receipt.envelope_digest,
                "policy_revision": decision.receipt.policy_revision,
                "profile": decision.receipt.profile,
                "detail": list(decision.receipt.detail),
            }
        )
        assert envelope.envelope_id not in receipt_json
        assert envelope.source_ref not in receipt_json


# ---------------------------------------------------------------------------
# Management-context read surface: seeded corpus must never reach the response
# ---------------------------------------------------------------------------

UNKNOWN_CLASS_CITATION = RawCitation(
    citation_id="cite-unknown-class-001",
    source_ref="dev-fixture-source-unclassified",
    uri="https://internal.dev-fixture/unclassified/notes",
    snapshot_digest="dev-fixture-digest-unknown-001",
    observed_at="2026-07-21T11:00:00+00:00",
    retrieval_strategy="dev-fixture-strategy-exact",
    field_class="citations",
    data_class="unknown",
    transform_state="raw",
    text="dev-fixture unclassified content that must never be admitted.",
)

REVERSIBLE_TOKEN_CITATION = RawCitation(
    citation_id="cite-reversible-token-001",
    source_ref="dev-fixture-source-pseudonymized",
    uri="https://internal.dev-fixture/pseudonymized/notes",
    snapshot_digest="dev-fixture-digest-reversible-001",
    observed_at="2026-07-21T11:00:00+00:00",
    retrieval_strategy="dev-fixture-strategy-exact",
    field_class="citations",
    data_class="personal",
    transform_state="pseudonymized",
    text="dev-fixture reversible-token content: subject-alias-77f2.",
)


class _MixedSeededEvidenceSource:
    """Fixture evidence source returning a batch mixing safe citations with the
    full AC-SP86-3 seeded-unsafe corpus: PII, unknown-class, reversible-token
    (pseudonymized), active-content, and credential-shaped values."""

    def health(self):
        from tests.management.fixtures import SourceHealth

        return SourceHealth(healthy=True)

    def fetch_citations(self, request):
        return (
            SAFE_CITATION,
            INTERNAL_CITATION,
            PII_RAW_CITATION,
            ACTIVE_CONTENT_CITATION,
            CREDENTIAL_CITATION,
            UNKNOWN_CLASS_CITATION,
            REVERSIBLE_TOKEN_CITATION,
        )

    def fetch_quality(self, request):
        return NORMAL_QUALITY


_SEEDED_SECRET_SUBSTRINGS: tuple[str, ...] = (
    "dev-fixture-user@example.test",  # PII_RAW_CITATION email
    "<script>alert('dev-fixture-xss')</script>",  # ACTIVE_CONTENT_CITATION
    "api_key",  # CREDENTIAL_CITATION
    "unclassified content that must never be admitted",  # UNKNOWN_CLASS_CITATION
    "subject-alias-77f2",  # REVERSIBLE_TOKEN_CITATION
)


def test_seeded_corpus_never_reaches_the_management_context_response() -> None:
    producer = ManagementContextProducer(contract_manifest=MANIFEST)
    request = make_request()
    result = producer.produce(
        request,
        authorization=AUTHORIZATION,
        consumer_pin=CONSUMER_PIN_MATCHING,
        evidence_source=_MixedSeededEvidenceSource(),
        pii_policy_revision="1.0.0",
        reference_now=REFERENCE_NOW,
        produced_at=PRODUCED_AT,
    )

    assert result.is_success, "safe citations alone must still produce a bundle"
    bundle = result.bundle
    assert bundle is not None

    admitted_ids = {c.citation_id for c in bundle.citations}
    assert admitted_ids == {SAFE_CITATION.citation_id, INTERNAL_CITATION.citation_id}
    assert PII_RAW_CITATION.citation_id not in admitted_ids
    assert ACTIVE_CONTENT_CITATION.citation_id not in admitted_ids
    assert CREDENTIAL_CITATION.citation_id not in admitted_ids
    assert UNKNOWN_CLASS_CITATION.citation_id not in admitted_ids
    assert REVERSIBLE_TOKEN_CITATION.citation_id not in admitted_ids

    # Every dropped citation is accounted for in coverage_gaps, never silently
    # discarded without evidence.
    assert len(bundle.coverage_gaps) == 5

    serialized = json.dumps(bundle.to_schema_dict())
    for secret in _SEEDED_SECRET_SUBSTRINGS:
        assert secret not in serialized, f"seeded secret leaked into the response: {secret!r}"
