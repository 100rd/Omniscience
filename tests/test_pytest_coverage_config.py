"""Tests for the repo-wide pytest coverage configuration in pyproject.toml.

Guards against `--cov` regressing to a bare, target-less flag (which makes
measured scope depend on the invoking cwd) and against the CI-only
`--cov-fail-under` gate silently disappearing from the config that local
runs and CI both inherit from.
"""

from __future__ import annotations

import shlex
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _pytest_addopts() -> list[str]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    addopts = pyproject["tool"]["pytest"]["ini_options"]["addopts"]
    assert isinstance(addopts, str)
    return shlex.split(addopts)


def test_addopts_does_not_use_bare_cov_flag() -> None:
    """`--cov` with no target measures whatever the cwd happens to be.

    The flag must always carry an explicit `--cov=<target>` argument so
    coverage scope is stable regardless of the directory pytest is invoked
    from.
    """
    opts = _pytest_addopts()
    assert "--cov" not in opts, (
        "addopts uses a bare '--cov' with no target; use '--cov=<package>' "
        "for one or more explicit targets instead"
    )


def test_addopts_declares_explicit_cov_targets() -> None:
    """At least one explicit --cov=<target> must be configured."""
    opts = _pytest_addopts()
    cov_targets = [opt.removeprefix("--cov=") for opt in opts if opt.startswith("--cov=")]
    assert cov_targets, "addopts must declare at least one explicit --cov=<target>"

    for target in cov_targets:
        target_path = REPO_ROOT / target
        assert target_path.exists(), (
            f"--cov target {target!r} does not resolve to a path in the repo"
        )


def test_addopts_declares_cov_fail_under_floor() -> None:
    """A --cov-fail-under floor must be baked into addopts.

    Without this, coverage regressions are only caught when the invoking
    command happens to append its own --cov-fail-under (e.g. in CI), so a
    local `pytest` run or a differently-configured CI job would not fail on
    a coverage drop.
    """
    opts = _pytest_addopts()
    fail_under_opts = [opt for opt in opts if opt.startswith("--cov-fail-under=")]
    assert len(fail_under_opts) == 1, (
        "addopts must declare exactly one --cov-fail-under=<n> floor"
    )

    value = fail_under_opts[0].removeprefix("--cov-fail-under=")
    assert value.isdigit(), f"--cov-fail-under value must be numeric, got {value!r}"
    floor = int(value)
    assert 0 < floor <= 100, f"--cov-fail-under floor {floor} is out of range"
