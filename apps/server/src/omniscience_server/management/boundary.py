"""ManagementContextBoundary -- server-side entrypoint wrapping
``ManagementContextProducer`` (SPEC-MCTX REQ-MCTX-1).

A structurally malformed request payload is denied before a
``ManagementContextRequest`` is even constructed, let alone before any evidence
source is touched -- REQ-MCTX-1's "denied without retrieval" applies to payload
shape, not only to authorized-scope checks.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from omniscience_core.management.contract_schemas import validate_against_schema
from omniscience_core.management.evidence_source import ManagementEvidenceSource
from omniscience_core.management.manifest import ManagementContractPin
from omniscience_core.management.producer import ManagementContextProducer
from omniscience_core.management.request import ManagementContextRequest, RequestAuthorization
from omniscience_core.management.severance import ManagementContextResult


def request_from_payload(payload: Mapping[str, Any]) -> ManagementContextRequest:
    """Construct a request from an already schema-validated payload. Callers must
    call ``validate_against_schema`` first -- see ``ManagementContextBoundary.handle``."""
    return ManagementContextRequest(
        request_id=payload["request_id"],
        tenant_id=payload["tenant_id"],
        workspace_id=payload["workspace_id"],
        subject_ref=payload["subject_ref"],
        management_domain=payload["management_domain"],
        purpose=payload["purpose"],
        requested_field_classes=tuple(payload["requested_field_classes"]),
        evidence_cut=payload["evidence_cut"],
        max_age_seconds=payload["max_age_seconds"],
        contract_major=payload["contract_major"],
        schema_revision=payload["schema_revision"],
        caller_identity=payload["caller_identity"],
        correlation_id=payload["correlation_id"],
    )


@dataclass(frozen=True)
class ManagementContextBoundary:
    producer: ManagementContextProducer

    def handle(
        self,
        payload: Mapping[str, Any],
        *,
        authorization: RequestAuthorization,
        consumer_pin: ManagementContractPin,
        evidence_source: ManagementEvidenceSource,
        pii_policy_revision: str,
        reference_now: str,
        produced_at: str,
    ) -> ManagementContextResult:
        payload_errors = validate_against_schema(payload, "management-context-request.schema.json")
        if payload_errors:
            return ManagementContextResult.deny("request_schema_invalid", tuple(payload_errors))

        request = request_from_payload(payload)
        return self.producer.produce(
            request,
            authorization=authorization,
            consumer_pin=consumer_pin,
            evidence_source=evidence_source,
            pii_policy_revision=pii_policy_revision,
            reference_now=reference_now,
            produced_at=produced_at,
        )
