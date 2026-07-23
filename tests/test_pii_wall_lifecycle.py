"""AC-SP61-3 (P-PII-5+6): lifecycle receipts remain per-store and partial.

A DeletionReceipt's aggregate disposition is always derived from per-store evidence;
it can never claim "complete" (or a non-conformant "deleted"/"clean") while any
required store is pending, failed, backup_pending, immutable_retention, or unavailable.
"""

from __future__ import annotations

import pytest
from omniscience_core.privacy import build_deletion_receipt, check_lifecycle_claim
from omniscience_core.privacy.lifecycle import aggregate_lifecycle


def test_all_stores_complete_yields_complete_receipt() -> None:
    receipt = build_deletion_receipt(
        request_id="dev-fixture-deletion-001",
        tenant_id="dev-fixture-tenant-a",
        policy_revision="1.0.0",
        store_class="knowledge-store",
        store_states=[
            {"store": "postgres-doc-001", "disposition": "complete"},
            {"store": "qdrant-vector-001", "disposition": "complete"},
        ],
        produced_at="2026-06-02T00:00:00Z",
        producer="dev-fixture-producer-omniscience-lifecycle",
    )
    assert receipt.disposition == "complete"
    assert set(receipt.covered_artifacts) == {"postgres-doc-001", "qdrant-vector-001"}
    assert receipt.excluded_or_pending == ()


@pytest.mark.parametrize(
    ("blocking_disposition", "expected_aggregate"),
    [
        ("pending", "pending"),
        ("failed", "failed"),
        ("backup_pending", "backup_pending"),
        ("immutable_retention", "immutable_retention"),
        ("unavailable", "unavailable"),
    ],
)
def test_partial_coverage_never_collapses_to_complete(
    blocking_disposition: str, expected_aggregate: str
) -> None:
    receipt = build_deletion_receipt(
        request_id="dev-fixture-deletion-002",
        tenant_id="dev-fixture-tenant-a",
        policy_revision="1.0.0",
        store_class="knowledge-store",
        store_states=[
            {"store": "postgres-doc-002", "disposition": "complete"},
            {"store": "qdrant-vector-002", "disposition": "complete"},
            {"store": "neo4j-graph-002", "disposition": blocking_disposition},
        ],
        produced_at="2026-06-02T00:00:00Z",
        producer="dev-fixture-producer-omniscience-lifecycle",
    )
    assert receipt.disposition == expected_aggregate
    assert receipt.disposition != "complete"
    assert "neo4j-graph-002" in receipt.excluded_or_pending


def test_empty_store_states_aggregate_to_unavailable_not_complete() -> None:
    assert aggregate_lifecycle([]) == "unavailable"


def test_claiming_complete_over_partial_store_states_is_rejected() -> None:
    store_states = [
        {"store": "postgres-doc-906", "disposition": "complete"},
        {"store": "qdrant-vector-906", "disposition": "complete"},
        {"store": "neo4j-graph-906", "disposition": "failed"},
    ]
    errors = check_lifecycle_claim("complete", store_states)
    assert errors != []


@pytest.mark.parametrize("non_conformant_literal", ["deleted", "clean"])
def test_non_conformant_disposition_literals_are_rejected(non_conformant_literal: str) -> None:
    store_states = [{"store": "postgres-doc-999", "disposition": "complete"}]
    errors = check_lifecycle_claim(non_conformant_literal, store_states)
    assert errors != []


def test_honest_claim_matching_store_states_is_accepted() -> None:
    store_states = [{"store": "postgres-doc-777", "disposition": "pending"}]
    assert check_lifecycle_claim("pending", store_states) == []
