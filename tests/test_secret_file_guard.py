"""Tests for the secret-file guard (review action point: tracked .env leak).

Contract under test:

1. Repo hygiene — no ``.env`` (or other real env file) is tracked by git,
   and ``.gitignore`` keeps it that way. The README tells users to write
   POSTGRES_PASSWORD / OMNISCIENCE_SECRET_KEY into ``.env``, so tracking
   that file would near-guarantee a real-secret leak (CWE-312/538).

2. Guard script — ``scripts/check_no_tracked_secrets.py`` exposes
   ``find_tracked_secret_files(paths)`` that, given an iterable of
   git-tracked paths, returns the ones that are secret files (the ``.env``
   family), while allowing templates such as ``.env.example``. Run as a
   CLI in a repo, it exits 0 when clean and non-zero when a secret file
   is tracked.

3. CI wiring — the GitHub Actions CI workflow runs the guard script so a
   re-committed ``.env`` fails the build instead of landing on main.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent
GUARD_SCRIPT = REPO_ROOT / "scripts" / "check_no_tracked_secrets.py"


def _load_guard_module():
    assert GUARD_SCRIPT.exists(), (
        "scripts/check_no_tracked_secrets.py is missing — the CI secret-file "
        "guard script has not been implemented yet"
    )
    spec = importlib.util.spec_from_file_location("check_no_tracked_secrets", GUARD_SCRIPT)
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


# ---------------------------------------------------------------------------
# 1. Repo hygiene: no tracked .env, .gitignore covers it
# ---------------------------------------------------------------------------


def test_no_env_file_tracked_in_git():
    """No real env file may be tracked; .env.example is the only template."""
    result = _git(["ls-files"], cwd=REPO_ROOT)
    assert result.returncode == 0, result.stderr
    tracked = result.stdout.splitlines()

    offenders = [
        path
        for path in tracked
        if Path(path).name == ".env"
        or (
            Path(path).name.startswith(".env.")
            and not Path(path).name.endswith((".example", ".sample", ".template"))
        )
    ]
    assert offenders == [], (
        f"Secret env files are tracked in git: {offenders}. "
        "Remove them from version control and rotate any leaked secrets."
    )


def test_gitignore_covers_env():
    """.gitignore must ignore .env so it cannot be re-added accidentally."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    lines = [line.strip() for line in gitignore.splitlines()]
    assert ".env" in lines, ".gitignore must contain a literal `.env` entry"


# ---------------------------------------------------------------------------
# 2. Guard script: find_tracked_secret_files()
# ---------------------------------------------------------------------------


def test_guard_flags_env_family():
    guard = _load_guard_module()
    tracked = [
        ".env",
        "apps/api/.env",
        ".env.local",
        ".env.production",
    ]
    flagged = guard.find_tracked_secret_files(tracked)
    assert set(flagged) == set(tracked)


def test_guard_allows_templates_and_lookalikes():
    guard = _load_guard_module()
    tracked = [
        ".env.example",
        "docs/.env.example",
        ".env.ci.example",
        "bin/env",
        "packages/core/alembic/env.py",
        "bench/incidents/bench-0037-config-drift-env-var-typo-default.yaml",
        "README.md",
    ]
    flagged = guard.find_tracked_secret_files(tracked)
    assert flagged == []


def test_guard_cli_passes_on_clean_repo(tmp_path):
    _git(["init", "-q"], cwd=tmp_path)
    (tmp_path / ".env.example").write_text("OMNISCIENCE_SECRET_KEY=\n", encoding="utf-8")
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
        f"guard must exit 0 when no secret file is tracked; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_guard_cli_fails_when_env_is_tracked(tmp_path):
    _git(["init", "-q"], cwd=tmp_path)
    (tmp_path / ".env").write_text("POSTGRES_PASSWORD=hunter2\n", encoding="utf-8")
    _git(["add", "-f", ".env"], cwd=tmp_path)

    result = subprocess.run(
        [sys.executable, str(GUARD_SCRIPT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "guard must fail when a .env file is git-tracked"
    output = result.stdout + result.stderr
    assert ".env" in output, "guard failure output must name the offending file"


# ---------------------------------------------------------------------------
# 3. CI wiring: the guard runs in the CI workflow
# ---------------------------------------------------------------------------


def test_ci_workflow_runs_secret_file_guard():
    ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    ci = yaml.safe_load(ci_path.read_text(encoding="utf-8"))
    run_steps = [
        step.get("run", "")
        for job in ci["jobs"].values()
        for step in job.get("steps", [])
    ]
    assert any("check_no_tracked_secrets.py" in run for run in run_steps), (
        "CI workflow must run scripts/check_no_tracked_secrets.py so a "
        "re-committed .env fails the build"
    )
