"""Tests for the curl | bash installer's checksum verification.

``.mcp/install.sh`` is fetched and piped straight into ``bash`` by the
documented one-liner, which is a supply-chain risk if the download is
tampered with or corrupted in transit. This test file pins the expected
contract for hardening that path:

* Alongside the compose file it downloads
  (``<OMNISCIENCE_REPO>/<OMNISCIENCE_REF>/docker-compose.prod.yml``), the
  installer must fetch a checksum sidecar at the same location with a
  ``.sha256`` suffix, containing a standard ``sha256sum``-style line:
  ``<hex-digest>  docker-compose.prod.yml``.
* It must compute the SHA-256 digest of the downloaded compose file and
  compare it against the digest in the sidecar.
* On a digest mismatch (tampered/corrupted download), or when the sidecar
  is missing entirely, the installer must abort with a non-zero exit
  status *before* running ``docker compose ... up``, and its output must
  mention the checksum/verification failure.
* On a match, installation proceeds exactly as before.
* The README's one-line install instructions must also document a safer
  "download, inspect, verify checksum, then run" alternative to blindly
  piping the script into bash.
"""

import hashlib
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
INSTALL_SCRIPT = REPO_ROOT / ".mcp" / "install.sh"

COMPOSE_FIXTURE = b"""\
services:
  app:
    image: ghcr.io/100rd/omniscience-app:latest
"""


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def docker_log(tmp_path: Path) -> Path:
    return tmp_path / "docker-compose-invocations.log"


@pytest.fixture
def stub_bin(tmp_path: Path, docker_log: Path) -> Path:
    """A fake `docker` that records invocations instead of touching real containers."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "docker",
        f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "--version" ]]; then
  echo "Docker version 24.0.0, build deadbeef"
  exit 0
fi
if [[ "$1" == "compose" ]]; then
  shift
  if [[ "$1" == "version" ]]; then
    echo "Docker Compose version v2.29.0"
    exit 0
  fi
  echo "$@" >> "{docker_log}"
  exit 0
fi
exit 1
""",
    )
    return bin_dir


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """A fake raw.githubusercontent.com layout: <repo>/<ref>/<file>, served over file://."""
    repo_dir = tmp_path / "repo"
    ref_dir = repo_dir / "main"
    ref_dir.mkdir(parents=True)
    (ref_dir / "docker-compose.prod.yml").write_bytes(COMPOSE_FIXTURE)
    return repo_dir


def _run_install(
    tmp_path: Path,
    fixture_repo: Path,
    stub_bin: Path,
    checksum_line: str | None,
) -> subprocess.CompletedProcess[str]:
    ref_dir = fixture_repo / "main"
    if checksum_line is not None:
        (ref_dir / "docker-compose.prod.yml.sha256").write_text(checksum_line)

    health_file = tmp_path / "health"
    health_file.write_text("ok")

    install_dir = tmp_path / "omniscience"

    env = dict(os.environ)
    env["PATH"] = f"{stub_bin}:{env['PATH']}"
    env["OMNISCIENCE_REPO"] = f"file://{fixture_repo}"
    env["OMNISCIENCE_REF"] = "main"
    env["OMNISCIENCE_DIR"] = str(install_dir)
    env["OMNISCIENCE_HEALTH_URL"] = f"file://{health_file}"

    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_install_aborts_on_checksum_mismatch(
    tmp_path: Path, fixture_repo: Path, stub_bin: Path, docker_log: Path
) -> None:
    wrong_digest = "0" * 64
    result = _run_install(
        tmp_path,
        fixture_repo,
        stub_bin,
        checksum_line=f"{wrong_digest}  docker-compose.prod.yml\n",
    )

    assert result.returncode != 0
    combined = (result.stdout + result.stderr).lower()
    assert "checksum" in combined or "verif" in combined

    assert not docker_log.exists() or "up" not in docker_log.read_text()


def test_install_aborts_when_checksum_sidecar_missing(
    tmp_path: Path, fixture_repo: Path, stub_bin: Path, docker_log: Path
) -> None:
    result = _run_install(tmp_path, fixture_repo, stub_bin, checksum_line=None)

    assert result.returncode != 0
    assert not docker_log.exists() or "up" not in docker_log.read_text()


def test_install_proceeds_on_checksum_match(
    tmp_path: Path, fixture_repo: Path, stub_bin: Path, docker_log: Path
) -> None:
    digest = hashlib.sha256(COMPOSE_FIXTURE).hexdigest()
    result = _run_install(
        tmp_path,
        fixture_repo,
        stub_bin,
        checksum_line=f"{digest}  docker-compose.prod.yml\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr

    assert docker_log.exists()
    assert "up" in docker_log.read_text()

    compose_path = tmp_path / "omniscience" / "docker-compose.prod.yml"
    assert compose_path.read_bytes() == COMPOSE_FIXTURE


def test_readme_documents_verify_before_run_alternative() -> None:
    readme = (REPO_ROOT / "README.md").read_text()
    one_liner = (
        "curl -fsSL https://raw.githubusercontent.com/100rd/Omniscience/"
        "main/.mcp/install.sh | bash"
    )
    install_idx = readme.index(one_liner)
    surrounding = readme[install_idx : install_idx + 2000].lower()

    assert "sha256" in surrounding or "checksum" in surrounding
    assert "inspect" in surrounding or "review the script" in surrounding
