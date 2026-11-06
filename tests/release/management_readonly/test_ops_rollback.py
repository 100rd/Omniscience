"""AC-SP86-5: the release is observable, restart-safe, and reversible without
Omnius or a model/provider.

Covers: /health and /ready respond with the release's own contract closure
(health/readiness), the SP-81 producer is a pure function so a process
restart reproduces byte-identical output for identical input (restart-safe /
"projection convergence" never regresses to a favorable default on
incomplete evidence), and the release-lock digests
``scripts/qualify_management_readonly_release.py`` computes are themselves
exactly reproducible -- the prerequisite for any exact-digest rollback.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from httpx import AsyncClient
from omniscience_core.management import ManagementContextProducer

from tests.management.fixtures import (
    AUTHORIZATION,
    CONSUMER_PIN_MATCHING,
    MANIFEST,
    PARTIAL_QUALITY,
    PRODUCED_AT,
    REFERENCE_NOW,
    VALID_REQUEST,
    StaticEvidenceSource,
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


# ---------------------------------------------------------------------------
# Observable: /health, /ready, /metrics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ready_endpoint_reports_the_release_profile_id(client: AsyncClient) -> None:
    response = await client.get("/ready")
    assert response.status_code in (200, 503)
    body = response.json()
    assert body["profile_id"] == "management-readonly-v1"
    assert "checks" in body
    assert "status" in body


@pytest.mark.asyncio
async def test_ready_checks_include_the_release_contract_closure(client: AsyncClient) -> None:
    response = await client.get("/ready")
    checks = response.json()["checks"]
    assert set(checks) >= {
        "postgres",
        "nats",
        "mcp_contract",
        "management_context_contract",
        "pw0_privacy_contract",
    }


@pytest.mark.asyncio
async def test_ready_contract_checks_are_healthy_against_the_real_repo_contracts(
    client: AsyncClient,
) -> None:
    """The contracts/mcp, contracts/management, and contracts/pii pin.json
    files this task built/relies on are actually present and well-formed in
    this checkout -- a broken contracts/ closure must be visibly unhealthy,
    not silently 'unchecked'."""
    response = await client.get("/ready")
    checks = response.json()["checks"]
    assert checks["mcp_contract"] == "healthy"
    assert checks["management_context_contract"] == "healthy"
    assert checks["pw0_privacy_contract"] == "healthy"


@pytest.mark.asyncio
async def test_ready_never_returns_a_management_verdict_field(client: AsyncClient) -> None:
    """ADR-0022: the read-only surface exposes no management verdict, action
    recommendation, or authorization. /ready's body must stay confined to
    dependency-health vocabulary."""
    body = (await client.get("/ready")).json()
    forbidden_tokens = ("verdict", "recommendation", "approval", "authorize", "incident")
    serialized = str(body).lower()
    for token in forbidden_tokens:
        assert token not in serialized


@pytest.mark.asyncio
async def test_metrics_endpoint_is_mounted(client: AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Restart-safe: the producer is a pure function of its inputs
# ---------------------------------------------------------------------------


def test_producer_is_restart_safe_deterministic_replay() -> None:
    """Two independent ``ManagementContextProducer`` instances (modeling two
    process restarts) given byte-identical inputs must produce a
    byte-identical bundle -- no in-memory cache or process-local state can
    leak into the result (SPEC-MCTX REQ-MCTX-10 deterministic replay)."""
    first = ManagementContextProducer(contract_manifest=MANIFEST).produce(
        VALID_REQUEST,
        authorization=AUTHORIZATION,
        consumer_pin=CONSUMER_PIN_MATCHING,
        evidence_source=StaticEvidenceSource(),
        pii_policy_revision="1.0.0",
        reference_now=REFERENCE_NOW,
        produced_at=PRODUCED_AT,
    )
    second = ManagementContextProducer(contract_manifest=MANIFEST).produce(
        VALID_REQUEST,
        authorization=AUTHORIZATION,
        consumer_pin=CONSUMER_PIN_MATCHING,
        evidence_source=StaticEvidenceSource(),
        pii_policy_revision="1.0.0",
        reference_now=REFERENCE_NOW,
        produced_at=PRODUCED_AT,
    )
    assert first.is_success and second.is_success
    assert first.bundle.to_schema_dict() == second.bundle.to_schema_dict()


def test_incomplete_quality_evidence_never_fabricates_convergence() -> None:
    """'projection convergence' (KnowledgeQualitySnapshot.projection_status)
    must report 'unknown' on incomplete source evidence, never default to a
    favorable 'converged' -- a restart or a partially-initialized dependency
    can never present as fully converged."""
    result = ManagementContextProducer(contract_manifest=MANIFEST).produce(
        VALID_REQUEST,
        authorization=AUTHORIZATION,
        consumer_pin=CONSUMER_PIN_MATCHING,
        evidence_source=StaticEvidenceSource(quality=PARTIAL_QUALITY),
        pii_policy_revision="1.0.0",
        reference_now=REFERENCE_NOW,
        produced_at=PRODUCED_AT,
    )
    assert result.is_success
    assert result.bundle.quality.projection_status == "unknown"
    assert result.bundle.projection_state == "unknown"


# ---------------------------------------------------------------------------
# Exact-digest rollback: release-lock digests are exactly reproducible
# ---------------------------------------------------------------------------


def test_release_lock_digests_are_exactly_reproducible() -> None:
    """Rollback (contracts/releases/management-readonly-v1/rollback.json)
    restores a prior release by its exact digests -- that only works if
    re-running the qualify harness against the same tree always reproduces
    byte-identical digests. This is the prerequisite property, exercised
    directly (not merely assumed)."""
    first_lock, _ = qualify.build_release_lock()
    second_lock, _ = qualify.build_release_lock()

    digest_fields = (
        "image_digest",
        "chart_digest",
        "configuration_digest",
        "mcp_manifest_digest",
        "management_context_manifest_digest",
        "schema_set_digest",
        "pii_policy_digest",
        "severance_fixture_digest",
        "language_neutral_consumer_kit_digest",
    )
    for field in digest_fields:
        assert first_lock[field] == second_lock[field], f"{field} is not reproducible"


def test_rollback_contract_names_a_typed_probe_and_procedure() -> None:
    rollback_path = ROOT / "contracts" / "releases" / "management-readonly-v1" / "rollback.json"
    rollback = json.loads(rollback_path.read_text(encoding="utf-8"))
    assert rollback["kind"] == "revert-pr"
    assert rollback["probe"] == (
        "restore-prior-omniscience-release-digests-and-reject-unqualified-candidate"
    )
    assert isinstance(rollback["procedure"], list) and rollback["procedure"]
