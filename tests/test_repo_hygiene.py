"""Repository hygiene: generated/cache artifacts and secrets must not be tracked.

Guards the P0 review action point: the tree must not track Hypothesis example
databases (.hypothesis/), pytest caches (.pytest_cache/), a real .env, or the
ad-hoc root-level debug_fixture.py script, and .gitignore must prevent each of
them from being committed again. Only .env.example stays tracked as the
documented template.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def tracked_files() -> list[str]:
    if shutil.which("git") is None:
        pytest.skip("git executable not available")
    result = _git("ls-files")
    if result.returncode != 0:
        pytest.skip(f"not a git checkout: {result.stderr.strip()}")
    return result.stdout.splitlines()


def test_no_hypothesis_database_tracked(tracked_files: list[str]) -> None:
    offenders = [f for f in tracked_files if f.startswith(".hypothesis/")]
    assert not offenders, (
        "Hypothesis example database is regenerated on every run and must not be "
        f"committed; tracked entries: {offenders[:10]}"
        f"{' …' if len(offenders) > 10 else ''}"
    )


def test_no_pytest_cache_tracked(tracked_files: list[str]) -> None:
    offenders = [f for f in tracked_files if f.startswith(".pytest_cache/")]
    assert not offenders, (
        f".pytest_cache/ is a self-documented 'do not commit' cache; tracked: {offenders}"
    )


def test_env_file_not_tracked(tracked_files: list[str]) -> None:
    assert ".env" not in tracked_files, (
        ".env may hold real secrets and must never be tracked; keep .env.example instead"
    )


def test_env_example_still_tracked(tracked_files: list[str]) -> None:
    assert ".env.example" in tracked_files, (
        ".env.example is the documented configuration template and must stay tracked"
    )


def test_debug_fixture_not_tracked(tracked_files: list[str]) -> None:
    assert "debug_fixture.py" not in tracked_files, (
        "root-level debug_fixture.py is an ad-hoc debug script that pollutes test "
        "collection; it must be removed (a maintained variant belongs in tests/ or scripts/)"
    )


def test_debug_fixture_absent_from_root() -> None:
    assert not (REPO_ROOT / "debug_fixture.py").exists(), (
        "root-level debug_fixture.py runs asyncio.run() at import time and pollutes "
        "test collection; delete it"
    )


@pytest.mark.parametrize(
    "path",
    [
        ".hypothesis/constants/0123456789abcdef",
        ".pytest_cache/README.md",
        ".env",
    ],
)
def test_gitignore_blocks_regenerable_artifacts(tracked_files: list[str], path: str) -> None:
    # tracked_files is requested only so the git-availability skip applies here too.
    result = _git("check-ignore", "-q", "--no-index", path)
    assert result.returncode == 0, (
        f"{path!r} is a regenerable artifact or secret and must be matched by "
        ".gitignore so it cannot be committed again"
    )
