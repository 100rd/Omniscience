"""Guards against the debug_fixture.py root-script hygiene issue regressing.

The original ``debug_fixture.py`` (repo root):
- lived at the repo root instead of ``scripts/``, violating repo layout
  conventions (one-off operational scripts belong in ``scripts/``).
- imported a private test helper (``tests.test_store_contract._full_store_factory``)
  from non-test code, coupling a script to test internals.
- caught a bare ``Exception``, printed a traceback, and fell through to a
  normal return - so ``asyncio.run(main())`` and the process exit with
  status 0 even when the store failed to construct.

The action point accepts two remediations: delete the script outright, or
keep it under ``scripts/`` with the failure mode fixed (no bare exception
swallowing, non-zero exit on failure). These tests accept either outcome
but forbid the original violations.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent


def _find_debug_fixture() -> Path | None:
    scripts_path = REPO_ROOT / "scripts" / "debug_fixture.py"
    if scripts_path.exists():
        return scripts_path
    return None


def test_debug_fixture_is_not_at_repo_root():
    assert not (REPO_ROOT / "debug_fixture.py").exists(), (
        "debug_fixture.py must not live at the repo root; either delete it "
        "entirely or move it under scripts/ (see repo layout conventions)."
    )


def test_debug_fixture_does_not_import_private_test_helpers():
    script = _find_debug_fixture()
    if script is None:
        pytest.skip("debug_fixture.py was removed entirely")

    contents = script.read_text(encoding="utf-8")
    assert "tests.test_store_contract" not in contents, (
        "debug_fixture.py must not import private test helpers such as "
        "_full_store_factory from the tests package."
    )
    assert "_full_store_factory" not in contents


def _is_exit_call(stmt: ast.stmt) -> bool:
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return False
    func = stmt.value.func
    if isinstance(func, ast.Attribute):
        return func.attr == "exit"
    if isinstance(func, ast.Name):
        return func.id == "exit"
    return False


def test_debug_fixture_does_not_swallow_bare_exceptions():
    script = _find_debug_fixture()
    if script is None:
        pytest.skip("debug_fixture.py was removed entirely")

    tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        is_bare_or_broad = node.type is None or (
            isinstance(node.type, ast.Name) and node.type.id == "Exception"
        )
        if not is_bare_or_broad:
            continue

        handles_failure = any(
            isinstance(stmt, ast.Raise) or _is_exit_call(stmt) for stmt in ast.walk(node)
        )
        assert handles_failure, (
            "debug_fixture.py must not catch a bare/broad Exception without "
            "re-raising or exiting non-zero; the original script printed a "
            "traceback and let the process exit 0 on failure."
        )
