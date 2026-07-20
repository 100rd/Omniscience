"""AC-DR-1 tests: backup-policy evidence must name every SPEC-SOT authority
table and carry immutable-copy ownership, retention, encryption, cross-
account/region copies, and a measured recovery point. Missing fields are RED
— see fixture ids policy-* in test_backup_restore_fixtures.py.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from qualify_backup_restore import (
    AUTHORITY_TABLES,
    check_backup_policy_evidence,
)
from qualify_backup_restore import main as cli_main


def _valid_policy() -> dict[str, object]:
    return {
        "authority_tables": sorted(AUTHORITY_TABLES),
        "immutable_copy": {
            "owner": "backup-team",
            "retention_days": 35,
            "encryption": "aws:kms:alias/omniscience-backup",
        },
        "cross_account_copy": {"account": "222233334444"},
        "cross_region_copy": {"region": "us-west-2"},
        "measured_recovery_point_seconds": 300,
    }


def test_policy_absent_is_red() -> None:
    result = check_backup_policy_evidence(None)
    assert result.status == "RED"
    assert result.reasons == ("policy_absent",)


def test_policy_not_an_object_is_red() -> None:
    result = check_backup_policy_evidence(["not", "an", "object"])
    assert result.status == "RED"
    assert result.reasons == ("policy_malformed:not_an_object",)


def test_policy_missing_one_authority_table_is_red() -> None:
    policy = _valid_policy()
    policy["authority_tables"] = sorted(AUTHORITY_TABLES - {"outbox_events"})
    result = check_backup_policy_evidence(policy)
    assert result.status == "RED"
    assert "authority_table_missing:outbox_events" in result.reasons


def test_policy_authority_tables_absent_lists_every_missing_table() -> None:
    policy = _valid_policy()
    del policy["authority_tables"]
    result = check_backup_policy_evidence(policy)
    assert result.status == "RED"
    assert "authority_tables_absent_or_malformed" in result.reasons
    for table in AUTHORITY_TABLES:
        assert f"authority_table_missing:{table}" in result.reasons


@pytest.mark.parametrize(
    "mutate,expected_reason",
    [
        (lambda p: p["immutable_copy"].pop("owner"), "immutable_copy_owner_absent"),
        (
            lambda p: p["immutable_copy"].__setitem__("retention_days", 0),
            "immutable_copy_retention_days_absent_or_invalid",
        ),
        (
            lambda p: p["immutable_copy"].__setitem__("retention_days", "35"),
            "immutable_copy_retention_days_absent_or_invalid",
        ),
        (lambda p: p["immutable_copy"].pop("encryption"), "immutable_copy_encryption_absent"),
        (lambda p: p.pop("immutable_copy"), "immutable_copy_absent_or_malformed"),
    ],
    ids=[
        "owner-absent",
        "retention-zero",
        "retention-wrong-type",
        "encryption-absent",
        "immutable-copy-absent",
    ],
)
def test_policy_immutable_copy_field_missing_is_red(mutate, expected_reason) -> None:
    policy = _valid_policy()
    mutate(policy)
    result = check_backup_policy_evidence(policy)
    assert result.status == "RED"
    assert expected_reason in result.reasons


def test_policy_missing_cross_account_copy_is_red() -> None:
    policy = _valid_policy()
    del policy["cross_account_copy"]
    result = check_backup_policy_evidence(policy)
    assert result.status == "RED"
    assert "cross_account_copy_absent" in result.reasons


def test_policy_missing_cross_region_copy_is_red() -> None:
    policy = _valid_policy()
    del policy["cross_region_copy"]
    result = check_backup_policy_evidence(policy)
    assert result.status == "RED"
    assert "cross_region_copy_absent" in result.reasons


def test_policy_missing_recovery_point_is_red() -> None:
    policy = _valid_policy()
    del policy["measured_recovery_point_seconds"]
    result = check_backup_policy_evidence(policy)
    assert result.status == "RED"
    assert "measured_recovery_point_absent_or_invalid" in result.reasons


def test_policy_negative_recovery_point_is_red() -> None:
    policy = _valid_policy()
    policy["measured_recovery_point_seconds"] = -1
    result = check_backup_policy_evidence(policy)
    assert result.status == "RED"
    assert "measured_recovery_point_absent_or_invalid" in result.reasons


def test_authority_tables_includes_governance_and_audit_history_tables() -> None:
    """REQ-SOT-1 'governance metadata required to rebuild projections' and
    REQ-SOT-8 'tenant and audit history' must be named explicitly in
    AUTHORITY_TABLES. This asserts against a hardcoded expectation, not
    against AUTHORITY_TABLES itself — unlike _valid_policy() (which derives
    its fixture from the same constant under test and so cannot catch an
    omission), this test fails if audit_log or entity_emitter is removed.
    """
    required_governance_and_audit_tables = {"audit_log", "entity_emitter"}
    missing = required_governance_and_audit_tables - AUTHORITY_TABLES
    assert not missing, (
        f"AUTHORITY_TABLES must include {missing} per REQ-SOT-1 (governance "
        "metadata) / REQ-SOT-8 (tenant and audit history)"
    )


def test_authority_tables_excludes_authentication_credential_table() -> None:
    """api_tokens is authentication/authorization credential material, not
    ledger content/lineage/entity-outbox-records/governance metadata that
    REQ-SOT-1/8 names — it is deliberately excluded from AUTHORITY_TABLES.
    """
    assert "api_tokens" not in AUTHORITY_TABLES


def test_policy_complete_evidence_is_green() -> None:
    result = check_backup_policy_evidence(_valid_policy())
    assert result.status == "GREEN"
    assert result.reasons == ()


def test_policy_multiple_missing_fields_all_collected() -> None:
    """All mismatches are collected, not short-circuited on the first one."""
    policy = {"authority_tables": []}
    result = check_backup_policy_evidence(policy)
    assert result.status == "RED"
    # every table + immutable_copy + cross-account + cross-region + recovery point
    assert len(result.reasons) >= len(AUTHORITY_TABLES) + 3


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write_json(tmp_path: Path, payload: object, name: str = "policy.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_cli_green_on_complete_policy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    policy_path = _write_json(tmp_path, _valid_policy())
    exit_code = cli_main(["--policy", str(policy_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "GREEN" in captured.out


def test_cli_red_on_absent_policy_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli_main(["--policy", str(tmp_path / "does-not-exist.json")])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "RED" in captured.err
    assert "Traceback" not in captured.err


def test_cli_no_checks_requested_is_red(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli_main([])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "no_check_requested" in captured.err
