"""Content-addressed fixture matrix for the backup/restore harness (AC-DR-6).

Single source of truth for the deterministic scenario ids exercised across
test_backup_restore_policy.py, test_backup_restore_approval.py,
test_backup_restore_refusal.py, and test_backup_restore_manifest.py. Mirrors
tests/conformance/consumer_severance/fixtures.py's content-addressed style so
CI can assert the exact fixture set exercised (AC-DR-6 "deterministic
fixtures exercise identity refusal, restore verification, projection
convergence, breach handling, and manifest validation"), not just "some
tests ran".

This module lists fixture *ids and expected decisions* only — the concrete
input construction lives in each scenario's own test file, next to the
assertions that exercise it, so the two stay in sync without a layer of
indirection between them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ScenarioFixture:
    id: str
    category: str  # "policy" | "approval" | "refusal" | "manifest"
    description: str
    expected_status: str  # "GREEN" | "RED"


FIXTURES: tuple[ScenarioFixture, ...] = (
    # AC-DR-1 backup-policy evidence
    ScenarioFixture("policy-absent", "policy", "no policy evidence supplied", "RED"),
    ScenarioFixture(
        "policy-missing-authority-table", "policy", "one SPEC-SOT table not named", "RED"
    ),
    ScenarioFixture(
        "policy-missing-immutable-copy", "policy", "owner/retention/encryption absent", "RED"
    ),
    ScenarioFixture("policy-missing-cross-account", "policy", "no cross-account copy", "RED"),
    ScenarioFixture("policy-missing-cross-region", "policy", "no cross-region copy", "RED"),
    ScenarioFixture(
        "policy-missing-recovery-point", "policy", "no measured recovery point", "RED"
    ),
    ScenarioFixture("policy-complete", "policy", "every required field present", "GREEN"),
    # AC-DR-2 destructive-command approval envelope
    ScenarioFixture("approval-absent", "approval", "no envelope supplied", "RED"),
    ScenarioFixture("approval-expired", "approval", "now is past expires_at", "RED"),
    ScenarioFixture(
        "approval-mismatched-command-digest", "approval", "digest != sha256(command)", "RED"
    ),
    ScenarioFixture(
        "approval-mismatched-environment-identity",
        "approval",
        "envelope identity != observed identity",
        "RED",
    ),
    ScenarioFixture("approval-replayed-nonce", "approval", "nonce already consumed", "RED"),
    ScenarioFixture("approval-valid", "approval", "every field matches, nonce fresh", "GREEN"),
    # AC-DR-3 destructive-safety refusal
    ScenarioFixture(
        "refusal-production-like-name", "refusal", "target name contains 'prod'", "RED"
    ),
    ScenarioFixture(
        "refusal-missing-disposable-tag", "refusal", "target tags lack disposable=true", "RED"
    ),
    ScenarioFixture(
        "refusal-non-empty-projections",
        "refusal",
        "qdrant/neo4j counts non-zero outside drill context",
        "RED",
    ),
    ScenarioFixture(
        "refusal-active-writer", "refusal", "writer lease source reports an active writer", "RED"
    ),
    ScenarioFixture(
        "refusal-ambiguous-credentials",
        "refusal",
        "credential resolver raises AmbiguousIdentityError",
        "RED",
    ),
    ScenarioFixture(
        "refusal-all-clear",
        "refusal",
        "disposable target, empty projections, no writers, unique identity",
        "GREEN",
    ),
    # AC-DR-5 drill manifest
    ScenarioFixture("manifest-all-absent", "manifest", "no measured inputs supplied", "RED"),
    ScenarioFixture("manifest-rto-exceeded", "manifest", "measured RTO over budget", "RED"),
    ScenarioFixture(
        "manifest-verification-failed", "manifest", "projection verification failed", "RED"
    ),
    ScenarioFixture(
        "manifest-hash-equivalence-mismatch", "manifest", "AP5 hash mismatch present", "RED"
    ),
    ScenarioFixture("manifest-query-probes-failed", "manifest", "query probes failed", "RED"),
    ScenarioFixture(
        "manifest-complete", "manifest", "every measured input present and passing", "GREEN"
    ),
)


def all_ids() -> frozenset[str]:
    return frozenset(f.id for f in FIXTURES)


def fixture_matrix_canonical_json() -> str:
    """Deterministic, content-addressable serialization of the fixture matrix."""
    payload = sorted(
        (
            {"id": f.id, "category": f.category, "expected_status": f.expected_status}
            for f in FIXTURES
        ),
        key=lambda row: row["id"],
    )
    return (
        json.dumps(
            payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    )


def fixture_matrix_sha256() -> str:
    return hashlib.sha256(fixture_matrix_canonical_json().encode("utf-8")).hexdigest()


def test_fixture_ids_are_unique() -> None:
    ids = [f.id for f in FIXTURES]
    assert len(ids) == len(set(ids))


def test_every_category_has_exactly_one_green_fixture() -> None:
    categories = {f.category for f in FIXTURES}
    for category in categories:
        greens = [f for f in FIXTURES if f.category == category and f.expected_status == "GREEN"]
        assert len(greens) == 1, f"category {category!r} must have exactly one GREEN fixture"


def test_fixture_matrix_sha256_is_stable_and_well_formed() -> None:
    digest_a = fixture_matrix_sha256()
    digest_b = fixture_matrix_sha256()
    assert digest_a == digest_b
    assert len(digest_a) == 64
    int(digest_a, 16)  # raises ValueError if not hex


def test_fixture_matrix_covers_expected_total_count() -> None:
    assert len(FIXTURES) == 25
