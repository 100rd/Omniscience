"""Tests for the enforced coverage floor (review action point: gate test
coverage with --cov-fail-under).

Contract under test:

``[tool.pytest.ini_options] addopts`` in the root pyproject.toml already
collects coverage (``--cov``) but sets no floor, so coverage can silently
regress on any pytest invocation that does not go through the one CI command
line that passes ``--cov-fail-under=80`` explicitly.

Acceptance criteria encoded here:

1. ``addopts`` contains a ``--cov-fail-under=<N>`` flag, turning the existing
   measurement into an enforced gate for every default pytest run.
2. The floor ``N`` is a real percentage: a number with ``0 < N <= 100``.
3. The floor does not undercut the gate CI already enforces on its explicit
   command line (currently ``--cov-fail-under=80`` in
   .github/workflows/ci.yml) — the baseline may only ratchet upward.
4. ``--cov`` stays in ``addopts`` — a fail-under flag without coverage
   collection would make the gate vacuous.
"""

import shlex
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent


def _pytest_addopts() -> list[str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = data["tool"]["pytest"]["ini_options"].get("addopts", "")
    return shlex.split(addopts)


def _cov_fail_under(args: list[str]) -> str | None:
    """Extract the --cov-fail-under value from a pytest argument list.

    Supports both ``--cov-fail-under=80`` and ``--cov-fail-under 80`` forms;
    the last occurrence wins, matching pytest semantics.
    """
    value = None
    for i, arg in enumerate(args):
        if arg.startswith("--cov-fail-under="):
            value = arg.split("=", 1)[1]
        elif arg == "--cov-fail-under" and i + 1 < len(args):
            value = args[i + 1]
    return value


def _ci_fail_under_floors() -> list[float]:
    """Every --cov-fail-under value passed explicitly in CI run steps."""
    ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    ci = yaml.safe_load(ci_path.read_text(encoding="utf-8"))
    floors = []
    for job in ci["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run", "")
            if "--cov-fail-under" not in run:
                continue
            for line in run.splitlines():
                if "--cov-fail-under" in line:
                    value = _cov_fail_under(shlex.split(line.strip()))
                    if value is not None:
                        floors.append(float(value))
    return floors


def test_addopts_set_a_coverage_floor():
    """pyproject addopts must carry --cov-fail-under so every default pytest
    run enforces the floor, not just the one CI command line."""
    args = _pytest_addopts()
    assert _cov_fail_under(args) is not None, (
        "pyproject.toml [tool.pytest.ini_options] addopts collects coverage "
        "but sets no floor — add --cov-fail-under=<N> (start at the current "
        "measured coverage as the baseline and ratchet upward)"
    )


def test_coverage_floor_is_a_valid_percentage():
    value = _cov_fail_under(_pytest_addopts())
    assert value is not None, "addopts must set --cov-fail-under (see test above)"
    floor = float(value)
    assert 0 < floor <= 100, (
        f"--cov-fail-under={value} is not an enforceable percentage: the "
        "floor must be greater than 0 (a zero floor gates nothing) and at "
        "most 100"
    )


def test_coverage_floor_does_not_undercut_ci_gate():
    """CI already fails the build below 80% via an explicit command-line flag;
    the pyproject baseline must not regress below any floor CI enforces."""
    value = _cov_fail_under(_pytest_addopts())
    assert value is not None, "addopts must set --cov-fail-under (see test above)"
    floor = float(value)
    for ci_floor in _ci_fail_under_floors():
        assert floor >= ci_floor, (
            f"pyproject sets --cov-fail-under={floor} but CI already enforces "
            f"--cov-fail-under={ci_floor}; the baseline may only ratchet "
            "upward, never below the existing gate"
        )


def test_addopts_still_collect_coverage():
    """A fail-under floor is only meaningful while --cov stays in addopts."""
    args = _pytest_addopts()
    assert any(arg == "--cov" or arg.startswith("--cov=") for arg in args), (
        "addopts must keep --cov: without coverage collection the "
        "--cov-fail-under gate is vacuous"
    )
