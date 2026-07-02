"""Meta-tests for the coverage configuration in the root pyproject.toml.

Review action point (P2): a bare ``--cov`` in ``[tool.pytest.ini_options].addopts``
leaves coverage collection unscoped — in a uv workspace the report picks up
extraneous imported modules — and makes pytest-cov a hard requirement without
acting as an actual gate.

Accepted end states (either satisfies the whole suite):

1. Coverage collection is removed from the global ``addopts`` entirely
   (moved to CI / an opt-in invocation).  All scoping/gate tests below then
   pass vacuously.
2. Coverage collection stays in ``addopts`` but is
   a) scoped to first-party code — ``--cov=packages --cov=apps`` (or
      equivalent per-package / module targets, or ``[tool.coverage.run].source``
      restricted to project code), covering BOTH the ``packages/`` and
      ``apps/`` workspace areas, and
   b) gated — ``--cov-fail-under=N`` in ``addopts`` or
      ``[tool.coverage.report].fail_under`` with a positive floor.

These tests parse the TOML directly (no pytest subprocess) so they are cheap
and deterministic.
"""

from __future__ import annotations

import shlex
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Flags that merely configure reporting/behaviour and do not name a target.
_NON_TARGET_COV_FLAGS = (
    "--cov-report",
    "--cov-fail-under",
    "--cov-branch",
    "--cov-append",
    "--cov-config",
    "--cov-context",
    "--cov-reset",
    "--no-cov",
    "--no-cov-on-fail",
)


def _load_pyproject() -> dict[str, Any]:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _addopts_tokens(pyproject: dict[str, Any]) -> list[str]:
    addopts = pyproject.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts", "")
    if isinstance(addopts, list):
        return [str(tok) for tok in addopts]
    return shlex.split(str(addopts))


def _coverage_run_sources(pyproject: dict[str, Any]) -> list[str]:
    source = pyproject.get("tool", {}).get("coverage", {}).get("run", {}).get("source", [])
    if isinstance(source, str):
        return [source]
    return [str(item) for item in source]


def _bare_cov_tokens(tokens: list[str]) -> list[str]:
    """``--cov`` tokens that enable collection without naming a target."""
    return [tok for tok in tokens if tok == "--cov"]


def _cov_targets(tokens: list[str]) -> list[str]:
    """Explicit collection targets: every ``--cov=<target>`` in addopts."""
    return [
        tok.split("=", 1)[1]
        for tok in tokens
        if tok.startswith("--cov=") and not tok.startswith(_NON_TARGET_COV_FLAGS)
    ]


def _cov_collection_enabled(tokens: list[str]) -> bool:
    return bool(_bare_cov_tokens(tokens) or _cov_targets(tokens))


def _fail_under(tokens: list[str], pyproject: dict[str, Any]) -> float | None:
    """The enforced coverage floor, from addopts or [tool.coverage.report]."""
    for idx, tok in enumerate(tokens):
        if tok.startswith("--cov-fail-under="):
            return float(tok.split("=", 1)[1])
        if tok == "--cov-fail-under" and idx + 1 < len(tokens):
            return float(tokens[idx + 1])
    report = pyproject.get("tool", {}).get("coverage", {}).get("report", {})
    fail_under = report.get("fail_under")
    return float(fail_under) if fail_under is not None else None


def _target_area(target: str) -> str | None:
    """Map a --cov target (path or module name) to a workspace area.

    Returns ``"packages"``, ``"apps"``, or ``None`` when the target does not
    correspond to first-party workspace code.
    """
    normalised = target.replace("\\", "/").strip("/")
    for area in ("packages", "apps"):
        if normalised == area or normalised.startswith(f"{area}/"):
            return area
    # Module-name targets (src layout): packages/*/src/<module>, apps/*/src/<module>.
    for area in ("packages", "apps"):
        if list((REPO_ROOT / area).glob(f"*/src/{target}")):
            return area
    return None


class TestCoverageScoping:
    """Coverage collection must not be a bare, unscoped ``--cov``."""

    def test_no_bare_cov_without_source_scoping(self) -> None:
        pyproject = _load_pyproject()
        tokens = _addopts_tokens(pyproject)
        bare = _bare_cov_tokens(tokens)
        if not bare:
            return
        assert _coverage_run_sources(pyproject), (
            "Bare --cov in [tool.pytest.ini_options].addopts collects coverage for every "
            "imported module in the uv workspace. Scope it explicitly: use "
            "--cov=packages --cov=apps (or set [tool.coverage.run].source), or move --cov "
            "out of the global addopts."
        )

    def test_cov_targets_are_first_party_only(self) -> None:
        pyproject = _load_pyproject()
        targets = _cov_targets(_addopts_tokens(pyproject)) + _coverage_run_sources(pyproject)
        foreign = [tgt for tgt in targets if _target_area(tgt) is None]
        assert not foreign, (
            f"Coverage targets {foreign} do not resolve to first-party workspace code "
            "(expected paths/modules under packages/ or apps/)."
        )

    def test_scoped_coverage_spans_packages_and_apps(self) -> None:
        pyproject = _load_pyproject()
        tokens = _addopts_tokens(pyproject)
        if not _cov_collection_enabled(tokens):
            return  # coverage moved out of global addopts — nothing to scope
        targets = _cov_targets(tokens) + _coverage_run_sources(pyproject)
        areas = {_target_area(tgt) for tgt in targets} - {None}
        assert areas >= {"packages", "apps"}, (
            f"Coverage collection is enabled in addopts but the configured targets "
            f"{targets or '<none>'} cover only {sorted(areas) or 'nothing'}; the report must "
            "span both the packages/ and apps/ workspace areas (e.g. --cov=packages "
            "--cov=apps) so it reflects the project's own code."
        )


class TestCoverageGate:
    """If pytest-cov is mandatory via addopts, it must enforce a floor."""

    def test_fail_under_gate_present_when_cov_enabled(self) -> None:
        pyproject = _load_pyproject()
        tokens = _addopts_tokens(pyproject)
        if not _cov_collection_enabled(tokens):
            return  # coverage moved out of global addopts — no gate required
        floor = _fail_under(tokens, pyproject)
        assert floor is not None, (
            "Coverage collection is mandatory (--cov in addopts) but there is no gate: add "
            "--cov-fail-under=N to addopts or fail_under to [tool.coverage.report]."
        )

    def test_fail_under_floor_is_positive(self) -> None:
        pyproject = _load_pyproject()
        tokens = _addopts_tokens(pyproject)
        if not _cov_collection_enabled(tokens):
            return
        floor = _fail_under(tokens, pyproject)
        assert floor is not None and floor > 0, (
            f"The coverage gate floor must be a positive percentage, got {floor!r}; a zero or "
            "missing floor means the mandatory pytest-cov dependency enforces nothing."
        )
