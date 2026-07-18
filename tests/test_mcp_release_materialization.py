"""Release-bundle materialization for the stable MCP v1 contract."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "apps/server/src/omniscience_server/mcp/materialize.py"
SPEC = importlib.util.spec_from_file_location("materialize_mcp_contract_v1", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MATERIALIZER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MATERIALIZER)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def source_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    contract_path = Path("apps/server/src/omniscience_server/mcp/contracts/v1")
    shutil.copytree(ROOT / contract_path, repo / contract_path)
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "materializer@example.invalid")
    _git(repo, "config", "user.name", "MCP Materializer Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "contract fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_materializer_builds_idempotent_content_addressed_bundle_from_exact_head(
    source_repo: tuple[Path, str],
    tmp_path: Path,
) -> None:
    repo, commit = source_repo
    output_root = tmp_path / "published"

    result = MATERIALIZER.materialize_release_bundle(
        repo=repo,
        source_commit=commit,
        output_root=output_root,
    )
    replay = MATERIALIZER.materialize_release_bundle(
        repo=repo,
        source_commit=commit,
        output_root=output_root,
    )

    assert replay == result
    assert result.bundle_dir == output_root / "sha256" / result.manifest_sha256
    manifest_bytes = (result.bundle_dir / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest_bytes == (
        json.dumps(
            manifest,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    assert hashlib.sha256(manifest_bytes).hexdigest() == result.manifest_sha256
    assert manifest["source_commit"] == {"git_commit": commit}
    registry_path = "apps/server/src/omniscience_server/mcp/contracts/v1/tool-registry.json"
    committed_registry = subprocess.run(
        ["git", "show", f"{commit}:{registry_path}"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    assert (result.bundle_dir / "tool-registry.json").read_bytes() == committed_registry


def test_materializer_rejects_dirty_or_non_head_source(
    source_repo: tuple[Path, str],
    tmp_path: Path,
) -> None:
    repo, first_commit = source_repo
    (repo / "untracked.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(MATERIALIZER.MaterializationError, match="source_worktree_not_clean"):
        MATERIALIZER.materialize_release_bundle(
            repo=repo,
            source_commit=first_commit,
            output_root=tmp_path / "dirty-output",
        )

    (repo / "untracked.txt").unlink()
    (repo / "note.txt").write_text("second", encoding="utf-8")
    _git(repo, "add", "note.txt")
    _git(repo, "commit", "-qm", "second commit")
    with pytest.raises(MATERIALIZER.MaterializationError, match="source_commit_not_head"):
        MATERIALIZER.materialize_release_bundle(
            repo=repo,
            source_commit=first_commit,
            output_root=tmp_path / "old-output",
        )


def test_materializer_rejects_committed_schema_digest_drift(
    source_repo: tuple[Path, str],
    tmp_path: Path,
) -> None:
    repo, _ = source_repo
    schema = (
        repo
        / "apps/server/src/omniscience_server/mcp/contracts/v1/schemas/search.input.schema.json"
    )
    schema.write_text(schema.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    _git(repo, "add", str(schema.relative_to(repo)))
    _git(repo, "commit", "-qm", "drift schema")
    drift_commit = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(MATERIALIZER.MaterializationError, match="schema_sha256_mismatch"):
        MATERIALIZER.materialize_release_bundle(
            repo=repo,
            source_commit=drift_commit,
            output_root=tmp_path / "drift-output",
        )


def test_materializer_cli_emits_receipt_and_stable_failure(
    source_repo: tuple[Path, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, commit = source_repo
    output_root = tmp_path / "cli-output"

    assert (
        MATERIALIZER.main(
            [
                "--repo",
                str(repo),
                "--source-commit",
                commit,
                "--output-root",
                str(output_root),
            ]
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["source_commit"] == commit
    assert Path(receipt["bundle_dir"]).is_dir()

    (repo / "dirty.txt").write_text("dirty", encoding="utf-8")
    assert (
        MATERIALIZER.main(
            [
                "--repo",
                str(repo),
                "--source-commit",
                commit,
                "--output-root",
                str(tmp_path / "failed-output"),
            ]
        )
        == 1
    )
    assert capsys.readouterr().err == (
        "MCP v1 materialization failed: source_worktree_not_clean\n"
    )


def test_materializer_rejects_content_address_conflict(
    source_repo: tuple[Path, str],
    tmp_path: Path,
) -> None:
    repo, commit = source_repo
    result = MATERIALIZER.materialize_release_bundle(
        repo=repo,
        source_commit=commit,
        output_root=tmp_path / "conflict-output",
    )
    (result.bundle_dir / "manifest.json").write_bytes(b"{}\n")

    with pytest.raises(MATERIALIZER.MaterializationError, match="content_address_conflict"):
        MATERIALIZER.materialize_release_bundle(
            repo=repo,
            source_commit=commit,
            output_root=tmp_path / "conflict-output",
        )


def test_materializer_strict_input_helpers_fail_closed(
    source_repo: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _ = source_repo
    for invalid in ("not-a-commit", "0" * 40):
        with pytest.raises(MATERIALIZER.MaterializationError, match="source_commit_invalid"):
            MATERIALIZER._validate_source(repo, invalid)

    for payload in (b"[]", b'{"x":1,"x":2}', b'{"x":NaN}', b"\xff"):
        with pytest.raises(MATERIALIZER.MaterializationError, match="invalid"):
            MATERIALIZER._strict_object(payload, code="manifest_invalid")
    for value in (None, "../manifest.json", "/schemas/input.json", "schemas/input.txt"):
        with pytest.raises(MATERIALIZER.MaterializationError, match="schema_path_invalid"):
            MATERIALIZER._schema_path(value)
    with pytest.raises(MATERIALIZER.MaterializationError, match="manifest_not_canonicalizable"):
        MATERIALIZER._canonical_bytes({"not_finite": float("nan")})

    monkeypatch.setattr(MATERIALIZER.shutil, "which", lambda _name: None)
    with pytest.raises(MATERIALIZER.MaterializationError, match="source_repository_invalid"):
        MATERIALIZER._git(repo, "status")

    with pytest.raises(MATERIALIZER.MaterializationError, match="source_repository_invalid"):
        MATERIALIZER.materialize_release_bundle(
            repo=tmp_path / "missing",
            source_commit="a" * 40,
            output_root=tmp_path / "missing-output",
        )


@pytest.mark.parametrize(
    ("binary", "stdout"),
    [(False, b"unexpected-bytes"), (True, "unexpected-text")],
)
def test_materializer_rejects_git_stdout_type_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    binary: bool,
    stdout: str | bytes,
) -> None:
    monkeypatch.setattr(MATERIALIZER.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(
        MATERIALIZER.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout=stdout,
            stderr=None,
        ),
    )

    with pytest.raises(MATERIALIZER.MaterializationError, match="source_repository_invalid"):
        MATERIALIZER._git(tmp_path, "status", binary=binary)
