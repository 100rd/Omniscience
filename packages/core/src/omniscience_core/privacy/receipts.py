"""Non-identifying receipts (ADR-0018 D5/PII-7, SPEC-PII REQ-PII-4/10).

Every receipt here carries only digests, counts, closed-taxonomy reason codes, and
policy/detector revisions -- never a raw value, subject reference content, or field
payload. ``tests/test_pii_wall_fail_closed.py`` scans produced receipts for leakage.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from omniscience_core.privacy.contract_schemas import validate_against_schema


def compute_integrity(payload: Mapping[str, Any]) -> dict[str, str]:
    """Content-addressed integrity marker over a canonical JSON payload."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {"algorithm": "sha256", "digest": digest}


@dataclass(frozen=True)
class BoundaryReceipt:
    """Evidence of one PW0 gate decision. ``envelope_digest`` replaces any reference
    to the envelope's own subject/content -- it is a digest of ``envelope_id`` plus
    ``policy_revision``, never the envelope payload."""

    receipt_id: str
    disposition: str
    reason: str
    envelope_digest: str
    policy_revision: str | None
    profile: str
    produced_at: str
    producer: str
    detail: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "disposition": self.disposition,
            "reason": self.reason,
            "envelope_digest": self.envelope_digest,
            "policy_revision": self.policy_revision,
            "profile": self.profile,
            "produced_at": self.produced_at,
            "producer": self.producer,
            "detail": list(self.detail),
        }


def digest_envelope_identity(*, envelope_id: str, policy_revision: str) -> str:
    canonical = json.dumps(
        {"envelope_id": envelope_id, "policy_revision": policy_revision}, sort_keys=True
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class SanitizationReceipt:
    receipt_id: str
    input_digest: str
    policy_revision: str
    profile: str
    detector_revisions: tuple[str, ...]
    transformations: tuple[str, ...]
    disposition: str
    coverage: str
    produced_at: str
    producer: str
    integrity: Mapping[str, str]
    output_digest: str | None = None

    def to_schema_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "receipt_id": self.receipt_id,
            "input_digest": self.input_digest,
            "policy_revision": self.policy_revision,
            "profile": self.profile,
            "detector_revisions": list(self.detector_revisions),
            "transformations": list(self.transformations),
            "disposition": self.disposition,
            "coverage": self.coverage,
            "produced_at": self.produced_at,
            "producer": self.producer,
            "integrity": dict(self.integrity),
        }
        if self.output_digest is not None:
            payload["output_digest"] = self.output_digest
        return payload


def validate_sanitization_receipt(receipt: SanitizationReceipt) -> list[str]:
    return validate_against_schema(receipt.to_schema_dict(), "sanitization-receipt.schema.json")


@dataclass(frozen=True)
class PrivacyCoverage:
    """Owner-produced coverage projection (ADR-0018 D10). Partial coverage is named
    explicitly in ``uncovered_or_unknown``; it is never inferred as fully covered."""

    component_id: str
    tenant_id: str
    policy_revision: str
    profile: str
    boundary_set: tuple[str, ...]
    covered_stores: tuple[str, ...]
    uncovered_or_unknown: tuple[str, ...]
    observed_at: str
    freshness_deadline: str
    source_health: str
    integrity: Mapping[str, str]
    workspace_id: str | None = None

    def to_schema_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "component_id": self.component_id,
            "tenant_id": self.tenant_id,
            "policy_revision": self.policy_revision,
            "profile": self.profile,
            "boundary_set": list(self.boundary_set),
            "covered_stores": list(self.covered_stores),
            "uncovered_or_unknown": list(self.uncovered_or_unknown),
            "observed_at": self.observed_at,
            "freshness_deadline": self.freshness_deadline,
            "source_health": self.source_health,
            "integrity": dict(self.integrity),
        }
        if self.workspace_id is not None:
            payload["workspace_id"] = self.workspace_id
        return payload


def validate_privacy_coverage(coverage: PrivacyCoverage) -> list[str]:
    return validate_against_schema(
        coverage.to_schema_dict(), "privacy-coverage-envelope.schema.json"
    )


_RAW_LEAK_KEY_TOKENS: Sequence[str] = (
    "raw",
    "plaintext",
    "reversible_token",
    "prompt_excerpt",
    "provider_payload",
    "subject_value",
)


def scan_receipt_for_raw_leakage(receipt: Mapping[str, Any]) -> list[str]:
    """Defense-in-depth scan (SPEC-PII REQ-PII-10): a schema cannot prove the absence
    of an unnamed field, so this walks the whole receipt for forbidden field names."""
    errors: list[str] = []
    for key, value in _walk(receipt):
        lowered = key.lower()
        if any(token in lowered for token in _RAW_LEAK_KEY_TOKENS):
            errors.append(f"raw leakage: forbidden field name '{key}' present in receipt")
        if isinstance(value, str) and "@" in value and "." in value.split("@")[-1]:
            errors.append(f"raw leakage: field '{key}' contains an email-shaped value")
    return errors


def _walk(node: Any, prefix: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.append((path, value))
            out.extend(_walk(value, path))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            out.extend(_walk(value, f"{prefix}[{index}]"))
    return out
