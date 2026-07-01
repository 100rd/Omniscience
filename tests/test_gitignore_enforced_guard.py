"""Tests for scratch-artifact hygiene (review action point: purge scratch/cache
artifacts from VCS and enforce .gitignore in CI).

Contract under test:

1. Repo hygiene — no scratch/cache artifacts are tracked by git:
   ``.hypothesis/``, ``.agents/``, ``.pytest_cache/`` contents and the root
   ``debug_fixture.py`` (a local debug script coupled to the private test
   helper ``_full_store_factory``) must be purged from version control, and
   ``.gitignore`` must keep them out.

2. Guard script — ``scripts/check_gitignore_enforced.py`` exposes
   ``tracked_ignored_paths(repo_root)`` returning the git-tracked paths that
   the repo's .gitignore rules say should be ignored (i.e. tracked-but-ignored
   files, ``git ls-files -ci --exclude-standard``). Run as a CLI in a repo, it
   exits 0 when clean and non-zero when a tracked file violates .gitignore.

3. CI wiring — the GitHub Actions CI workflow runs the guard script so a
   re-committed cache/scratch artifact fails the build instead of landing
   on main.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
GUARD_SCRIPT = REPO_ROOT / "scripts" / "check_gitignore_enforced.py"


def _load_guard_module():
    assert GUARD_SCRIPT.exists(), (
        "scripts/check_gitignore_enforced.py is missing — the CI .gitignore "
        "enforcement guard script has not been implemented yet"
    )
    spec = importlib.util.spec_from_file_location("check_gitignore_enforced", GUARD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(args, cwd):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _tracked_files(cwd):
    result = _git(["ls-files"], cwd=cwd)
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


# ---------------------------------------------------------------------------
# 1. Repo hygiene: no scratch/cache artifacts tracked, .gitignore covers them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scratch_dir",
    [".hypothesis/", ".agents/", ".pytest_cache/"],
)
def test_no_scratch_dir_contents_tracked(scratch_dir):
    """Local cache/scratch directories must not be committed."""
    offenders = [path for path in _tracked_files(REPO_ROOT) if path.startswith(scratch_dir)]
    assert offenders == [], (
        f"{len(offenders)} file(s) under {scratch_dir} are tracked in git "
        f"(e.g. {offenders[:5]}). They are local state — remove them with "
        f"`git rm -r --cached {scratch_dir}` and ignore the directory."
    )


def test_debug_fixture_not_tracked():
    """Root debug_fixture.py is a local debug script coupled to the private
    test helper _full_store_factory; it must not live in version control."""
    assert "debug_fixture.py" not in _tracked_files(REPO_ROOT), (
        "debug_fixture.py is tracked at the repo root — it is scratch state "
        "and must be purged from version control (git rm)."
    )


@pytest.mark.parametrize(
    "entry",
    [".hypothesis/", ".agents/", ".pytest_cache/"],
)
def test_gitignore_covers_scratch_dirs(entry):
    """.gitignore must ignore the scratch dirs so they cannot be re-added."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    lines = [line.strip() for line in gitignore.splitlines()]
    assert entry in lines, f".gitignore must contain a literal `{entry}` entry"


def test_no_tracked_file_violates_gitignore():
    """The general invariant the guard enforces: nothing tracked by git may
    match the repo's own .gitignore rules."""
    result = _git(["ls-files", "-ci", "--exclude-standard"], cwd=REPO_ROOT)
    assert result.returncode == 0, result.stderr
    offenders = result.stdout.splitlines()
    assert offenders == [], (
        f"Tracked files violate .gitignore: {offenders[:10]}. "
        "Remove them from the index (git rm --cached)."
    )


# ---------------------------------------------------------------------------
# 2. Guard script: tracked_ignored_paths() and CLI behavior
# ---------------------------------------------------------------------------


def _make_repo_with_tracked_ignored_artifacts(tmp_path):
    _git(["init", "-q"], cwd=tmp_path)
    (tmp_path / ".gitignore").write_text(
        ".hypothesis/\n.agents/\n.pytest_cache/\n", encoding="utf-8"
    )
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / ".hypothesis" / "constants").mkdir(parents=True)
    (tmp_path / ".hypothesis" / "constants" / "c1").write_text("0\n", encoding="utf-8")
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "report.md").write_text("scratch\n", encoding="utf-8")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".pytest_cache" / "CACHEDIR.TAG").write_text("tag\n", encoding="utf-8")
    _git(["add", ".gitignore", "app.py"], cwd=tmp_path)
    _git(
        [
            "add",
            "-f",
            ".hypothesis/constants/c1",
            ".agents/report.md",
            ".pytest_cache/CACHEDIR.TAG",
        ],
        cwd=tmp_path,
    )
    return tmp_path


def test_tracked_ignored_paths_flags_artifacts(tmp_path):
    guard = _load_guard_module()
    repo = _make_repo_with_tracked_ignored_artifacts(tmp_path)
    offenders = guard.tracked_ignored_paths(repo)
    assert set(offenders) == {
        ".hypothesis/constants/c1",
        ".agents/report.md",
        ".pytest_cache/CACHEDIR.TAG",
    }


def test_tracked_ignored_paths_clean_repo(tmp_path):
    guard = _load_guard_module()
    _git(["init", "-q"], cwd=tmp_path)
    (tmp_path / ".gitignore").write_text(".pytest_cache/\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    _git(["add", "."], cwd=tmp_path)
    assert guard.tracked_ignored_paths(tmp_path) == []


def test_guard_cli_passes_on_clean_repo(tmp_path):
    _load_guard_module()  # asserts the script exists with a clear message
    _git(["init", "-q"], cwd=tmp_path)
    (tmp_path / ".gitignore").write_text(".pytest_cache/\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    _git(["add", "."], cwd=tmp_path)

    result = subprocess.run(
        [sys.executable, str(GUARD_SCRIPT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"guard must exit 0 when no tracked file violates .gitignore; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_guard_cli_fails_on_tracked_ignored_artifacts(tmp_path):
    _load_guard_module()
    repo = _make_repo_with_tracked_ignored_artifacts(tmp_path)

    result = subprocess.run(
        [sys.executable, str(GUARD_SCRIPT)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, (
        "guard must fail when git-tracked files match .gitignore rules"
    )
    output = result.stdout + result.stderr
    assert ".hypothesis/constants/c1" in output, (
        "guard failure output must name the offending files"
    )


# ---------------------------------------------------------------------------
# 3. CI wiring: the guard runs in the CI workflow
# ---------------------------------------------------------------------------


def test_ci_workflow_runs_gitignore_guard():
    ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    ci = yaml.safe_load(ci_path.read_text(encoding="utf-8"))
    run_steps = [
        step.get("run", "")
        for job in ci["jobs"].values()
        for step in job.get("steps", [])
    ]
    assert any("check_gitignore_enforced.py" in run for run in run_steps), (
        "CI workflow must run scripts/check_gitignore_enforced.py so a "
        "re-committed scratch/cache artifact fails the build"
    )
