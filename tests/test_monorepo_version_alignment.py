"""Guards against monorepo package versions drifting from the release line.

The root `pyproject.toml` ("omniscience") is the canonical version for a
tagged release. Every publishable package under apps/, packages/, and sdks/
(Python via pyproject.toml, JS/TS via package.json) must declare the same
version so consumers on a tagged release aren't confused by stragglers.
"""

import json
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent


def _canonical_version() -> str:
    root_pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return root_pyproject["project"]["version"]


def _discover_pyproject_files() -> list[Path]:
    files = []
    for base in ("apps", "packages", "sdks"):
        base_dir = REPO_ROOT / base
        if not base_dir.is_dir():
            continue
        for path in sorted(base_dir.glob("*/pyproject.toml")):
            files.append(path)
    return files


def _discover_package_json_files() -> list[Path]:
    files = []
    for base in ("apps", "packages", "sdks"):
        base_dir = REPO_ROOT / base
        if not base_dir.is_dir():
            continue
        for path in sorted(base_dir.glob("*/package.json")):
            if "node_modules" in path.parts:
                continue
            files.append(path)
    return files


def _pyproject_id(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def _package_json_id(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


@pytest.mark.parametrize(
    "pyproject_path", _discover_pyproject_files(), ids=_pyproject_id
)
def test_python_package_version_matches_release_line(pyproject_path: Path) -> None:
    canonical = _canonical_version()
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    version = data["project"]["version"]

    assert version == canonical, (
        f"{pyproject_path.relative_to(REPO_ROOT)} is at version {version!r}, "
        f"which lags the {canonical!r} release line declared in the root pyproject.toml"
    )


@pytest.mark.parametrize(
    "package_json_path", _discover_package_json_files(), ids=_package_json_id
)
def test_js_package_version_matches_release_line(package_json_path: Path) -> None:
    canonical = _canonical_version()
    data = json.loads(package_json_path.read_text(encoding="utf-8"))
    version = data["version"]

    assert version == canonical, (
        f"{package_json_path.relative_to(REPO_ROOT)} is at version {version!r}, "
        f"which lags the {canonical!r} release line declared in the root pyproject.toml"
    )


def test_discovery_found_the_known_lagging_packages() -> None:
    """Sanity check that the scan actually reaches the packages this test guards.

    If this starts failing because the paths moved, update the discovery globs
    above rather than deleting the assertions in the tests above.
    """
    pyproject_ids = {_pyproject_id(p) for p in _discover_pyproject_files()}
    package_json_ids = {_package_json_id(p) for p in _discover_package_json_files()}

    assert "sdks/python/pyproject.toml" in pyproject_ids
    assert "apps/admin/package.json" in package_json_ids
    assert "sdks/typescript/package.json" in package_json_ids
