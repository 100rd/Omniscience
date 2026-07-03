"""Guards the scope of the S104 ("hardcoded bind-all-interfaces") lint ignore.

``[tool.ruff.lint].ignore`` used to suppress S104 repo-wide, with a comment
claiming it was "required for containerised services". In reality only two
files ever bind ``0.0.0.0`` on purpose -- the server's uvicorn entrypoint and
the CLI's ``mcp serve --http`` subprocess invocation -- see consilium round-1
P2. A repo-wide ignore silences the check everywhere else too, so a future
socket/bind call anywhere in the codebase would never be flagged.

These tests assert S104 is suppressed only via
``[tool.ruff.lint.per-file-ignores]``, scoped to exactly the files that
legitimately bind to all interfaces today, and that the well-known cache
directories (`.hypothesis/`, `.coverage`, `.pytest_cache/`) are excluded via
``.gitignore`` and are not accidentally tracked in git.
"""

from __future__ import annotations

import fnmatch
import subprocess
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
GITIGNORE = REPO_ROOT / ".gitignore"

# The only source files (outside tests/benchmarks/bench) that legitimately
# bind to all network interfaces today. If this set ever changes, the
# per-file-ignores scope in pyproject.toml must change with it.
KNOWN_BIND_ALL_ENTRYPOINTS = {
    "apps/server/src/omniscience_server/__main__.py",
    "apps/cli/src/omniscience_cli/commands/mcp.py",
}

CACHE_PATHS = [".hypothesis", ".coverage", ".pytest_cache"]


def _ruff_lint_config() -> dict:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return config["tool"]["ruff"]["lint"]


def _tracked_files(pattern: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", pattern],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _files_binding_all_interfaces(python_files: list[str]) -> set[str]:
    """Tracked, non-test/-benchmark source files containing a literal
    "0.0.0.0" host string -- the only paths S104 should legitimately ignore.
    """
    matches = set()
    for rel_path in python_files:
        if rel_path.startswith(("tests/", "benchmarks/", "bench/")):
            continue
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        if '"0.0.0.0"' in text or "'0.0.0.0'" in text:
            matches.add(rel_path)
    return matches


class TestS104ScopedToEntrypoints:
    def test_s104_is_not_ignored_repo_wide(self):
        lint_config = _ruff_lint_config()
        global_ignore = lint_config.get("ignore", [])
        assert "S104" not in global_ignore, (
            "S104 (hardcoded bind-all-interfaces) must not be suppressed in "
            "[tool.ruff.lint].ignore -- a repo-wide ignore silences the check "
            "on every file, not just the containerised entrypoints that "
            "legitimately need host='0.0.0.0'. Scope the suppression via "
            "[tool.ruff.lint.per-file-ignores] instead."
        )

    def test_known_bind_all_entrypoints_fixture_is_current(self):
        """Sanity-checks the fixture the tests below rely on. If a new file
        starts binding to 0.0.0.0, update KNOWN_BIND_ALL_ENTRYPOINTS and the
        pyproject.toml per-file-ignores scope together."""
        python_files = _tracked_files("*.py")
        bind_all_files = _files_binding_all_interfaces(python_files)
        assert bind_all_files == KNOWN_BIND_ALL_ENTRYPOINTS, (
            f"expected exactly {sorted(KNOWN_BIND_ALL_ENTRYPOINTS)} to bind to "
            f"0.0.0.0, found {sorted(bind_all_files)} instead -- update this "
            "test and the ruff per-file-ignores scope together."
        )

    def test_s104_per_file_ignore_exists(self):
        lint_config = _ruff_lint_config()
        per_file_ignores = lint_config.get("per-file-ignores", {})
        s104_patterns = [pattern for pattern, codes in per_file_ignores.items() if "S104" in codes]
        assert s104_patterns, (
            "expected [tool.ruff.lint.per-file-ignores] to contain at least "
            "one glob pattern suppressing S104 for the containerised "
            f"entrypoints {sorted(KNOWN_BIND_ALL_ENTRYPOINTS)}."
        )

    def test_s104_per_file_ignore_covers_all_known_entrypoints(self):
        lint_config = _ruff_lint_config()
        per_file_ignores = lint_config.get("per-file-ignores", {})
        s104_patterns = [pattern for pattern, codes in per_file_ignores.items() if "S104" in codes]
        assert s104_patterns, "no per-file-ignore pattern suppresses S104"

        python_files = _tracked_files("*.py")
        matched_files = {
            rel_path
            for rel_path in python_files
            for pattern in s104_patterns
            if fnmatch.fnmatch(rel_path, pattern)
        }

        missing = KNOWN_BIND_ALL_ENTRYPOINTS - matched_files
        assert not missing, (
            f"S104 per-file-ignore pattern(s) {s104_patterns} do not cover "
            f"the known bind-all entrypoint(s) {sorted(missing)} -- those "
            "files would fail lint once the repo-wide S104 ignore is removed."
        )

    def test_s104_per_file_ignore_does_not_leak_beyond_known_entrypoints(self):
        """The whole point of scoping S104 is that it stays enforced
        everywhere else. A pattern like "apps/*" or "**/*.py" would defeat
        that -- assert the per-file-ignore glob(s) match only the known
        bind-all entrypoints among all tracked Python files."""
        lint_config = _ruff_lint_config()
        per_file_ignores = lint_config.get("per-file-ignores", {})
        s104_patterns = [pattern for pattern, codes in per_file_ignores.items() if "S104" in codes]
        assert s104_patterns, "no per-file-ignore pattern suppresses S104"

        python_files = _tracked_files("*.py")
        matched_files = {
            rel_path
            for rel_path in python_files
            for pattern in s104_patterns
            if fnmatch.fnmatch(rel_path, pattern)
        }

        unexpected = matched_files - KNOWN_BIND_ALL_ENTRYPOINTS
        assert not unexpected, (
            f"S104 per-file-ignore pattern(s) {s104_patterns} match files "
            f"beyond the known bind-all entrypoints: {sorted(unexpected)}. "
            "Scope the per-file-ignore glob(s) more tightly (e.g. exact file "
            "paths) so S104 stays enforced on every other file."
        )


class TestCacheDirectoriesAreGitignored:
    def test_gitignore_lists_hypothesis_coverage_and_pytest_cache(self):
        gitignore_text = GITIGNORE.read_text(encoding="utf-8")
        lines = {line.strip() for line in gitignore_text.splitlines()}

        for expected in (".hypothesis/", ".coverage", ".pytest_cache/"):
            assert expected in lines, (
                f"expected '{expected}' to be listed in .gitignore so the "
                "corresponding cache directory/file is never accidentally "
                "tracked by `git add -A`."
            )

    def test_cache_paths_are_not_tracked_in_git(self):
        for rel_path in CACHE_PATHS:
            tracked = _tracked_files(rel_path)
            assert not tracked, (
                f"'{rel_path}' has tracked file(s) in git: {tracked} -- "
                "cache/state directories must never be committed."
            )

            nested = _tracked_files(f"{rel_path}/*")
            assert not nested, (
                f"'{rel_path}/' has tracked file(s) in git: {nested} -- "
                "cache/state directories must never be committed."
            )
