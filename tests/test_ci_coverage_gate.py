"""Guards against a silently-regressing coverage floor.

pytest-cov only fails a run when ``--cov-fail-under`` is set. If that flag
lives solely in one CI job's ad-hoc command line, it is easy to lose (job
rename, local `pytest` runs, other workflows that also collect coverage)
without anyone noticing, since a plain `--cov` run always exits 0 regardless
of coverage percentage. These tests assert the floor is declared in
pyproject.toml itself, so it applies by default everywhere pytest runs, and
that CI does not regress below that floor.
"""

import re
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _pytest_addopts() -> str:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return config["tool"]["pytest"]["ini_options"]["addopts"]


def _cov_fail_under(addopts: str) -> int | None:
    match = re.search(r"--cov-fail-under=(\d+)", addopts)
    return int(match.group(1)) if match else None


def test_pyproject_addopts_declares_a_coverage_floor():
    addopts = _pytest_addopts()
    threshold = _cov_fail_under(addopts)

    assert threshold is not None, (
        "pyproject.toml [tool.pytest.ini_options].addopts must set "
        "--cov-fail-under so a plain `pytest` run (locally or in any CI job) "
        "fails when coverage regresses, instead of relying on one job's "
        "command-line override."
    )


def test_pyproject_coverage_floor_is_meaningful():
    addopts = _pytest_addopts()
    threshold = _cov_fail_under(addopts)
    assert threshold is not None

    # A floor of 0 (or missing) can never fail a build; guard against a
    # no-op threshold being used to satisfy the letter of the check.
    assert threshold > 0
    assert threshold <= 100


def test_ci_workflow_test_job_does_not_undercut_pyproject_floor():
    """The CI 'test' job may override the threshold, but never downward."""
    addopts = _pytest_addopts()
    pyproject_threshold = _cov_fail_under(addopts)
    assert pyproject_threshold is not None

    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert "test" in jobs, "expected a 'test' job in .github/workflows/ci.yml"

    steps = jobs["test"]["steps"]
    pytest_steps = [
        step for step in steps if isinstance(step.get("run"), str) and "pytest" in step["run"]
    ]
    assert pytest_steps, "expected a pytest invocation in the CI 'test' job"

    for step in pytest_steps:
        run_line = step["run"]
        ci_threshold = _cov_fail_under(run_line)
        if ci_threshold is not None:
            assert ci_threshold >= pyproject_threshold, (
                f"CI pytest step sets --cov-fail-under={ci_threshold}, which is "
                f"lower than the pyproject.toml floor of {pyproject_threshold}; "
                "this would let CI pass PRs that a local run would reject."
            )
