"""AC-DR-5 tests: drill manifest construction and validation.

Absent or breached targets remain RED, never a placeholder — see fixture
ids manifest-* in test_backup_restore_fixtures.py.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from dr_verify import DRVerificationResult, RtoResult
from qualify_backup_restore import (
    build_manifest,
    validate_manifest_json,
)
from qualify_backup_restore import main as cli_main

_WS = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")


def _passing_rto() -> RtoResult:
    return RtoResult(elapsed_seconds=300.0, budget_seconds=900, exceeded=False)


def _breached_rto() -> RtoResult:
    return RtoResult(elapsed_seconds=1200.0, budget_seconds=900, exceeded=True)


def _passing_verification() -> DRVerificationResult:
    return DRVerificationResult(workspace_id=_WS, mismatches=[])


def _failing_verification() -> DRVerificationResult:
    return DRVerificationResult(
        workspace_id=_WS,
        mismatches=["Qdrant chunk count mismatch for source X"],
    )


def _consistent_manifest_kwargs() -> dict[str, object]:
    return {
        "drill_started_at": "2026-07-20T00:00:00Z",
        "drill_completed_at": "2026-07-20T00:05:00Z",
        "measured_rpo_seconds": 30.0,
        "rto_result": _passing_rto(),
        "source_backup_id": "backup-123",
        "restored_environment_identity": "recovery-env-alpha",
        "verification_result": _passing_verification(),
        "hash_equivalence_mismatches": [],
        "query_probes_passed": True,
    }


class TestBuildManifest:
    def test_all_absent_inputs_is_red_with_reasons(self) -> None:
        manifest = build_manifest(
            drill_started_at=None,
            drill_completed_at=None,
            measured_rpo_seconds=None,
            rto_result=None,
            source_backup_id=None,
            restored_environment_identity=None,
            verification_result=None,
            hash_equivalence_mismatches=None,
            query_probes_passed=None,
        )
        assert manifest.status == "RED"
        assert "drill_started_at_absent" in manifest.reasons
        assert "rto_result_absent" in manifest.reasons
        assert "verification_result_absent" in manifest.reasons
        assert "hash_equivalence_result_absent" in manifest.reasons
        assert "query_probes_absent" in manifest.reasons
        # honest absence, never a fabricated placeholder value
        assert manifest.source_backup_id is None
        assert manifest.restored_environment_identity is None

    def test_rto_exceeded_is_red(self) -> None:
        kwargs = _consistent_manifest_kwargs()
        kwargs["rto_result"] = _breached_rto()
        manifest = build_manifest(**kwargs)
        assert manifest.status == "RED"
        assert manifest.rto_exceeded is True
        assert any(r.startswith("rto_exceeded:") for r in manifest.reasons)

    def test_verification_failed_is_red(self) -> None:
        kwargs = _consistent_manifest_kwargs()
        kwargs["verification_result"] = _failing_verification()
        manifest = build_manifest(**kwargs)
        assert manifest.status == "RED"
        assert manifest.verification_passed is False
        assert "verification_failed" in manifest.reasons
        assert manifest.verification_mismatches == ("Qdrant chunk count mismatch for source X",)

    def test_hash_equivalence_mismatch_is_red(self) -> None:
        kwargs = _consistent_manifest_kwargs()
        kwargs["hash_equivalence_mismatches"] = ["chunk abc: vector_digest mismatch"]
        manifest = build_manifest(**kwargs)
        assert manifest.status == "RED"
        assert "hash_equivalence_mismatch" in manifest.reasons

    def test_query_probes_failed_is_red(self) -> None:
        kwargs = _consistent_manifest_kwargs()
        kwargs["query_probes_passed"] = False
        manifest = build_manifest(**kwargs)
        assert manifest.status == "RED"
        assert "query_probes_failed" in manifest.reasons

    def test_negative_rpo_is_red(self) -> None:
        kwargs = _consistent_manifest_kwargs()
        kwargs["measured_rpo_seconds"] = -5.0
        manifest = build_manifest(**kwargs)
        assert manifest.status == "RED"
        assert "measured_rpo_seconds_absent_or_invalid" in manifest.reasons

    def test_full_consistent_manifest_is_green(self) -> None:
        manifest = build_manifest(**_consistent_manifest_kwargs())
        assert manifest.status == "GREEN"
        assert manifest.reasons == ()
        assert manifest.rto_exceeded is False
        assert manifest.verification_passed is True

    def test_manifest_id_is_content_addressed(self) -> None:
        manifest_a = build_manifest(**_consistent_manifest_kwargs())
        manifest_b = build_manifest(**_consistent_manifest_kwargs())
        assert manifest_a.manifest_id == manifest_b.manifest_id
        assert manifest_a.manifest_id.startswith("sha256:")

        kwargs_c = _consistent_manifest_kwargs()
        kwargs_c["source_backup_id"] = "backup-999"
        manifest_c = build_manifest(**kwargs_c)
        assert manifest_c.manifest_id != manifest_a.manifest_id

    def test_to_json_round_trips_through_json_loads(self) -> None:
        manifest = build_manifest(**_consistent_manifest_kwargs())
        parsed = json.loads(manifest.to_json())
        assert parsed["status"] == "GREEN"
        assert parsed["manifest_id"] == manifest.manifest_id


class TestValidateManifestJson:
    def _green_manifest_dict(self) -> dict[str, object]:
        return build_manifest(**_consistent_manifest_kwargs()).to_dict()

    def test_absent_manifest_is_red(self) -> None:
        result = validate_manifest_json(None)
        assert result.status == "RED"
        assert result.reasons == ("manifest_absent",)

    def test_not_an_object_is_red(self) -> None:
        result = validate_manifest_json([1, 2, 3])
        assert result.status == "RED"
        assert result.reasons == ("manifest_malformed:not_an_object",)

    @pytest.mark.parametrize(
        "field",
        [
            "manifest_id",
            "drill_started_at",
            "drill_completed_at",
            "measured_rpo_seconds",
            "measured_rto_seconds",
            "rto_budget_seconds",
            "rto_exceeded",
            "source_backup_id",
            "restored_environment_identity",
            "verification_passed",
            "query_probes_passed",
            "status",
        ],
    )
    def test_missing_required_field_is_red(self, field: str) -> None:
        manifest = self._green_manifest_dict()
        del manifest[field]
        result = validate_manifest_json(manifest)
        assert result.status == "RED"
        assert f"manifest_field_absent:{field}" in result.reasons

    def test_declared_red_status_is_red(self) -> None:
        manifest = self._green_manifest_dict()
        manifest["status"] = "RED"
        result = validate_manifest_json(manifest)
        assert result.status == "RED"
        assert "manifest_status_not_green:'RED'" in result.reasons

    def test_forged_green_status_with_rto_exceeded_is_still_red(self) -> None:
        """A manifest that self-declares GREEN but carries rto_exceeded=True
        must not be trusted on its word — tamper-evidence, not just shape."""
        manifest = self._green_manifest_dict()
        manifest["rto_exceeded"] = True
        result = validate_manifest_json(manifest)
        assert result.status == "RED"
        assert "manifest_rto_exceeded" in result.reasons

    def test_forged_green_status_with_verification_failed_is_still_red(self) -> None:
        manifest = self._green_manifest_dict()
        manifest["verification_passed"] = False
        result = validate_manifest_json(manifest)
        assert result.status == "RED"
        assert "manifest_verification_failed" in result.reasons

    def test_forged_green_status_with_query_probes_failed_is_still_red(self) -> None:
        """AC-DR-5 lists query probes as one of the four evidence types
        (manifest, timestamps, hashes, query probes) — a manifest that
        self-declares GREEN but carries query_probes_passed=False must not
        be trusted on its word, same as rto_exceeded/verification_passed."""
        manifest = self._green_manifest_dict()
        manifest["query_probes_passed"] = False
        result = validate_manifest_json(manifest)
        assert result.status == "RED"
        assert "manifest_query_probes_failed" in result.reasons

    def test_forged_green_status_with_query_probes_absent_is_still_red(self) -> None:
        """An absent query_probes_passed field (never ran the probes) must
        not silently validate as GREEN either."""
        manifest = self._green_manifest_dict()
        del manifest["query_probes_passed"]
        result = validate_manifest_json(manifest)
        assert result.status == "RED"
        assert "manifest_field_absent:query_probes_passed" in result.reasons

    def test_valid_green_manifest_passes(self) -> None:
        result = validate_manifest_json(self._green_manifest_dict())
        assert result.status == "GREEN"
        assert result.reasons == ()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write_json(tmp_path: Path, payload: object, name: str = "manifest.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_cli_green_on_valid_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest_dict = build_manifest(**_consistent_manifest_kwargs()).to_dict()
    manifest_path = _write_json(tmp_path, manifest_dict)
    exit_code = cli_main(["--manifest", str(manifest_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "GREEN" in captured.out


def test_cli_red_on_breached_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    kwargs = _consistent_manifest_kwargs()
    kwargs["rto_result"] = _breached_rto()
    manifest_dict = build_manifest(**kwargs).to_dict()
    manifest_path = _write_json(tmp_path, manifest_dict)
    exit_code = cli_main(["--manifest", str(manifest_path)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "manifest_status_not_green" in captured.err or "manifest_rto_exceeded" in captured.err
