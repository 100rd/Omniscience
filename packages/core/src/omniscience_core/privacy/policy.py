"""Policy-skew detection (ADR-0018 D1/D11, SPEC-PII REQ-PII-1, fallback P-PII-7+8).

Same semantics as the ground-truth SP-60 ``pii_contract_validator.check_policy_skew``
(see ``contracts/pii/README.md`` -- Omniscience binds the *shape*, not the Python code,
so this is an independently authored function enforcing the identical rule set: an
unknown, stale, unsigned, or incompatible policy blocks new protected processing).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConsumerPolicyPin:
    """What this Omniscience deployment currently trusts -- compared against the live
    ``PIIPolicyBundle`` on every gate evaluation, never assumed still valid."""

    component_id: str
    schema_major: int
    policy_revision_pin: str
    signer_trust_profile: str


def check_policy_skew(
    consumer_pin: ConsumerPolicyPin, bundle: Mapping[str, Any], *, reference_now: str
) -> list[str]:
    """Return non-empty errors when the pinned schema major, policy revision, signer
    trust profile, or bundle validity window no longer match. Any error blocks new
    processing; it never erases receipts already produced under a prior-valid bundle.
    """
    errors: list[str] = []

    compatibility = bundle.get("compatibility", {})
    min_major = compatibility.get("min_supported_major")
    max_major = compatibility.get("max_supported_major")
    if (
        min_major is None
        or max_major is None
        or not (min_major <= consumer_pin.schema_major <= max_major)
    ):
        errors.append(
            f"skew: consumer schema_major {consumer_pin.schema_major!r} outside "
            f"bundle-supported range [{min_major!r}, {max_major!r}]"
        )

    if consumer_pin.policy_revision_pin != bundle.get("policy_revision"):
        errors.append(
            f"skew: consumer policy_revision_pin {consumer_pin.policy_revision_pin!r} "
            f"!= current bundle policy_revision {bundle.get('policy_revision')!r}"
        )

    signer = bundle.get("signer", {})
    if consumer_pin.signer_trust_profile != signer.get("key_id"):
        errors.append(
            f"skew: consumer signer_trust_profile {consumer_pin.signer_trust_profile!r} "
            f"!= bundle signer key_id {signer.get('key_id')!r}"
        )

    issued_at = bundle.get("issued_at", "")
    expires_at = bundle.get("expires_at", "")
    if issued_at and reference_now < issued_at:
        errors.append(f"skew: bundle not yet valid (issued_at {issued_at!r})")
    if expires_at and reference_now > expires_at:
        errors.append(f"skew: bundle expired (expires_at {expires_at!r})")

    return errors
