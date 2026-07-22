"""PW0-aware field admission for the management-context producer (SPEC-MCTX
REQ-MCTX-6): "PII policy is applied before retrieval projection and response
assembly." This module never re-implements PW0 classification -- it calls the same
closed-taxonomy predicate ``omniscience_core.privacy.taxonomy.is_pw0_admitted`` that
gates ordinary ingestion, so a citation field is admitted into a management-context
bundle under the exact same rule that admits it into any other PW0-protected sink.
"""

from __future__ import annotations

from dataclasses import dataclass

from omniscience_core.privacy.receipts import compute_integrity
from omniscience_core.privacy.taxonomy import is_pw0_admitted


@dataclass(frozen=True)
class PiiFieldDecision:
    admitted: bool
    reason: str


def admit_field(*, data_class: str, transform_state: str) -> PiiFieldDecision:
    """Return whether one citation field may be assembled into the bundle."""
    if is_pw0_admitted(data_class, transform_state):
        return PiiFieldDecision(admitted=True, reason="pw0_admitted")
    return PiiFieldDecision(admitted=False, reason="pw0_not_admitted")


def build_pii_receipt(
    *, admitted_count: int, dropped_count: int, policy_revision: str, produced_at: str
) -> dict[str, object]:
    """Non-identifying receipt attached to every bundle -- counts and a digest only,
    never a raw value or field payload (mirrors ``privacy.receipts.BoundaryReceipt``)."""
    payload = {
        "admitted_count": admitted_count,
        "dropped_count": dropped_count,
        "policy_revision": policy_revision,
        "produced_at": produced_at,
    }
    return {**payload, "integrity": compute_integrity(payload)}
