"""Structural invariant: ``bench/`` is the only benchmark *package*.

Review finding (P3): ``benchmarks/__init__.py`` was an empty holdover with
no content of its own — its only role was making ``benchmarks/`` importable
as a Python package, which duplicates what ``bench/`` already is (the
CI-wired suite, issue #241) and creates structural ambiguity between two
similarly-named top-level directories.

This does **not** mean the ``benchmarks/`` directory itself must disappear:
``benchmarks/test_bitemporal_perf.py`` is a real, actively referenced
performance-regression suite (ADR-0008, ``tests/property/README.md``) and
must survive this cleanup untouched. Only the empty package marker goes;
``bench/`` remains the single *package* namespace for benchmark tooling.
"""

from __future__ import annotations

import pathlib

_REPO_ROOT = pathlib.Path(".")


def test_benchmarks_dir_has_no_package_marker() -> None:
    """The empty ``benchmarks/__init__.py`` holdover must be removed."""
    stale_marker = _REPO_ROOT / "benchmarks" / "__init__.py"
    assert not stale_marker.exists(), (
        f"{stale_marker} is a stale, empty package marker. Remove it so "
        "bench/ remains the single benchmark *package* namespace."
    )


def test_bench_is_the_single_benchmark_package() -> None:
    """``bench/`` keeps its package marker — it is the CI-wired suite (#241)."""
    bench_marker = _REPO_ROOT / "bench" / "__init__.py"
    assert bench_marker.exists(), (
        f"{bench_marker} must exist: bench/ is the single benchmark package."
    )


def test_bitemporal_perf_suite_survives_the_cleanup() -> None:
    """Removing the stale package marker must not delete the real perf suite."""
    perf_suite = _REPO_ROOT / "benchmarks" / "test_bitemporal_perf.py"
    assert perf_suite.exists(), (
        f"{perf_suite} must be preserved — it is the documented "
        "bitemporal performance-regression suite (ADR-0008, "
        "tests/property/README.md), not part of the empty-namespace cleanup."
    )
