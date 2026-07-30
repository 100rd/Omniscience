"""AC-SP95-3: the local service contract exposes only exact tenant/workspace/
purpose, read-only Management Read-Only surfaces, and a closed fail-closed
matrix -- and each declared fail-closed reason is reproduced by the live SP-81
producer (positive read passes; every adverse case fails closed without a
donated bundle).
"""

from __future__ import annotations

import json
from pathlib import Path

from omniscience_core.management import ManagementContextProducer

from tests.management.fixtures import (
    AUTHORIZATION,
    CONSUMER_PIN_MAJOR_MISMATCH,
    CONSUMER_PIN_MANIFEST_MISMATCH,
    CONSUMER_PIN_MATCHING,
    CONSUMER_PIN_SCHEMA_MISMATCH,
    EVIDENCE_CUT_FUTURE,
    MANIFEST,
    PRODUCED_AT,
    REFERENCE_NOW,
    VALID_REQUEST,
    RaisingEvidenceSource,
    StaticEvidenceSource,
    make_request,
)

ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = (
    ROOT / "contracts" / "releases" / "management-readonly-local-v1" / "service-contract.json"
)
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
PRODUCER = ManagementContextProducer(contract_manifest=MANIFEST)


def _produce(request, *, evidence_source=None, consumer_pin=CONSUMER_PIN_MATCHING):
    return PRODUCER.produce(
        request,
        authorization=AUTHORIZATION,
        consumer_pin=consumer_pin,
        evidence_source=evidence_source or StaticEvidenceSource(),
        pii_policy_revision="1.0.0",
        reference_now=REFERENCE_NOW,
        produced_at=PRODUCED_AT,
    )


def _actual_reason(case: str) -> tuple[str, object]:
    """Return (reason, result) the live producer yields for a matrix case."""
    if case == "foreign_workspace":
        r = _produce(make_request(workspace_id="dev-fixture-workspace-FOREIGN"))
        return r.denial.reason, r
    if case == "unauthorized_purpose":
        r = _produce(make_request(purpose="dev-fixture-purpose-UNAUTHORIZED"))
        return r.denial.reason, r
    if case == "generic_or_widened_query":
        r = _produce(make_request(requested_field_classes=("citations", "coverage")))
        return r.denial.reason, r
    if case == "future_evidence_cut":
        r = _produce(make_request(evidence_cut=EVIDENCE_CUT_FUTURE))
        return r.denial.reason, r
    if case == "stale_evidence":
        r = _produce(make_request(max_age_seconds=60))
        return r.fallback.reason, r
    if case == "severed_dependency":
        r = _produce(VALID_REQUEST, evidence_source=StaticEvidenceSource(healthy=False))
        return r.fallback.reason, r
    if case == "source_loss_on_exception":
        r = _produce(VALID_REQUEST, evidence_source=RaisingEvidenceSource(fail_on_health=True))
        return r.fallback.reason, r
    if case == "contract_major_skew":
        r = _produce(VALID_REQUEST, consumer_pin=CONSUMER_PIN_MAJOR_MISMATCH)
        return r.fallback.reason, r
    if case == "contract_schema_skew":
        r = _produce(VALID_REQUEST, consumer_pin=CONSUMER_PIN_SCHEMA_MISMATCH)
        return r.fallback.reason, r
    if case == "contract_manifest_skew":
        r = _produce(VALID_REQUEST, consumer_pin=CONSUMER_PIN_MANIFEST_MISMATCH)
        return r.fallback.reason, r
    raise AssertionError(f"unmapped matrix case {case}")


def test_contract_identity_and_non_authority_fields() -> None:
    assert CONTRACT["schema"] == "management-readonly-local-v1-service-contract-v1"
    assert CONTRACT["profile_id"] == "management-readonly-local-v1"
    assert CONTRACT["base_release"]["auth_token_profile"] == "omniscience-mcp-read-v1"
    assert CONTRACT["availability_class"] == "development-single-host"
    assert CONTRACT["ha_qualified"] is False
    assert CONTRACT["activation_authority"] == "none"


def test_all_exposed_surfaces_are_read_only() -> None:
    surfaces = CONTRACT["exposed_read_surfaces"]
    assert surfaces
    assert all(s["kind"] == "read" for s in surfaces)


def test_negative_space_forbids_write_action_and_provider() -> None:
    neg = set(CONTRACT["negative_space"])
    for forbidden in (
        "owner_write_route",
        "action_or_control_or_effect_route",
        "generic_unbound_query",
        "omnius_runtime_or_mock",
        "external_model_or_provider_call",
    ):
        assert forbidden in neg


def test_positive_bound_read_passes() -> None:
    result = _produce(VALID_REQUEST)
    assert result.is_success and result.bundle is not None


def test_every_declared_fail_closed_case_matches_live_producer() -> None:
    matrix = {row["case"]: row for row in CONTRACT["fail_closed_matrix"]}
    assert matrix, "fail_closed_matrix is empty"
    invariant = set(CONTRACT["fail_closed_invariant"]["must_not_validate_as"])
    assert invariant == {"healthy", "empty", "cached_current"}
    for case, row in matrix.items():
        actual_reason, result = _actual_reason(case)
        assert actual_reason == row["reason"], (
            f"{case}: contract declares {row['reason']!r} but producer returned {actual_reason!r}"
        )
        # A denial/fallback never carries a populated bundle (no field donation).
        assert not result.is_success
        assert result.bundle is None
