"""Disposable, synthetic PW0 test fixtures (task-sp-61-pii-wall-pw0).

Every value below is a ``dev-fixture-*`` placeholder. None is live personal data,
a real signing key, or a production quarantine/store backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from omniscience_core.privacy import (
    ConsumerPolicyPin,
    DataEnvelope,
    DetectionResult,
    compute_integrity,
)

REFERENCE_NOW_VALID = "2026-06-15T00:00:00Z"
REFERENCE_NOW_AFTER_EXPIRY = "2027-06-01T00:00:00Z"


def make_envelope(
    envelope_id: str,
    *,
    data_class: str,
    transform_state: str,
    allowed_fields: tuple[str, ...] = ("title",),
    allowed_sinks: tuple[str, ...] = ("knowledge-store",),
) -> DataEnvelope:
    payload = {
        "envelope_id": envelope_id,
        "tenant_id": "dev-fixture-tenant-a",
        "source_ref": f"dev-fixture-source-{envelope_id}",
        "data_class": data_class,
        "transform_state": transform_state,
        "field_manifest": list(allowed_fields),
        "allowed_fields": list(allowed_fields),
        "allowed_sinks": list(allowed_sinks),
        "policy_revision": "1.0.0",
        "produced_at": "2026-06-01T00:00:00Z",
    }
    return DataEnvelope(
        envelope_id=envelope_id,
        tenant_id="dev-fixture-tenant-a",
        source_ref=f"dev-fixture-source-{envelope_id}",
        data_class=data_class,
        transform_state=transform_state,
        field_manifest=allowed_fields,
        allowed_fields=allowed_fields,
        allowed_sinks=allowed_sinks,
        policy_revision="1.0.0",
        produced_at="2026-06-01T00:00:00Z",
        integrity=compute_integrity(payload),
    )


SEEDED_ENVELOPES: dict[str, DataEnvelope] = {
    "public-raw": make_envelope("seed-public-001", data_class="public", transform_state="raw"),
    "internal-raw": make_envelope(
        "seed-internal-001", data_class="internal_non_personal", transform_state="raw"
    ),
    "personal-redacted": make_envelope(
        "seed-personal-redacted-001", data_class="personal", transform_state="redacted"
    ),
    "personal-raw": make_envelope(
        "seed-personal-001", data_class="personal", transform_state="raw"
    ),
    "sensitive-raw": make_envelope(
        "seed-sensitive-001", data_class="sensitive_personal", transform_state="raw"
    ),
    "prohibited-raw": make_envelope(
        "seed-prohibited-001", data_class="prohibited", transform_state="raw"
    ),
    "prohibited-redacted": make_envelope(
        "seed-prohibited-redacted-001", data_class="prohibited", transform_state="redacted"
    ),
    "unknown": make_envelope("seed-unknown-001", data_class="unknown", transform_state="raw"),
}


@dataclass(frozen=True)
class StaticDetector:
    """Deterministic fixture detector -- always reports the classification it was
    constructed with, regardless of envelope content."""

    data_class: str
    transform_state: str
    detector_revisions: tuple[str, ...] = ("exact-match-taxonomy-v1@1.0.0",)

    def classify(self, envelope: DataEnvelope) -> DetectionResult:
        return DetectionResult(
            data_class=self.data_class,
            transform_state=self.transform_state,
            detector_revisions=self.detector_revisions,
        )


class RaisingDetector:
    """Fixture detector that always fails -- proves the gate parks on detector
    failure instead of admitting (AC-SP61-2)."""

    def classify(self, envelope: DataEnvelope) -> DetectionResult:
        raise RuntimeError("fixture-injected detector failure")


@dataclass
class RecordingSink:
    """Fake protected sink recording every envelope it was actually called with --
    used to prove non-admitted fixtures never reach it."""

    calls: list[DataEnvelope] = field(default_factory=list)

    def store(self, envelope: DataEnvelope) -> None:
        self.calls.append(envelope)

    def parse(self, envelope: DataEnvelope) -> None:
        self.calls.append(envelope)

    def chunk(self, envelope: DataEnvelope) -> None:
        self.calls.append(envelope)

    def embed(self, envelope: DataEnvelope) -> None:
        self.calls.append(envelope)

    def project(self, envelope: DataEnvelope) -> None:
        self.calls.append(envelope)


def _policy_bundle_payload() -> dict:
    return {
        "bundle_id": "dev-fixture-bundle-sp61-v1",
        "policy_revision": "1.0.0",
        "taxonomy_version": "1.0.0",
        "profiles": {
            "PW0": {
                "admits_data_classes": ["public", "internal_non_personal"],
                "requires_transform": ["redacted"],
                "requires_permit": False,
            },
            "PW1": {
                "admits_data_classes": [
                    "public",
                    "internal_non_personal",
                    "personal",
                    "sensitive_personal",
                ],
                "requires_transform": ["pseudonymized", "redacted"],
                "requires_permit": False,
            },
            "PW2": {
                "admits_data_classes": [
                    "public",
                    "internal_non_personal",
                    "personal",
                    "sensitive_personal",
                ],
                "requires_transform": ["raw", "pseudonymized", "redacted"],
                "requires_permit": True,
            },
        },
        "detector_manifests": [{"detector_id": "exact-match-taxonomy-v1", "revision": "1.0.0"}],
        "transformations": ["raw", "redacted", "pseudonymized", "aggregated"],
        "purposes": [{"purpose_id": "dev-fixture-purpose-support"}],
        "source_classes": ["ingest-upload"],
        "sink_classes": ["knowledge-store", "model-context"],
        "field_rules": [{"field": "email", "min_data_class": "personal"}],
        "retention_rules": [{"store_class": "knowledge-store", "max_retention_days": 365}],
        "permit_constraints": {"max_lifetime_seconds": 3600},
        "compatibility": {"schema_major": 1, "min_supported_major": 1, "max_supported_major": 1},
        "issued_at": "2026-01-01T00:00:00Z",
        "expires_at": "2027-01-01T00:00:00Z",
        "signer": {"key_id": "dev-fixture-signer-v1", "algorithm": "ed25519-dev-fixture-only"},
    }


def policy_bundle_fixture() -> dict:
    payload = _policy_bundle_payload()
    payload["integrity"] = compute_integrity(payload)
    return payload


CONSUMER_PIN_MATCHING = ConsumerPolicyPin(
    component_id="dev-fixture-consumer-omniscience",
    schema_major=1,
    policy_revision_pin="1.0.0",
    signer_trust_profile="dev-fixture-signer-v1",
)

CONSUMER_PIN_SIGNER_MISMATCH = ConsumerPolicyPin(
    component_id="dev-fixture-consumer-omniscience",
    schema_major=1,
    policy_revision_pin="1.0.0",
    signer_trust_profile="dev-fixture-signer-v0-revoked",
)

CONSUMER_PIN_SCHEMA_MAJOR_MISMATCH = ConsumerPolicyPin(
    component_id="dev-fixture-consumer-omniscience",
    schema_major=2,
    policy_revision_pin="0.9.0",
    signer_trust_profile="dev-fixture-signer-v1",
)
