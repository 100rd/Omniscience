"""Repo hygiene lint: ephemeral test/cache artifacts must never be tracked by git.

Covers the review action point requiring that ``.coverage``, ``.pytest_cache/``,
and ``.hypothesis/`` (including its ``constants`` cache) are purged from the
git index and covered by ``.gitignore`` so they cannot be re-added by accident.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Patterns that must never appear among tracked files.
ARTIFACT_MARKERS = (".coverage", ".pytest_cache", ".hypothesis")

# Substrings that must each be present in the root .gitignore so these
# artifacts can't be re-committed in the future.
REQUIRED_GITIGNORE_ENTRIES = (
    ".pytest_cache/",
    ".coverage",
    ".hypothesis/",
)


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def test_no_coverage_pytest_cache_or_hypothesis_artifacts_tracked():
    """None of the ephemeral test/cache artifacts should be committed."""
    tracked = _tracked_files()

    offenders = [
        path
        for path in tracked
        for marker in ARTIFACT_MARKERS
        if marker in path.split("/")
        or Path(path).name == marker
        or Path(path).name.startswith(f"{marker}.")
    ]

    assert offenders == [], (
        "Ephemeral test/cache artifacts must not be tracked by git: "
        f"{offenders}"
    )


@pytest.mark.parametrize("entry", REQUIRED_GITIGNORE_ENTRIES)
def test_gitignore_covers_ephemeral_test_artifacts(entry):
    """.gitignore must list each ephemeral artifact so it can't be re-added."""
    gitignore_path = REPO_ROOT / ".gitignore"
    assert gitignore_path.exists(), ".gitignore is missing at repo root"

    contents = gitignore_path.read_text(encoding="utf-8")
    lines = {line.strip() for line in contents.splitlines()}

    assert entry in lines, (
        f"Expected '{entry}' to be present as its own line in .gitignore, "
        "so committed cache/test artifacts stay ignored."
    )
