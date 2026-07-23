"""DataEnvelope (ADR-0018 D5, SPEC-PII REQ-PII-2/3) -- server-derived, never caller-widened."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from omniscience_core.privacy.contract_schemas import validate_against_schema


@dataclass(frozen=True)
class DataEnvelope:
    """Pre-ingest classification carrier. Content and caller labels never widen
    ``data_class``, ``allowed_fields``, or ``allowed_sinks`` -- those are set by the
    gate from server-derived policy, not read back out of the payload (SPEC-PII
    REQ-PII-1)."""

    envelope_id: str
    tenant_id: str
    source_ref: str
    data_class: str
    transform_state: str
    field_manifest: tuple[str, ...]
    allowed_fields: tuple[str, ...]
    allowed_sinks: tuple[str, ...]
    policy_revision: str
    produced_at: str
    integrity: Mapping[str, str]
    workspace_id: str | None = None
    subject_ref: str | None = None
    purpose_id: str | None = None
    permit_id: str | None = None
    lineage: tuple[str, ...] = field(default_factory=tuple)
    expires_at: str | None = None
    coverage: str | None = None

    def to_schema_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "envelope_id": self.envelope_id,
            "tenant_id": self.tenant_id,
            "source_ref": self.source_ref,
            "data_class": self.data_class,
            "transform_state": self.transform_state,
            "field_manifest": list(self.field_manifest),
            "allowed_fields": list(self.allowed_fields),
            "allowed_sinks": list(self.allowed_sinks),
            "policy_revision": self.policy_revision,
            "produced_at": self.produced_at,
            "integrity": dict(self.integrity),
        }
        optional: Sequence[tuple[str, Any]] = (
            ("workspace_id", self.workspace_id),
            ("subject_ref", self.subject_ref),
            ("purpose_id", self.purpose_id),
            ("permit_id", self.permit_id),
            ("lineage", list(self.lineage) if self.lineage else None),
            ("expires_at", self.expires_at),
            ("coverage", self.coverage),
        )
        for key, value in optional:
            if value is not None:
                payload[key] = value
        return payload


def validate_envelope(envelope: DataEnvelope) -> list[str]:
    """Structural validation against the vendored ``data-envelope.schema.json``."""
    return validate_against_schema(envelope.to_schema_dict(), "data-envelope.schema.json")
