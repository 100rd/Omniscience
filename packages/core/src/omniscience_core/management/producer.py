"""ManagementContextProducer (SPEC-MCTX, ADR-0021) -- the read-only v1 producer.

Order of operations mirrors ``omniscience_core.privacy.gate.IngestPIIGate``: every
foreseeable failure returns a typed, closed-enum outcome. Nothing here calls a
provider/model, mutates state, or makes a management decision -- ADR-0021's
development-authority boundary is schema/fixtures/read-only producer code only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from omniscience_core.management.bundle import ManagementContextBundle, validate_bundle_schema
from omniscience_core.management.citation import ManagementCitation
from omniscience_core.management.conformance import conformance_scan, scan_text
from omniscience_core.management.evidence_source import (
    ManagementEvidenceSource,
    RawCitation,
    RawQualityInputs,
)
from omniscience_core.management.manifest import (
    ManagementContextCapabilityManifest,
    ManagementContractPin,
    check_contract_skew,
)
from omniscience_core.management.pii import admit_field, build_pii_receipt
from omniscience_core.management.quality import KnowledgeQualitySnapshot
from omniscience_core.management.request import (
    ManagementContextRequest,
    RequestAuthorization,
    validate_scope,
)
from omniscience_core.management.severance import ManagementContextResult
from omniscience_core.privacy.receipts import compute_integrity


def _parse(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


@dataclass(frozen=True)
class ManagementContextProducer:
    contract_manifest: ManagementContextCapabilityManifest

    def produce(
        self,
        request: ManagementContextRequest,
        *,
        authorization: RequestAuthorization,
        consumer_pin: ManagementContractPin,
        evidence_source: ManagementEvidenceSource,
        pii_policy_revision: str,
        reference_now: str,
        produced_at: str,
    ) -> ManagementContextResult:
        skew = check_contract_skew(consumer_pin, self.contract_manifest)
        if skew:
            return ManagementContextResult.require_fallback(skew[0], tuple(skew))

        scope_errors = validate_scope(request, authorization, reference_now=reference_now)
        if scope_errors:
            return ManagementContextResult.deny(scope_errors[0], tuple(scope_errors))

        try:
            health = evidence_source.health()
        except Exception as exc:
            return ManagementContextResult.require_fallback("producer_unavailable", (str(exc),))
        if not health.healthy:
            return ManagementContextResult.require_fallback(
                "source_severed", (health.reason or "evidence source unhealthy",)
            )

        if _parse(reference_now) - _parse(request.evidence_cut) > timedelta(
            seconds=request.max_age_seconds
        ):
            return ManagementContextResult.require_fallback(
                "evidence_stale", (f"evidence_cut {request.evidence_cut} exceeds max_age_seconds",)
            )

        try:
            raw_citations = evidence_source.fetch_citations(request)
            raw_quality = evidence_source.fetch_quality(request)
        except Exception as exc:
            return ManagementContextResult.require_fallback("source_severed", (str(exc),))

        citations, coverage_gaps, admitted, dropped = self._admit_citations(raw_citations)

        quality = self._build_quality(request, raw_quality)

        pii_receipt = build_pii_receipt(
            admitted_count=admitted,
            dropped_count=dropped,
            policy_revision=pii_policy_revision,
            produced_at=produced_at,
        )

        bundle = self._assemble_bundle(
            request=request,
            citations=citations,
            coverage_gaps=coverage_gaps,
            pii_receipt=pii_receipt,
            quality=quality,
            produced_at=produced_at,
        )

        violations = conformance_scan(bundle.to_schema_dict())
        if violations:
            return ManagementContextResult.require_fallback(
                "authority_field_detected", tuple(violations)
            )

        schema_errors = validate_bundle_schema(bundle)
        if schema_errors:
            return ManagementContextResult.require_fallback(
                "bundle_schema_invalid", tuple(schema_errors)
            )

        return ManagementContextResult.ok(bundle)

    @staticmethod
    def _admit_citations(
        raw_citations: tuple[RawCitation, ...],
    ) -> tuple[tuple[ManagementCitation, ...], tuple[str, ...], int, int]:
        admitted_citations: list[ManagementCitation] = []
        coverage_gaps: list[str] = []
        dropped = 0
        for raw in raw_citations:
            decision = admit_field(data_class=raw.data_class, transform_state=raw.transform_state)
            if not decision.admitted:
                dropped += 1
                coverage_gaps.append(f"{raw.field_class}:{decision.reason}")
                continue
            content_violations = scan_text(raw.text)
            if content_violations:
                dropped += 1
                coverage_gaps.append(f"{raw.field_class}:content_rejected")
                continue
            admitted_citations.append(
                ManagementCitation(
                    citation_id=raw.citation_id,
                    source_ref=raw.source_ref,
                    uri=raw.uri,
                    snapshot_digest=raw.snapshot_digest,
                    observed_at=raw.observed_at,
                    retrieval_strategy=raw.retrieval_strategy,
                    field_class=raw.field_class,
                    evidence_fitness="known",
                )
            )
        return tuple(admitted_citations), tuple(coverage_gaps), len(admitted_citations), dropped

    @staticmethod
    def _build_quality(
        request: ManagementContextRequest, raw_quality: RawQualityInputs
    ) -> KnowledgeQualitySnapshot:
        detail = dict(raw_quality.detail)
        snapshot_id = f"quality-{request.request_id}"
        provenance_status = raw_quality.provenance_status or "unknown"
        freshness_status = raw_quality.freshness_status or "unknown"
        conformance_status = raw_quality.conformance_status or "unknown"
        coverage_status = raw_quality.coverage_status or "unknown"
        conflict_status = raw_quality.conflict_status or "unknown"
        projection_status = raw_quality.projection_status or "unknown"
        integrity_payload: dict[str, Any] = {
            "snapshot_id": snapshot_id,
            "workspace_id": request.workspace_id,
            "management_domain": request.management_domain,
            "provenance_status": provenance_status,
            "freshness_status": freshness_status,
            "conformance_status": conformance_status,
            "coverage_status": coverage_status,
            "conflict_status": conflict_status,
            "projection_status": projection_status,
            "evaluated_at": request.evidence_cut,
            "detail": {axis: list(reasons) for axis, reasons in detail.items()},
        }
        return KnowledgeQualitySnapshot(
            snapshot_id=snapshot_id,
            workspace_id=request.workspace_id,
            management_domain=request.management_domain,
            provenance_status=provenance_status,
            freshness_status=freshness_status,
            conformance_status=conformance_status,
            coverage_status=coverage_status,
            conflict_status=conflict_status,
            projection_status=projection_status,
            evaluated_at=request.evidence_cut,
            detail=detail,
            integrity=compute_integrity(integrity_payload),
        )

    def _assemble_bundle(
        self,
        *,
        request: ManagementContextRequest,
        citations: tuple[ManagementCitation, ...],
        coverage_gaps: tuple[str, ...],
        pii_receipt: dict[str, Any],
        quality: KnowledgeQualitySnapshot,
        produced_at: str,
    ) -> ManagementContextBundle:
        source_revisions = {"contract": self.contract_manifest.producer_manifest_digest}
        identity = {
            "request_digest": request.digest(),
            "producer_revision": self.contract_manifest.producer_manifest_digest,
            "produced_at": produced_at,
        }
        return ManagementContextBundle(
            bundle_id=f"bundle-{request.request_id}",
            request_digest=request.digest(),
            producer_revision=self.contract_manifest.producer_manifest_digest,
            source_revisions=source_revisions,
            produced_at=produced_at,
            observed_at=request.evidence_cut,
            freshness_deadline=request.evidence_cut,
            source_health="healthy",
            citations=citations,
            conflict_set=(),
            projection_state=quality.projection_status,
            coverage_gaps=coverage_gaps,
            pii_receipt=pii_receipt,
            quality=quality,
            synthesis=(),
            integrity=compute_integrity(identity),
        )
