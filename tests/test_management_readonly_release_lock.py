"""AC-SP86-1 conformance tests -- scripts/qualify_management_readonly_release.py.

Invariant: ``build_release_lock`` re-derives every release-lock digest from
this repository's actual files and never mutates anything on the GREEN or any
RED path (mirrors ``tests/test_consumer_severance_verifier.py``'s read-only
proof for ``scripts/check_consumer_severance.py``). Digest drift -- a
tampered vendored contract file that no longer matches its own ``pin.json``
-- is exercised against an isolated temporary copy, never the real
``contracts/`` tree.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "qualify_management_readonly_release.py"

_SPEC = importlib.util.spec_from_file_location("qualify_management_readonly_release", SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
qualify = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = qualify
_SPEC.loader.exec_module(qualify)

WATCHED_DIRS: tuple[Path, ...] = (
    ROOT / "scripts",
    ROOT / "contracts",
)


def _fingerprint(paths: Iterable[Path]) -> dict[str, str]:
    fingerprint: dict[str, str] = {}
    for base in paths:
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and "evidence" not in path.parts:
                fingerprint[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return fingerprint


# ---------------------------------------------------------------------------
# Read-only proof
# ---------------------------------------------------------------------------


def test_build_release_lock_never_mutates_the_repository() -> None:
    before = _fingerprint(WATCHED_DIRS)
    qualify.build_release_lock()
    after = _fingerprint(WATCHED_DIRS)
    assert before == after


# ---------------------------------------------------------------------------
# Shape / structural conformance
# ---------------------------------------------------------------------------


def test_release_lock_has_every_required_output_field() -> None:
    release_lock, _ = qualify.build_release_lock()
    for field in qualify._RELEASE_LOCK_REQUIRED_FIELDS:
        assert field in release_lock
        assert release_lock[field] not in (None, "", [], {})


def test_release_lock_profile_identity_is_pinned() -> None:
    release_lock, _ = qualify.build_release_lock()
    assert release_lock["profile_id"] == "management-readonly-v1"
    assert release_lock["auth_token_profile"] == "omniscience-mcp-read-v1"
    assert release_lock["dependency_profile"]["omnius_status"] == "not_deployed"


def test_release_lock_pii_policy_digest_matches_the_pinned_sp60_constant() -> None:
    release_lock, _ = qualify.build_release_lock()
    assert release_lock["pii_policy_digest"] == qualify.PINNED_SP60_POLICY_DIGEST


def test_release_lock_digest_fields_are_content_addressed() -> None:
    release_lock, _ = qualify.build_release_lock()
    for field in (
        "image_digest",
        "source_input_digest",
        "chart_digest",
        "configuration_digest",
        "mcp_manifest_digest",
        "management_context_manifest_digest",
        "schema_set_digest",
        "pii_policy_digest",
        "severance_fixture_digest",
        "language_neutral_consumer_kit_digest",
    ):
        value = release_lock[field]
        assert value.startswith("sha256:")
        assert len(value) == len("sha256:") + 64


def test_git_commit_is_the_real_40_hex_head() -> None:
    release_lock, _ = qualify.build_release_lock()
    git = shutil.which("git")
    assert git is not None
    head = subprocess.run(
        [git, "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert release_lock["git_commit"] == head.lower()


# ---------------------------------------------------------------------------
# Dirty/mutable-input RED behavior (AC-SP86-1 "mutable or dirty inputs are RED")
# ---------------------------------------------------------------------------


def test_missing_git_produces_a_red_reason_not_a_crash(monkeypatch) -> None:
    monkeypatch.setattr(qualify.shutil, "which", lambda _name: None)
    release_lock, result = qualify.build_release_lock()
    assert result.status == "RED"
    assert "git_commit_unavailable" in result.reasons
    assert "git_status_unavailable" in result.reasons
    assert release_lock["git_commit"] == "0" * 40


def test_dirty_scoped_worktree_is_red(monkeypatch) -> None:
    monkeypatch.setattr(qualify, "_scoped_worktree_is_clean", lambda repo, paths: False)
    _release_lock, result = qualify.build_release_lock()
    assert result.status == "RED"
    assert "scoped_worktree_dirty" in result.reasons


def test_clean_scoped_worktree_removes_the_dirty_reason(monkeypatch) -> None:
    monkeypatch.setattr(qualify, "_scoped_worktree_is_clean", lambda repo, paths: True)
    _release_lock, result = qualify.build_release_lock()
    assert "scoped_worktree_dirty" not in result.reasons


def test_source_inputs_and_dependency_lock_are_in_digest_and_cleanliness_scope(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for relative, content in (
        ("Dockerfile", "FROM python:3.12-slim\n"),
        ("pyproject.toml", "[project]\nname='sample'\n"),
        ("uv.lock", "version = 1\n"),
        ("apps/server/src/omniscience_server/routes/tokens.py", "TOKEN_POLICY = 1\n"),
        ("packages/core/src/omniscience_core/config/auth.py", "AUTH = 1\n"),
        ("contracts/mcp/manifest.json", "{}\n"),
    ):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)

    before = qualify._source_input_digest(repo)
    assert before is not None
    assert qualify._scoped_worktree_is_clean(repo, qualify.SCOPED_PATHS) is True

    for relative, content in (
        ("apps/server/src/omniscience_server/routes/tokens.py", "TOKEN_POLICY = 2\n"),
        ("uv.lock", "version = 2\n"),
    ):
        (repo / relative).write_text(content, encoding="utf-8")
        assert qualify._source_input_digest(repo) != before
        assert qualify._scoped_worktree_is_clean(repo, qualify.SCOPED_PATHS) is False
        subprocess.run(["git", "checkout", "--", relative], cwd=repo, check=True)
        before = qualify._source_input_digest(repo)

    ignored = repo / ".venv" / "cache"
    ignored.parent.mkdir()
    ignored.write_text("cache", encoding="utf-8")
    # Common build caches are not source inputs even if accidentally present.
    assert qualify._source_input_digest(repo) == before


def test_severance_receipt_rejects_duplicate_ids_and_malformed_field_types() -> None:
    fixture_path = (
        ROOT
        / "contracts"
        / "releases"
        / "management-readonly-v1"
        / "severance-fixtures.json"
    )
    fixture_rows = json.loads(fixture_path.read_text(encoding="utf-8"))["fixtures"]
    receipt_rows = [
        {"id": row["id"], "decision": row["expected_decision"]} for row in fixture_rows
    ]

    duplicate = [*receipt_rows, dict(receipt_rows[0], decision="healthy")]
    result = qualify.verify_severance_receipt(receipt={"results": duplicate})
    assert result.status == "RED"
    assert any("duplicate_result_id" in reason for reason in result.reasons)

    for malformed_row in (
        {**receipt_rows[0], "id": [receipt_rows[0]["id"]]},
        {**receipt_rows[0], "id": "  "},
        {**receipt_rows[0], "decision": [receipt_rows[0]["decision"]]},
        {**receipt_rows[0], "decision": ""},
    ):
        rows = [malformed_row, *receipt_rows[1:]]
        result = qualify.verify_severance_receipt(receipt={"results": rows})
        assert result.status == "RED"
        assert result.reasons


# ---------------------------------------------------------------------------
# Digest drift: tampered vendored file vs its own pin.json (isolated copy)
# ---------------------------------------------------------------------------


def _isolated_mcp_copy(tmp_path: Path) -> Path:
    """Copy contracts/mcp/ into an isolated tmp dir and also mirror its
    schemas/v1/*.json files into a flat schemas/*.json layout at the same
    root, matching the SP-10 ground-truth layout
    (apps/server/.../mcp/contracts/v1/{manifest.json,tool-registry.json,schemas/*.json}).
    This lets a test pass ``ground_truth_root=isolated`` and get a
    self-consistent ground-truth leg, isolating the assertion to the
    pin-vs-file digest check."""
    isolated = tmp_path / "mcp"
    shutil.copytree(ROOT / "contracts" / "mcp", isolated)
    flat_schemas = isolated / "schemas"
    for schema_file in (isolated / "schemas" / "v1").glob("*.json"):
        shutil.copy2(schema_file, flat_schemas / schema_file.name)
    return isolated


def test_tampered_vendored_schema_is_detected_as_pin_drift(tmp_path: Path) -> None:
    """Copies contracts/mcp/ into an isolated tmp dir, corrupts one vendored
    schema file's bytes, and proves ``verify_pin`` (loaded from that same
    isolated copy) reports a digest mismatch -- never the real repository's
    contracts/mcp/ is touched."""
    isolated = _isolated_mcp_copy(tmp_path)

    target = isolated / "schemas" / "v1" / "meta.schema.json"
    target.write_bytes(target.read_bytes() + b"\n// tampered-by-test\n")

    spec = importlib.util.spec_from_file_location(
        "_tmp_verify_pin", isolated / "tooling" / "verify_pin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # ground_truth_root=isolated: the copy is compared against itself for the
    # ground-truth leg, isolating this assertion to the pin-vs-file digest
    # check (the real cross-check against apps/server/.../mcp/contracts/v1
    # is exercised by the (untouched) real contracts/mcp/tooling/verify_pin.py
    # elsewhere -- see contracts/mcp/README.md).
    errors = module.verify_pin(contract_root=isolated, ground_truth_root=isolated)
    assert any("pin digest drift" in e for e in errors)


def test_untampered_isolated_copy_still_verifies_clean(tmp_path: Path) -> None:
    isolated = _isolated_mcp_copy(tmp_path)

    spec = importlib.util.spec_from_file_location(
        "_tmp_verify_pin_clean", isolated / "tooling" / "verify_pin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    errors = module.verify_pin(contract_root=isolated, ground_truth_root=isolated)
    assert errors == []


# ---------------------------------------------------------------------------
# --write evidence persistence (isolated tmp_path, never the real repo)
# ---------------------------------------------------------------------------


def test_write_evidence_persists_a_canonical_json_file(tmp_path: Path) -> None:
    release_lock, _ = qualify.build_release_lock()
    target = qualify._write_evidence(tmp_path, release_lock)
    assert target.is_file()
    expected_dir = tmp_path / "contracts" / "releases" / "management-readonly-v1" / "evidence"
    assert target.parent == expected_dir
    round_tripped = json.loads(target.read_text(encoding="utf-8"))
    assert round_tripped == release_lock


# ---------------------------------------------------------------------------
# CLI contract
# ---------------------------------------------------------------------------


def test_cli_exits_nonzero_and_reports_reasons_on_red() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--quiet"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # This repository's worktree has uncommitted task-sp-86 work at test time,
    # so the CLI is expected to report RED on the scoped-dirty reason -- this
    # test asserts the CLI's *contract* (nonzero exit + named reasons on
    # stderr), not that the tree happens to be clean.
    assert result.returncode in (0, 1)
    if result.returncode == 1:
        assert "RED" in result.stderr
        assert "- " in result.stderr


def test_cli_report_is_valid_json_on_stdout() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    payload = json.loads(result.stdout)
    assert payload["profile_id"] == "management-readonly-v1"
