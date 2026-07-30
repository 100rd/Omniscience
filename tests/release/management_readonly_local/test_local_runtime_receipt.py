"""AC-SP95-7: one immutable local owner receipt binds artifacts, configuration,
behaviour and non-authority fields, and re-derives byte-identically.

Mirrors the SP-86 release-lock discipline: the qualifier is a pure, read-only
function of the tree; digests reproduce exactly (the exact-rollback
prerequisite); a dirty scoped worktree or an unusable input is deterministically
RED with a named reason and honest placeholders -- never a fabricated GREEN.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "qualify_management_readonly_local.py"

_SPEC = importlib.util.spec_from_file_location("qualify_management_readonly_local", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
qualify = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = qualify
_SPEC.loader.exec_module(qualify)


_DIGEST_FIELDS = (
    "sp86_release_digest",
    "image_content_digest",
    "configuration_digest",
    "service_contract_digest",
    "fragment_digest",
    "service_inventory_digest",
    "network_inventory_digest",
    "volume_inventory_digest",
    "host_binding_inventory_digest",
    "local_model_digest",
    "dependency_closure_digest",
    "readiness_digest",
)


def _fingerprint(*roots: Path) -> str:
    h = hashlib.sha256()
    for root in roots:
        files = (p for p in root.rglob("*") if p.is_file() and "evidence" not in p.parts)
        for path in sorted(files):
            h.update(path.relative_to(ROOT).as_posix().encode())
            h.update(path.read_bytes())
    return h.hexdigest()


def test_receipt_has_every_required_field_populated() -> None:
    receipt, _ = qualify.build_receipt()
    for field in qualify._RECEIPT_REQUIRED_FIELDS:
        assert field in receipt, f"missing receipt field {field}"
        if field != "git_worktree_scope_clean":
            assert receipt[field] not in (None, "", [], {}), f"empty receipt field {field}"


def test_receipt_non_authority_fields_are_pinned() -> None:
    receipt, _ = qualify.build_receipt()
    assert receipt["profile_id"] == "management-readonly-local-v1"
    assert receipt["base_profile_id"] == "management-readonly-v1"
    assert receipt["availability_class"] == "development-single-host"
    assert receipt["ha_qualified"] is False
    assert receipt["activation_authority"] == "none"


def test_receipt_never_leaks_a_management_verdict() -> None:
    """The receipt is ops/packaging evidence -- it must carry no management
    verdict, recommendation, or authorization vocabulary (ADR-0022 boundary)."""
    receipt, _ = qualify.build_receipt()
    serialized = json.dumps(receipt).lower()
    for token in ("verdict", "recommendation", "approval", "authorize", "incident"):
        assert token not in serialized


def test_receipt_digests_are_all_sha256_and_reproducible() -> None:
    first, _ = qualify.build_receipt()
    second, _ = qualify.build_receipt()
    for field in _DIGEST_FIELDS:
        assert first[field].startswith("sha256:")
        assert len(first[field]) == len("sha256:") + 64
        assert first[field] == second[field], f"{field} not reproducible"


def test_consumes_the_exact_sp86_release_lock() -> None:
    """The receipt binds the EXACT SP-86 release lock (by its own commit) and
    surfaces that SP-86 has no live OCI digest yet -- pending, not fabricated."""
    receipt, result = qualify.build_receipt()
    assert receipt["sp86_release_git_commit"] == "c733a0d445c090187461814263be7a07d20b7af3"
    assert receipt["sp86_release_digest"] == qualify._sha256_file(qualify.SP86_RELEASE_LOCK)
    assert "sp86_image_registry_digest" in receipt["pending"]
    assert "sp86_image_registry_digest_pending" in result.reasons


def test_source_mode_dirty_tree_is_not_reproducible() -> None:
    receipt, result = qualify.build_receipt(mode="source")
    # This working tree is dirty pre-commit; source mode can never be reproducible.
    assert receipt["reproducible"] is False
    assert receipt["qualification_status"] == "development-only"
    assert result.status == "RED"


def test_qualifier_is_read_only() -> None:
    before = _fingerprint(ROOT / "scripts", ROOT / "contracts" / "releases")
    qualify.build_receipt()
    after = _fingerprint(ROOT / "scripts", ROOT / "contracts" / "releases")
    assert before == after


def test_missing_git_yields_named_reason_and_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qualify.shutil, "which", lambda _name: None)
    receipt, result = qualify.build_receipt()
    assert receipt["git_commit"] == "0" * 40
    assert "git_commit_unavailable" in result.reasons
    assert "git_status_unavailable" in result.reasons


def test_clean_scope_removes_the_dirty_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qualify, "_scoped_worktree_is_clean", lambda *_a, **_k: True)
    receipt, result = qualify.build_receipt()
    assert receipt["git_worktree_scope_clean"] is True
    assert "scoped_worktree_dirty" not in result.reasons


def test_evidence_write_roundtrips(tmp_path: Path) -> None:
    receipt, _ = qualify.build_receipt()
    out = qualify._write_evidence(tmp_path, receipt)
    assert out.exists()
    reloaded = json.loads(out.read_text(encoding="utf-8"))
    assert reloaded == receipt


def test_rendered_config_binds_when_supplied(tmp_path: Path) -> None:
    rendered = tmp_path / "rendered.yaml"
    rendered.write_text("name: omniscience-local\nservices: {}\n", encoding="utf-8")
    receipt, result = qualify.build_receipt(rendered_config=rendered)
    assert receipt["rendered_fragment_digest"] == qualify._sha256_file(rendered)
    assert "rendered_config_not_supplied" not in result.reasons
