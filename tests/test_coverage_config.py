"""Coverage configuration hygiene: scoped sources and an enforced fail-under gate.

Guards the P1 review action point: root pyproject.toml must not run coverage
with a bare ``--cov`` (which instruments third-party/stdlib code and mis-maps
the src/ layout) and must carry a ``fail-under`` threshold so CI can actually
fail on coverage regressions. Coverage sources must enumerate every
first-party package under packages/*/src and apps/*/src, either via
``[tool.coverage.run] source`` or via explicit ``--cov=<target>`` flags.
"""

import shlex
import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"


@pytest.fixture(scope="module")
def pyproject() -> dict[str, Any]:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


@pytest.fixture(scope="module")
def addopts_tokens(pyproject: dict[str, Any]) -> list[str]:
    ini_options = pyproject.get("tool", {}).get("pytest", {}).get("ini_options", {})
    addopts = ini_options.get("addopts", "")
    if isinstance(addopts, str):
        return shlex.split(addopts)
    return [str(token) for token in addopts]


@pytest.fixture(scope="module")
def coverage_sources(pyproject: dict[str, Any], addopts_tokens: list[str]) -> list[str]:
    """All configured coverage targets, from coverage.run.source and --cov=<target> flags."""
    sources = list(pyproject.get("tool", {}).get("coverage", {}).get("run", {}).get("source", []))
    sources += [token.split("=", 1)[1] for token in addopts_tokens if token.startswith("--cov=")]
    return sources


def first_party_packages() -> list[str]:
    """Import names of every first-party src-layout package in the workspace."""
    packages = []
    for src in sorted(REPO_ROOT.glob("packages/*/src")) + sorted(REPO_ROOT.glob("apps/*/src")):
        for candidate in sorted(src.iterdir()):
            if (candidate / "__init__.py").is_file():
                packages.append(candidate.name)
    return packages


def test_workspace_has_first_party_packages() -> None:
    # Sanity check for the parametrized scope test: an empty discovery would
    # silently vacuously pass it.
    assert first_party_packages(), "expected src-layout packages under packages/ and apps/"


def test_coverage_has_explicit_source_scope(coverage_sources: list[str]) -> None:
    assert coverage_sources, (
        "coverage must be scoped to first-party code via [tool.coverage.run] source in "
        "pyproject.toml or explicit --cov=<pkg> targets in addopts; a bare --cov instruments "
        "third-party/stdlib code and mis-maps the src/ layout"
    )


@pytest.mark.parametrize("package", first_party_packages())
def test_every_first_party_package_is_covered(coverage_sources: list[str], package: str) -> None:
    # A source entry may be an import name ("omniscience_core") or a path
    # ("packages/core/src/omniscience_core"); accept either spelling.
    covered = any(
        source == package or source.replace("\\", "/").rstrip("/").endswith(f"/{package}")
        for source in coverage_sources
    )
    assert covered, (
        f"first-party package {package!r} is missing from the coverage source scope; "
        "add it to [tool.coverage.run] source (or an explicit --cov target) so its "
        "regressions count against the gate"
    )


def test_coverage_scope_excludes_tests(coverage_sources: list[str]) -> None:
    offenders = [
        source
        for source in coverage_sources
        if source == "tests" or source.replace("\\", "/").rstrip("/").endswith("/tests")
    ]
    assert not offenders, (
        f"coverage sources must target production packages, not the test suite: {offenders}"
    )


def test_coverage_fail_under_gate_is_enforced(
    pyproject: dict[str, Any], addopts_tokens: list[str]
) -> None:
    threshold = None
    for token in addopts_tokens:
        if token.startswith("--cov-fail-under="):
            threshold = float(token.split("=", 1)[1])
    if threshold is None:
        report = pyproject.get("tool", {}).get("coverage", {}).get("report", {})
        if "fail_under" in report:
            threshold = float(report["fail_under"])
    assert threshold is not None, (
        "coverage must enforce a regression gate: add --cov-fail-under=<pct> to addopts or "
        "fail_under to [tool.coverage.report] in pyproject.toml"
    )
    assert 0 < threshold <= 100, (
        f"coverage fail-under threshold must be a meaningful percentage in (0, 100], "
        f"got {threshold}; set it at or just below the measured baseline, then ratchet"
    )
