"""AC-SP86-4: consumer severance fixture kit + read-only verifier.

Loss, token-expiry, freshness-expiry, and contract-skew fixtures each require
a typed ``fallback_required`` decision and can never validate as a false
healthy/empty/cached-current outcome. The first three scenarios are driven
against the live SP-81 ``ManagementContextProducer`` (loss and
freshness-expiry) or asserted as hand-authored ground truth consistent with
the closed ``FALLBACK_REASONS`` taxonomy (token-expiry, which the current
read-only producer surface does not itself mint -- see fixture note below);
the read-only receipt verifier is exercised against both a conforming and a
tampered receipt.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from omniscience_core.management import ManagementContextProducer

from tests.management.fixtures import (
    AUTHORIZATION,
    CONSUMER_PIN_MAJOR_MISMATCH,
    CONSUMER_PIN_MANIFEST_MISMATCH,
    CONSUMER_PIN_MATCHING,
    CONSUMER_PIN_SCHEMA_MISMATCH,
    MANIFEST,
    PRODUCED_AT,
    REFERENCE_NOW,
    VALID_REQUEST,
    RaisingEvidenceSource,
    StaticEvidenceSource,
    make_request,
)
from tests.release.management_readonly.severance_fixtures import (
    all_fixtures,
    all_ids,
    expected_decisions,
    fixture_matrix_sha256,
    fixtures_by_scenario,
)

ROOT = Path(__file__).resolve().parents[3]
_SPEC = importlib.util.spec_from_file_location(
    "qualify_management_readonly_release",
    ROOT / "scripts" / "qualify_management_readonly_release.py",
)
assert _SPEC is not None and _SPEC.loader is not None
qualify = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = qualify
_SPEC.loader.exec_module(qualify)

PRODUCER = ManagementContextProducer(contract_manifest=MANIFEST)


def _produce(request, *, evidence_source, consumer_pin=CONSUMER_PIN_MATCHING):
    return PRODUCER.produce(
        request,
        authorization=AUTHORIZATION,
        consumer_pin=consumer_pin,
        evidence_source=evidence_source,
        pii_policy_revision="1.0.0",
        reference_now=REFERENCE_NOW,
        produced_at=PRODUCED_AT,
    )


def test_fixture_kit_covers_every_required_scenario() -> None:
    scenarios = {row["scenario"] for row in all_fixtures()}
    assert scenarios == {"loss", "token_expiry", "freshness_expiry", "contract_skew"}


def test_every_fixture_declares_fallback_required_and_never_a_favorable_claim() -> None:
    for row in all_fixtures():
        assert row["expected_decision"] == "fallback_required"
        assert set(row["must_not_validate_as"]) == {"healthy", "empty", "cached_current"}


def test_loss_fixtures_reproduce_against_the_live_producer() -> None:
    for row in fixtures_by_scenario("loss"):
        source = (
            StaticEvidenceSource(healthy=False, unhealthy_reason=row["id"])
            if row["id"] == "sev-loss-002"
            else RaisingEvidenceSource(fail_on_health=True)
        )
        result = _produce(VALID_REQUEST, evidence_source=source)
        assert not result.is_success
        assert result.bundle is None
        assert result.fallback is not None
        assert result.fallback.reason == row["reason"]


def test_freshness_expiry_fixture_reproduces_against_the_live_producer() -> None:
    (row,) = fixtures_by_scenario("freshness_expiry")
    request = make_request(max_age_seconds=60)
    result = _produce(request, evidence_source=StaticEvidenceSource())
    assert not result.is_success
    assert result.bundle is None
    assert result.fallback.reason == row["reason"]


def test_contract_skew_fixtures_reproduce_against_the_live_producer() -> None:
    pins = {
        "contract_major_mismatch": CONSUMER_PIN_MAJOR_MISMATCH,
        "schema_digest_mismatch": CONSUMER_PIN_SCHEMA_MISMATCH,
        "producer_manifest_mismatch": CONSUMER_PIN_MANIFEST_MISMATCH,
    }
    for row in fixtures_by_scenario("contract_skew"):
        result = _produce(
            VALID_REQUEST, evidence_source=StaticEvidenceSource(), consumer_pin=pins[row["reason"]]
        )
        assert not result.is_success
        assert result.bundle is None
        assert result.fallback.reason == row["reason"]


def test_token_expiry_reason_is_a_recognized_closed_taxonomy_value() -> None:
    """The current read-only surface has no live token-revocation call path to
    drive end-to-end (auth/token lifecycle is SP-10's bootstrap-profile
    concern, REQ-MCP-9), so this fixture is hand-authored ground truth -- same
    posture as ``tests/conformance/consumer_severance/fixtures.py``'s
    canary-derived fixtures. It must still be a real, closed-taxonomy
    ``FALLBACK_REASONS`` value, not an invented string."""
    from omniscience_core.management import FALLBACK_REASONS

    (row,) = fixtures_by_scenario("token_expiry")
    assert row["reason"] in FALLBACK_REASONS
    assert row["reason"] == "token_revoked"


def test_verify_severance_receipt_accepts_a_conforming_receipt() -> None:
    receipt = {
        "results": [
            {"id": fid, "decision": decision} for fid, decision in expected_decisions().items()
        ]
    }
    result = qualify.verify_severance_receipt(receipt=receipt)
    assert result.is_green, result.reasons


def test_verify_severance_receipt_rejects_a_false_healthy_claim() -> None:
    decisions = dict(expected_decisions())
    tampered_id = next(iter(decisions))
    decisions[tampered_id] = "healthy"
    receipt = {
        "results": [{"id": fid, "decision": decision} for fid, decision in decisions.items()]
    }
    result = qualify.verify_severance_receipt(receipt=receipt)
    assert not result.is_green
    assert any("decision_mismatch" in reason for reason in result.reasons)


def test_verify_severance_receipt_rejects_partial_coverage() -> None:
    decisions = expected_decisions()
    partial_id = next(iter(decisions))
    receipt = {"results": [{"id": partial_id, "decision": decisions[partial_id]}]}
    result = qualify.verify_severance_receipt(receipt=receipt)
    assert not result.is_green
    assert any("receipt_partial" in reason for reason in result.reasons)


def test_verify_severance_receipt_rejects_an_absent_receipt() -> None:
    result = qualify.verify_severance_receipt(receipt=None)
    assert not result.is_green
    assert result.reasons == ("receipt_absent",)


def test_fixture_matrix_digest_is_stable_and_content_addressed() -> None:
    assert fixture_matrix_sha256() == fixture_matrix_sha256()
    assert len(fixture_matrix_sha256()) == 64
    assert len(all_ids()) == len(all_fixtures())
