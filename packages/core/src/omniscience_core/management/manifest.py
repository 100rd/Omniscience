"""ManagementContextCapabilityManifest and consumer contract pin (SPEC-MCTX
REQ-MCTX-7) -- unknown major, digest skew, or incompatible capability returns
typed fallback-required; no best-effort parsing ever happens on skew.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from omniscience_core.management.contract_schemas import validate_against_schema


@dataclass(frozen=True)
class ManagementContextCapabilityManifest:
    """What this Omniscience deployment currently produces -- compared against every
    consumer's pinned ``ManagementContractPin`` on every ``produce`` call."""

    contract_major: int
    schema_revisions: Mapping[str, str]
    producer_manifest_digest: str
    supported_field_classes: tuple[str, ...]
    supported_domains: tuple[str, ...]
    produced_at: str

    def to_schema_dict(self) -> dict[str, Any]:
        return {
            "contract_major": self.contract_major,
            "schema_revisions": dict(self.schema_revisions),
            "producer_manifest_digest": self.producer_manifest_digest,
            "supported_field_classes": list(self.supported_field_classes),
            "supported_domains": list(self.supported_domains),
            "produced_at": self.produced_at,
        }


def validate_manifest(manifest: ManagementContextCapabilityManifest) -> list[str]:
    return validate_against_schema(
        manifest.to_schema_dict(), "management-context-capability-manifest.schema.json"
    )


@dataclass(frozen=True)
class ManagementContractPin:
    """What a consumer currently trusts -- compared against the live
    ``ManagementContextCapabilityManifest`` on every ``produce`` call, never assumed
    still valid (mirrors ``omniscience_core.privacy.policy.ConsumerPolicyPin``)."""

    contract_major: int
    schema_revision_pin: str
    producer_manifest_digest_pin: str


def check_contract_skew(
    consumer_pin: ManagementContractPin, manifest: ManagementContextCapabilityManifest
) -> list[str]:
    """Return non-empty reasons when the pinned major, schema revision, or producer
    manifest digest no longer match the live manifest. Any error blocks a favorable
    response -- the caller must return typed fallback-required (REQ-MCTX-7)."""
    errors: list[str] = []

    if consumer_pin.contract_major != manifest.contract_major:
        errors.append("contract_major_mismatch")

    if consumer_pin.schema_revision_pin not in manifest.schema_revisions.values():
        errors.append("schema_digest_mismatch")

    if consumer_pin.producer_manifest_digest_pin != manifest.producer_manifest_digest:
        errors.append("producer_manifest_mismatch")

    return errors
