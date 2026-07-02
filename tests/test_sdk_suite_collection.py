"""Meta-tests: no nested pytest suite may be silently orphaned.

Review action point (P1): ``sdks/python/pyproject.toml`` defines its own
``[tool.pytest.ini_options]`` with ``testpaths=["tests"]``, while the root
config's ``testpaths=["tests"]`` means a top-level ``uv run pytest`` never
discovers that suite — and no CI workflow runs it either, so those tests
silently never execute.

Accepted end states (either satisfies the suite, per nested suite):

1. Root collection — the root ``[tool.pytest.ini_options].testpaths`` is
   widened so the nested suite's test directory is collected by a top-level
   ``uv run pytest``.
2. Explicit CI delegation — a GitHub Actions workflow that triggers on
   push/pull_request (not manual-only) runs pytest against the nested suite
   (naming its path as an argument or via ``working-directory``).

A nested suite that is removed or folded into the root ``tests/`` tree stops
being detected and these tests pass vacuously.

These tests parse the TOML/workflow files directly (no pytest subprocess) so
they are cheap and deterministic.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT_PYPROJECT = REPO_ROOT / "pyproject.toml"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# Top-level areas that can host their own Python distributions with a nested
# pytest configuration. Keep in sync with [tool.uv.workspace].members plus the
# independently released SDKs.
_NESTED_SUITE_AREAS = ("sdks", "packages", "apps", "examples")

# A workflow only counts as CI delegation if it runs automatically on code
# changes; a manual-only (workflow_dispatch) hook still leaves the suite
# effectively skipped.
_AUTO_TRIGGER_RE = re.compile(
    r"^\s*(push|pull_request|pull_request_target|merge_group|schedule)\s*:", re.MULTILINE
)


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _pytest_ini_options(pyproject: dict[str, Any]) -> dict[str, Any] | None:
    return pyproject.get("tool", {}).get("pytest", {}).get("ini_options")


def _testpaths(ini_options: dict[str, Any]) -> list[str]:
    testpaths = ini_options.get("testpaths", [])
    if isinstance(testpaths, str):
        return testpaths.split()
    return [str(item) for item in testpaths]


def _has_tests(directory: Path) -> bool:
    return directory.is_dir() and any(directory.rglob("test_*.py"))


def _nested_suites() -> dict[str, list[Path]]:
    """Nested pytest suites keyed by repo-relative distribution directory.

    A nested suite is a non-root ``pyproject.toml`` under one of the known
    workspace/SDK areas that declares ``[tool.pytest.ini_options]`` and whose
    resolved testpaths actually contain tests.
    """
    suites: dict[str, list[Path]] = {}
    for area in _NESTED_SUITE_AREAS:
        for pyproject_path in sorted((REPO_ROOT / area).glob("*/pyproject.toml")):
            ini_options = _pytest_ini_options(_load_toml(pyproject_path))
            if ini_options is None:
                continue
            base = pyproject_path.parent
            test_dirs = [
                (base / testpath).resolve() for testpath in _testpaths(ini_options) or ["tests"]
            ]
            test_dirs = [d for d in test_dirs if _has_tests(d)]
            if test_dirs:
                suites[base.relative_to(REPO_ROOT).as_posix()] = test_dirs
    return suites


def _root_collected_dirs() -> list[Path]:
    ini_options = _pytest_ini_options(_load_toml(ROOT_PYPROJECT)) or {}
    return [(REPO_ROOT / testpath).resolve() for testpath in _testpaths(ini_options)]


def _is_root_collected(test_dir: Path) -> bool:
    for collected in _root_collected_dirs():
        if test_dir == collected or collected in test_dir.parents:
            return True
    return False


def _delegating_workflows(suite_dir: str) -> list[str]:
    """Workflows that auto-run on code changes and invoke pytest for the suite."""
    if not WORKFLOWS_DIR.is_dir():
        return []
    delegating = []
    for workflow in sorted(WORKFLOWS_DIR.glob("*.y*ml")):
        text = workflow.read_text(encoding="utf-8")
        if "pytest" not in text:
            continue
        if suite_dir not in text:
            continue
        if not _AUTO_TRIGGER_RE.search(text):
            continue
        delegating.append(workflow.name)
    return delegating


def _is_reachable(suite_dir: str, test_dirs: list[Path]) -> bool:
    if all(_is_root_collected(test_dir) for test_dir in test_dirs):
        return True
    return bool(_delegating_workflows(suite_dir))


class TestNestedSuiteReachability:
    """Every nested pytest suite must be root-collected or CI-delegated."""

    def test_no_nested_suite_is_orphaned(self) -> None:
        orphaned = [
            suite_dir
            for suite_dir, test_dirs in _nested_suites().items()
            if not _is_reachable(suite_dir, test_dirs)
        ]
        assert not orphaned, (
            f"Nested pytest suites {orphaned} are never executed: the root "
            "[tool.pytest.ini_options].testpaths does not collect them and no "
            "push/pull_request-triggered workflow in .github/workflows runs pytest "
            "against them. Widen root testpaths or add an explicit CI job so the "
            "suite is not silently skipped."
        )

    def test_python_sdk_suite_is_reachable(self) -> None:
        """The known orphan from the review digest: sdks/python."""
        suites = _nested_suites()
        if "sdks/python" not in suites:
            return  # SDK suite removed or folded into the root tree — nothing to wire
        assert _is_reachable("sdks/python", suites["sdks/python"]), (
            "sdks/python defines its own [tool.pytest.ini_options] with "
            "testpaths=['tests'], but a top-level `uv run pytest` never discovers it "
            "(root testpaths=['tests'] only) and no automatically triggered workflow "
            "delegates to it. Either widen root collection to include the SDK suite "
            "or add an explicit CI job (e.g. `uv run pytest` with "
            "working-directory: sdks/python)."
        )
