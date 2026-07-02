"""Test-collection hygiene: a green root run must not hide unrun suites.

Guards the P1 review action point: ``testpaths = ["tests"]`` silently orphans
any test tree living outside ``tests/`` (today: ``benchmarks/``, the helm chart
assertions, the standalone Python SDK suite). The root pyproject.toml must
carry an explicit manifest dispositioning EVERY tree that contains pytest
files, so a future co-located package suite cannot appear on disk without
either joining the root run or being excluded with a documented reason:

    [tool.omniscience.test-collection]
    collected = ["tests"]                # must mirror pytest testpaths exactly

    [tool.omniscience.test-collection.excluded]
    "benchmarks" = "<why this tree is not part of the root run, and where it runs>"

On top of the static manifest, the root collection is validated observably:
``pytest --collect-only`` must succeed, reach every collected tree, and must
not leak nodes from excluded trees into the root run.
"""

import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Directories that can never hold first-party test trees.
PRUNED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    "site-packages",
}


@pytest.fixture(scope="module")
def pyproject() -> dict[str, Any]:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


@pytest.fixture(scope="module")
def testpaths(pyproject: dict[str, Any]) -> list[str]:
    ini_options = pyproject.get("tool", {}).get("pytest", {}).get("ini_options", {})
    paths = ini_options.get("testpaths", [])
    if isinstance(paths, str):
        paths = paths.split()
    return [str(path) for path in paths]


@pytest.fixture(scope="module")
def manifest(pyproject: dict[str, Any]) -> dict[str, Any]:
    """The explicit test-tree disposition manifest; its absence is the defect."""
    section = (
        pyproject.get("tool", {}).get("omniscience", {}).get("test-collection")
    )
    assert section is not None, (
        "pyproject.toml must declare [tool.omniscience.test-collection] enumerating every "
        "test tree in the repository: 'collected' (trees run by the root pytest invocation, "
        "mirroring testpaths) and 'excluded' (a table of tree -> reason for suites that "
        "intentionally run elsewhere, e.g. helm chart assertions or the standalone SDK); "
        "without it, a green root run can silently hide unrun suites"
    )
    return section


@pytest.fixture(scope="module")
def collected_trees(manifest: dict[str, Any]) -> list[str]:
    trees = manifest.get("collected", [])
    assert isinstance(trees, list) and trees, (
        "[tool.omniscience.test-collection] must declare a non-empty 'collected' list of "
        "trees the root pytest run collects"
    )
    return [str(tree).replace("\\", "/").rstrip("/") for tree in trees]


@pytest.fixture(scope="module")
def excluded_trees(manifest: dict[str, Any]) -> dict[str, str]:
    excluded = manifest.get("excluded", {})
    assert isinstance(excluded, dict), (
        "[tool.omniscience.test-collection].excluded must be a table mapping each "
        "intentionally-excluded test tree to the reason it does not join the root run"
    )
    return {
        str(tree).replace("\\", "/").rstrip("/"): str(reason)
        for tree, reason in excluded.items()
    }


def discovered_test_files() -> list[str]:
    """Repo-relative POSIX paths of every pytest-style test file on disk."""
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in PRUNED_DIRS and not d.startswith(".")]
        for filename in filenames:
            is_test_module = filename.endswith(".py") and (
                filename.startswith("test_") or filename.endswith("_test.py")
            )
            if is_test_module:
                relpath = (Path(dirpath) / filename).relative_to(REPO_ROOT)
                files.append(relpath.as_posix())
    return sorted(files)


def _under(path: str, tree: str) -> bool:
    return path == tree or path.startswith(tree + "/")


def test_discovery_finds_test_files() -> None:
    # Sanity check: an empty discovery would vacuously pass the disposition
    # guard below and defeat the whole point of this module.
    files = discovered_test_files()
    assert any(_under(f, "tests") for f in files), (
        "expected to discover the root tests/ suite; the on-disk discovery walk is broken"
    )


def test_testpaths_is_explicit(testpaths: list[str]) -> None:
    assert testpaths, (
        "pytest testpaths must be explicitly configured in pyproject.toml; relying on "
        "rootdir-wide auto-discovery makes the collected surface implicit and fragile"
    )


def test_collected_manifest_mirrors_testpaths(
    collected_trees: list[str], testpaths: list[str]
) -> None:
    normalized_testpaths = sorted(p.replace("\\", "/").rstrip("/") for p in testpaths)
    assert sorted(collected_trees) == normalized_testpaths, (
        f"the manifest's collected trees {sorted(collected_trees)} must exactly mirror pytest "
        f"testpaths {normalized_testpaths}; a drift between them means the documented intent "
        "and the actual collection no longer agree"
    )


def test_every_test_tree_is_dispositioned(
    collected_trees: list[str], excluded_trees: dict[str, str]
) -> None:
    known_trees = collected_trees + list(excluded_trees)
    orphans = [
        path
        for path in discovered_test_files()
        if not any(_under(path, tree) for tree in known_trees)
    ]
    assert not orphans, (
        f"test files exist outside every dispositioned tree: {orphans}; either add their tree "
        "to pytest testpaths + the manifest's 'collected' list, or add it to "
        "[tool.omniscience.test-collection].excluded with the reason it runs elsewhere — "
        "otherwise a green root run silently never executes them"
    )


def test_no_tree_is_both_collected_and_excluded(
    collected_trees: list[str], excluded_trees: dict[str, str]
) -> None:
    overlap = [
        (collected, excluded)
        for collected in collected_trees
        for excluded in excluded_trees
        if _under(collected, excluded) or _under(excluded, collected)
    ]
    assert not overlap, (
        f"manifest trees overlap between 'collected' and 'excluded': {overlap}; each tree "
        "must have exactly one disposition"
    )


def test_manifest_trees_exist_and_contain_tests(
    collected_trees: list[str], excluded_trees: dict[str, str]
) -> None:
    files = discovered_test_files()
    stale = [
        tree
        for tree in collected_trees + list(excluded_trees)
        if not any(_under(path, tree) for path in files)
    ]
    assert not stale, (
        f"manifest entries reference trees with no test files on disk: {stale}; remove stale "
        "entries so the manifest stays an accurate map of the real test surface"
    )


def test_excluded_trees_document_a_reason(excluded_trees: dict[str, str]) -> None:
    undocumented = [tree for tree, reason in excluded_trees.items() if not reason.strip()]
    assert not undocumented, (
        f"excluded test trees must state why they are outside the root run and where they DO "
        f"run: {undocumented}"
    )


@pytest.fixture(scope="module")
def root_collection(collected_trees: list[str]) -> list[str]:
    """Node ids from a real `pytest --collect-only` of the root configuration."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    tail = f"{result.stdout[-4000:]}\n{result.stderr[-4000:]}"
    assert result.returncode == 0, (
        "`pytest --collect-only` must succeed from the repo root — collection errors "
        f"mean part of the suite silently never runs:\n{tail}"
    )
    return [line.split("::", 1)[0] for line in result.stdout.splitlines() if "::" in line]


def test_root_collection_reaches_every_collected_tree(
    root_collection: list[str], collected_trees: list[str]
) -> None:
    unreached = [
        tree
        for tree in collected_trees
        if not any(_under(path.replace("\\", "/"), tree) for path in root_collection)
    ]
    assert not unreached, (
        f"pytest --collect-only collected zero tests from declared trees: {unreached}; the "
        "manifest claims they are part of the root run but collection never reaches them"
    )


def test_root_collection_does_not_leak_excluded_trees(
    root_collection: list[str], excluded_trees: dict[str, str]
) -> None:
    leaked = sorted(
        {
            tree
            for tree in excluded_trees
            for path in root_collection
            if _under(path.replace("\\", "/"), tree)
        }
    )
    assert not leaked, (
        f"trees documented as excluded are being collected by the root run anyway: {leaked}; "
        "either move them to 'collected' or fix testpaths so the manifest matches reality"
    )
