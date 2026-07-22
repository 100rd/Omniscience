"""Disposable, synthetic SP-81 management-context fixtures (task-sp-81).

Every value below is a ``dev-fixture-*`` placeholder. None is live personal data,
a real evidence source, or a production contract manifest.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from omniscience_core.management import (
    ManagementContextCapabilityManifest,
    ManagementContextRequest,
    ManagementContractPin,
    RawCitation,
    RawQualityInputs,
    RequestAuthorization,
    SourceHealth,
)

REFERENCE_NOW = "2026-07-21T12:00:00+00:00"
PRODUCED_AT = "2026-07-21T12:00:00+00:00"
EVIDENCE_CUT = "2026-07-21T11:00:00+00:00"
EVIDENCE_CUT_FUTURE = "2026-07-22T00:00:00+00:00"
WORKSPACE_ID = "dev-fixture-workspace-a"
DOMAIN = "reliability"
PURPOSE = "dev-fixture-purpose-support"

MANIFEST = ManagementContextCapabilityManifest(
    contract_major=1,
    schema_revisions={"management-context-bundle.schema.json": "dev-fixture-schema-rev-1"},
    producer_manifest_digest="dev-fixture-producer-manifest-digest-v1",
    supported_field_classes=(
        "citations",
        "provenance",
        "freshness",
        "coverage",
        "knowledge_quality",
    ),
    supported_domains=(DOMAIN, "knowledge_quality"),
    produced_at=PRODUCED_AT,
)

CONSUMER_PIN_MATCHING = ManagementContractPin(
    contract_major=1,
    schema_revision_pin="dev-fixture-schema-rev-1",
    producer_manifest_digest_pin="dev-fixture-producer-manifest-digest-v1",
)

CONSUMER_PIN_MAJOR_MISMATCH = ManagementContractPin(
    contract_major=2,
    schema_revision_pin="dev-fixture-schema-rev-1",
    producer_manifest_digest_pin="dev-fixture-producer-manifest-digest-v1",
)

CONSUMER_PIN_SCHEMA_MISMATCH = ManagementContractPin(
    contract_major=1,
    schema_revision_pin="dev-fixture-schema-rev-STALE",
    producer_manifest_digest_pin="dev-fixture-producer-manifest-digest-v1",
)

CONSUMER_PIN_MANIFEST_MISMATCH = ManagementContractPin(
    contract_major=1,
    schema_revision_pin="dev-fixture-schema-rev-1",
    producer_manifest_digest_pin="dev-fixture-producer-manifest-digest-STALE",
)

AUTHORIZATION = RequestAuthorization(
    workspace_id=WORKSPACE_ID,
    allowed_domains=(DOMAIN,),
    purpose_field_classes={PURPOSE: ("citations", "provenance", "freshness", "knowledge_quality")},
    max_allowed_age_seconds=7200,
)


def make_request(
    *,
    request_id: str = "req-001",
    workspace_id: str = WORKSPACE_ID,
    management_domain: str = DOMAIN,
    purpose: str = PURPOSE,
    requested_field_classes: tuple[str, ...] = ("citations", "knowledge_quality"),
    evidence_cut: str = EVIDENCE_CUT,
    max_age_seconds: int = 3600,
    contract_major: int = 1,
    schema_revision: str = "dev-fixture-schema-rev-1",
) -> ManagementContextRequest:
    return ManagementContextRequest(
        request_id=request_id,
        tenant_id="dev-fixture-tenant-a",
        workspace_id=workspace_id,
        subject_ref="dev-fixture-subject-svc-checkout",
        management_domain=management_domain,
        purpose=purpose,
        requested_field_classes=requested_field_classes,
        evidence_cut=evidence_cut,
        max_age_seconds=max_age_seconds,
        contract_major=contract_major,
        schema_revision=schema_revision,
        caller_identity="dev-fixture-caller-barbarossa",
        correlation_id="dev-fixture-correlation-001",
    )


VALID_REQUEST = make_request()

SAFE_CITATION = RawCitation(
    citation_id="cite-public-001",
    source_ref="dev-fixture-source-runbook",
    uri="https://internal.dev-fixture/runbooks/checkout-timeout",
    snapshot_digest="dev-fixture-digest-001",
    observed_at=EVIDENCE_CUT,
    retrieval_strategy="dev-fixture-strategy-exact",
    field_class="citations",
    data_class="public",
    transform_state="raw",
    text="Checkout timeout runbook: retry with exponential backoff.",
)

INTERNAL_CITATION = RawCitation(
    citation_id="cite-internal-001",
    source_ref="dev-fixture-source-postmortem",
    uri="https://internal.dev-fixture/postmortems/2026-07-01",
    snapshot_digest="dev-fixture-digest-002",
    observed_at=EVIDENCE_CUT,
    retrieval_strategy="dev-fixture-strategy-exact",
    field_class="citations",
    data_class="internal_non_personal",
    transform_state="raw",
    text="Postmortem excerpt: root cause was a connection-pool exhaustion.",
)

PII_RAW_CITATION = RawCitation(
    citation_id="cite-personal-raw-001",
    source_ref="dev-fixture-source-ticket",
    uri="https://internal.dev-fixture/tickets/9001",
    snapshot_digest="dev-fixture-digest-003",
    observed_at=EVIDENCE_CUT,
    retrieval_strategy="dev-fixture-strategy-exact",
    field_class="citations",
    data_class="personal",
    transform_state="raw",
    text="Reporter contact: dev-fixture-user@example.test",
)

ACTIVE_CONTENT_CITATION = RawCitation(
    citation_id="cite-active-content-001",
    source_ref="dev-fixture-source-wiki",
    uri="https://internal.dev-fixture/wiki/notes",
    snapshot_digest="dev-fixture-digest-004",
    observed_at=EVIDENCE_CUT,
    retrieval_strategy="dev-fixture-strategy-exact",
    field_class="citations",
    data_class="public",
    transform_state="raw",
    text="See notes <script>alert('dev-fixture-xss')</script> for details.",
)

CREDENTIAL_CITATION = RawCitation(
    citation_id="cite-credential-001",
    source_ref="dev-fixture-source-config",
    uri="https://internal.dev-fixture/config/notes",
    snapshot_digest="dev-fixture-digest-005",
    observed_at=EVIDENCE_CUT,
    retrieval_strategy="dev-fixture-strategy-exact",
    field_class="citations",
    data_class="public",
    transform_state="raw",
    text="Rotate the api_key before the next deploy window.",
)

NORMAL_QUALITY = RawQualityInputs(
    provenance_status="complete",
    freshness_status="fresh",
    conformance_status="conformant",
    coverage_status="complete",
    conflict_status="none",
    projection_status="converged",
    detail={"provenance": ("dev-fixture-source-runbook",)},
)

PARTIAL_QUALITY = RawQualityInputs(
    provenance_status=None,
    freshness_status="stale",
    conformance_status=None,
    coverage_status="partial",
    conflict_status="unknown",
    projection_status=None,
    detail={},
)

AUTHORITY_LEAK_QUALITY = RawQualityInputs(
    provenance_status="complete",
    freshness_status="fresh",
    conformance_status="conformant",
    coverage_status="complete",
    conflict_status="none",
    projection_status="converged",
    detail={"risk": ("dev-fixture-leaked-risk-score",)},
)


@dataclass(frozen=True)
class StaticEvidenceSource:
    citations: tuple[RawCitation, ...] = (SAFE_CITATION, INTERNAL_CITATION)
    quality: RawQualityInputs = NORMAL_QUALITY
    healthy: bool = True
    unhealthy_reason: str | None = None

    def health(self) -> SourceHealth:
        return SourceHealth(healthy=self.healthy, reason=self.unhealthy_reason)

    def fetch_citations(self, request: ManagementContextRequest) -> tuple[RawCitation, ...]:
        return self.citations

    def fetch_quality(self, request: ManagementContextRequest) -> RawQualityInputs:
        return self.quality


@dataclass
class RaisingEvidenceSource:
    """Fixture source that always fails -- proves the producer returns
    fallback-required instead of a crash or a stale success (AC-SP81-3)."""

    fail_on_health: bool = False
    calls: list[str] = field(default_factory=list)

    def health(self) -> SourceHealth:
        self.calls.append("health")
        if self.fail_on_health:
            raise RuntimeError("dev-fixture-injected source failure")
        return SourceHealth(healthy=True)

    def fetch_citations(self, request: ManagementContextRequest) -> tuple[RawCitation, ...]:
        self.calls.append("fetch_citations")
        raise RuntimeError("dev-fixture-injected source failure")

    def fetch_quality(self, request: ManagementContextRequest) -> RawQualityInputs:
        self.calls.append("fetch_quality")
        return NORMAL_QUALITY
