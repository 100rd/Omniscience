"""ManagementContextRequest (SPEC-MCTX REQ-MCTX-1) -- scope is denied before retrieval.

Workspace/tenant identity is never trusted from the request payload alone; callers
must also supply a server-derived ``RequestAuthorization`` (mirrors SPEC-ACL
REQ-ACL-1) that ``validate_scope`` checks the request against.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from omniscience_core.management.contract_schemas import validate_against_schema
from omniscience_core.management.taxonomy import FIELD_CLASSES, MANAGEMENT_DOMAINS
from omniscience_core.privacy.receipts import compute_integrity


@dataclass(frozen=True)
class ManagementContextRequest:
    request_id: str
    tenant_id: str
    workspace_id: str
    subject_ref: str
    management_domain: str
    purpose: str
    requested_field_classes: tuple[str, ...]
    evidence_cut: str
    max_age_seconds: int
    contract_major: int
    schema_revision: str
    caller_identity: str
    correlation_id: str

    def to_schema_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "subject_ref": self.subject_ref,
            "management_domain": self.management_domain,
            "purpose": self.purpose,
            "requested_field_classes": list(self.requested_field_classes),
            "evidence_cut": self.evidence_cut,
            "max_age_seconds": self.max_age_seconds,
            "contract_major": self.contract_major,
            "schema_revision": self.schema_revision,
            "caller_identity": self.caller_identity,
            "correlation_id": self.correlation_id,
        }

    def digest(self) -> str:
        """Content-addressed digest used as ``request_digest`` on the produced bundle
        and for deterministic replay comparisons (REQ-MCTX-10)."""
        return compute_integrity(self.to_schema_dict())["digest"]


def validate_request_schema(request: ManagementContextRequest) -> list[str]:
    return validate_against_schema(
        request.to_schema_dict(), "management-context-request.schema.json"
    )


@dataclass(frozen=True)
class RequestAuthorization:
    """Server-derived authorization context -- never read back out of the request
    payload itself (SPEC-ACL REQ-ACL-1)."""

    workspace_id: str
    allowed_domains: tuple[str, ...]
    purpose_field_classes: Mapping[str, tuple[str, ...]]
    max_allowed_age_seconds: int


def validate_scope(
    request: ManagementContextRequest,
    authorization: RequestAuthorization,
    *,
    reference_now: str,
) -> list[str]:
    """Return non-empty reasons when scope must be denied before any retrieval.

    Every check here runs before an evidence source is ever touched -- REQ-MCTX-1
    fallback: "missing, foreign, widened, future or unsupported scope is denied
    without retrieval."
    """
    errors: list[str] = []

    if request.workspace_id != authorization.workspace_id:
        errors.append("workspace_missing_or_foreign")

    if request.management_domain not in MANAGEMENT_DOMAINS or (
        request.management_domain not in authorization.allowed_domains
    ):
        errors.append("domain_unknown")

    allowed_field_classes = authorization.purpose_field_classes.get(request.purpose)
    if allowed_field_classes is None:
        errors.append("purpose_unknown_or_unauthorized")
    else:
        widened = [
            field_class
            for field_class in request.requested_field_classes
            if field_class not in FIELD_CLASSES or field_class not in allowed_field_classes
        ]
        if widened:
            errors.append("field_class_widened")

    if request.evidence_cut > reference_now:
        errors.append("evidence_cut_in_future")

    if (
        request.max_age_seconds <= 0
        or request.max_age_seconds > authorization.max_allowed_age_seconds
    ):
        errors.append("max_age_invalid")

    if not request.contract_major or not request.schema_revision:
        errors.append("contract_revision_missing")

    return errors
