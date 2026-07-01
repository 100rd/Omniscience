"""Tests for narrowly-scoped ruff suppressions (review action point:
tighten broad linter suppressions).

Contract under test:

The root pyproject.toml currently suppresses security-relevant ruff rules
far more broadly than the code requires:

* ``S104`` (binding to all interfaces) is ignored *globally* under
  ``[tool.ruff.lint].ignore``, although only a couple of entrypoint files
  actually bind ``0.0.0.0``.
* The ``tests/*`` block under ``[tool.ruff.lint.per-file-ignores]``
  disables the hardcoded-password checks (``S105``/``S106``/``S107``),
  the insecure-temp-file check (``S108``) and the blind-exception
  assertion check (``B017``) for the entire test tree, silencing the
  rules even in test files that never trip them.

Acceptance criteria encoded here:

1. ``S104`` is no longer in the global ignore list — it must be scoped
   via per-line ``# noqa: S104`` or per-file-ignores entries naming the
   specific files that bind all interfaces.
2. Any per-file-ignores entry that carries ``S104`` names a concrete
   ``.py`` file (no wildcard globs sweeping in whole directories).
3. The ``tests/*`` per-file-ignores block no longer disables
   ``S105``/``S106``/``S107``/``S108``/``B017``; individual test files
   that genuinely need a suppression use per-line ``# noqa`` (or, at
   most, a per-file-ignores entry naming that one file).
4. The narrowing is not undone by relocation: none of those codes may
   reappear in the global ignore list or under a new broad wildcard
   pattern covering the test tree.

Defensible suppressions stay untouched: ``S101`` (pytest asserts),
``S306``/``S603``/``S607`` in tests, and the already-scoped
``scripts/*``/``benchmarks/*``/``helm/**`` blocks are out of scope.
"""

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# Codes the review flagged as too broadly disabled for the whole test tree.
TESTS_BANNED_CODES = frozenset({"S105", "S106", "S107", "S108", "B017"})


def _ruff_lint_config() -> dict:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["ruff"]["lint"]


def _global_ignore() -> list[str]:
    return _ruff_lint_config().get("ignore", [])


def _per_file_ignores() -> dict[str, list[str]]:
    lint = _ruff_lint_config()
    merged: dict[str, list[str]] = {}
    for key in ("per-file-ignores", "extend-per-file-ignores"):
        merged.update(lint.get(key, {}))
    return merged


def _is_broad_test_glob(pattern: str) -> bool:
    """A pattern that sweeps in the whole test tree rather than one file."""
    normalized = pattern.lstrip("./")
    if "*" not in normalized and "?" not in normalized:
        return False  # names a concrete file — narrow by construction
    return normalized in {"*", "**", "**/*"} or normalized.startswith("tests")


def test_s104_is_not_globally_ignored():
    """S104 must not blanket the whole repo: only the entrypoints that
    deliberately bind 0.0.0.0 for containerised deployment need it."""
    assert "S104" not in _global_ignore(), (
        "S104 (hardcoded-bind-all-interfaces) is disabled repo-wide in "
        "[tool.ruff.lint].ignore — scope it to the specific entrypoint "
        "files that bind 0.0.0.0 via per-line '# noqa: S104' or "
        "per-file-ignores entries naming those files"
    )


def test_s104_per_file_entries_name_concrete_files():
    """If S104 moves to per-file-ignores, each carrying pattern must point
    at a single concrete .py file, not a wildcard over a directory tree."""
    for pattern, codes in _per_file_ignores().items():
        if "S104" not in codes:
            continue
        assert "*" not in pattern and "?" not in pattern, (
            f"per-file-ignores pattern {pattern!r} suppresses S104 via a "
            "wildcard glob — name the concrete file(s) that bind all "
            "interfaces instead"
        )
        assert pattern.endswith(".py"), (
            f"per-file-ignores pattern {pattern!r} suppresses S104 but does "
            "not name a specific Python file"
        )


def test_tests_block_keeps_hardcoded_password_checks_enabled():
    """S105/S106/S107 must not be disabled for the entire test tree; test
    fixtures that use placeholder secrets take a per-line noqa."""
    for pattern, codes in _per_file_ignores().items():
        if not _is_broad_test_glob(pattern):
            continue
        leaked = sorted(set(codes) & {"S105", "S106", "S107"})
        assert not leaked, (
            f"per-file-ignores pattern {pattern!r} disables the "
            f"hardcoded-password checks {leaked} for the whole test tree — "
            "use per-line '# noqa: S10x' on the fixture lines that need it"
        )


def test_tests_block_keeps_insecure_tempfile_check_enabled():
    """S108 must not be disabled for the entire test tree; the handful of
    tests asserting on literal /tmp paths take a per-line noqa."""
    for pattern, codes in _per_file_ignores().items():
        if not _is_broad_test_glob(pattern):
            continue
        assert "S108" not in codes, (
            f"per-file-ignores pattern {pattern!r} disables S108 "
            "(insecure temp file) for the whole test tree — use per-line "
            "'# noqa: S108' where a literal temp path is intentional"
        )


def test_tests_block_keeps_blind_exception_check_enabled():
    """B017 must not be disabled for the entire test tree; the inherited
    contract tests that assert on bare Exception take a per-line noqa."""
    for pattern, codes in _per_file_ignores().items():
        if not _is_broad_test_glob(pattern):
            continue
        assert "B017" not in codes, (
            f"per-file-ignores pattern {pattern!r} disables B017 "
            "(assert blind exception) for the whole test tree — use "
            "per-line '# noqa: B017' in the contract tests that need it"
        )


def test_banned_codes_are_not_relocated_to_the_global_ignore():
    """Removing codes from tests/* must not be 'fixed' by promoting them to
    the repo-wide ignore list, which would widen the suppression instead."""
    relocated = sorted(TESTS_BANNED_CODES & set(_global_ignore()))
    assert not relocated, (
        f"{relocated} moved to the global [tool.ruff.lint].ignore list — "
        "that widens the suppression from tests/* to the whole repo; use "
        "per-line noqa or concrete per-file entries instead"
    )


def test_defensible_test_suppressions_are_preserved():
    """The narrowing must not overshoot: S101 (pytest assert) stays disabled
    for the test tree, or the whole suite starts failing lint."""
    broad_test_patterns = [
        pattern for pattern in _per_file_ignores() if _is_broad_test_glob(pattern)
    ]
    assert any(
        "S101" in _per_file_ignores()[pattern] for pattern in broad_test_patterns
    ), (
        "the test tree must keep an S101 suppression (assert is the "
        "standard pytest assertion mechanism) — only the over-broad codes "
        "were meant to be removed"
    )
