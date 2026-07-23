"""Per-store lifecycle aggregation (ADR-0018 D8, SPEC-PII REQ-PII-9, AC-SP61-3).

Same closed-taxonomy aggregation semantics as the ground-truth SP-60
``pii_contract_validator.aggregate_lifecycle`` (see ``contracts/pii/README.md``):
the aggregate disposition is *derived* from per-store evidence, never accepted as a
caller-supplied claim, so a partial deletion/restore cannot present as "complete".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from omniscience_core.privacy.contract_schemas import validate_against_schema
from omniscience_core.privacy.receipts import compute_integrity

TERMINAL_COMPLETE_DISPOSITIONS = frozenset({"complete", "excluded"})

BLOCKING_DISPOSITION_SEVERITY: tuple[str, ...] = (
    "unavailable",
    "failed",
    "backup_pending",
    "immutable_retention",
    "pending",
)


def aggregate_lifecycle(store_states: Sequence[Mapping[str, str]]) -> str:
    """Honest aggregate disposition for a set of per-store lifecycle states.

    Returns ``"complete"`` only when every store is ``complete`` or ``excluded`` with
    at least one ``complete``. Otherwise returns the most-severe blocking disposition
    present. Empty ``store_states`` has no evidence of anything, so it is
    ``"unavailable"``, never ``"complete"``.
    """
    if not store_states:
        return "unavailable"
    dispositions = [state["disposition"] for state in store_states]
    all_terminal = all(d in TERMINAL_COMPLETE_DISPOSITIONS for d in dispositions)
    if all_terminal and "complete" in dispositions:
        return "complete"
    for candidate in BLOCKING_DISPOSITION_SEVERITY:
        if candidate in dispositions:
            return candidate
    return "unavailable"


def check_lifecycle_claim(
    claimed_disposition: str, store_states: Sequence[Mapping[str, str]]
) -> list[str]:
    """Reject a claimed aggregate disposition that collapses partial coverage."""
    errors: list[str] = []
    if claimed_disposition in ("deleted", "clean"):
        errors.append(
            f"lifecycle collapse: non-conformant disposition literal {claimed_disposition!r}; "
            "only the closed REQ-PII-9 disposition values are permitted"
        )
        return errors
    honest = aggregate_lifecycle(store_states)
    if claimed_disposition != honest:
        errors.append(
            f"lifecycle collapse: disposition claims {claimed_disposition!r} but "
            f"store_states only support {honest!r}"
        )
    return errors


@dataclass(frozen=True)
class DeletionReceipt:
    request_id: str
    tenant_id: str
    policy_revision: str
    store_class: str
    covered_artifacts: tuple[str, ...]
    excluded_or_pending: tuple[str, ...]
    disposition: str
    store_states: tuple[Mapping[str, str], ...]
    produced_at: str
    producer: str
    integrity: Mapping[str, str]
    workspace_id: str | None = None
    subject_ref: str | None = None

    def to_schema_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "request_id": self.request_id,
            "tenant_id": self.tenant_id,
            "policy_revision": self.policy_revision,
            "store_class": self.store_class,
            "covered_artifacts": list(self.covered_artifacts),
            "excluded_or_pending": list(self.excluded_or_pending),
            "disposition": self.disposition,
            "store_states": [dict(state) for state in self.store_states],
            "produced_at": self.produced_at,
            "producer": self.producer,
            "integrity": dict(self.integrity),
        }
        if self.workspace_id is not None:
            payload["workspace_id"] = self.workspace_id
        if self.subject_ref is not None:
            payload["subject_ref"] = self.subject_ref
        return payload


def build_deletion_receipt(
    *,
    request_id: str,
    tenant_id: str,
    policy_revision: str,
    store_class: str,
    store_states: Sequence[Mapping[str, str]],
    produced_at: str,
    producer: str,
    workspace_id: str | None = None,
    subject_ref: str | None = None,
) -> DeletionReceipt:
    """Build a ``DeletionReceipt`` whose ``disposition`` is always the honest
    aggregate of ``store_states`` -- callers have no parameter to force ``complete``
    while a required store is pending or unavailable (AC-SP61-3)."""
    disposition = aggregate_lifecycle(store_states)
    covered = tuple(
        state["store"]
        for state in store_states
        if state["disposition"] in TERMINAL_COMPLETE_DISPOSITIONS
    )
    pending = tuple(
        state["store"]
        for state in store_states
        if state["disposition"] not in TERMINAL_COMPLETE_DISPOSITIONS
    )
    base_payload = {
        "request_id": request_id,
        "tenant_id": tenant_id,
        "policy_revision": policy_revision,
        "store_class": store_class,
        "covered_artifacts": list(covered),
        "excluded_or_pending": list(pending),
        "disposition": disposition,
        "store_states": [dict(state) for state in store_states],
        "produced_at": produced_at,
        "producer": producer,
    }
    integrity = compute_integrity(base_payload)
    receipt = DeletionReceipt(
        request_id=request_id,
        tenant_id=tenant_id,
        policy_revision=policy_revision,
        store_class=store_class,
        covered_artifacts=covered,
        excluded_or_pending=pending,
        disposition=disposition,
        store_states=tuple(dict(state) for state in store_states),
        produced_at=produced_at,
        producer=producer,
        integrity=integrity,
        workspace_id=workspace_id,
        subject_ref=subject_ref,
    )
    errors = validate_against_schema(receipt.to_schema_dict(), "deletion-receipt.schema.json")
    if errors:
        raise ValueError("; ".join(errors))
    return receipt
