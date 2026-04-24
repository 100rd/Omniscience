"""Static lint guard: raw ``qdrant_client.models.Filter`` construction
outside the filter-builder module is forbidden.

ADR-0006 §ACL carry-forward mandates: "Add a linter or review rule
that rejects construction of Qdrant ``Filter`` objects that omit
``workspace_id`` in the ``must`` clause; prefer a thin typed
filter-builder that takes ``workspace_id`` as a required constructor
argument."

This test is the mechanised form of that rule.  It walks every
``.py`` under ``apps/``, ``packages/``, and ``tests/`` and asserts
that only the allow-listed files construct ``qm.Filter(...)`` /
``Filter(...)`` directly.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

# Files permitted to construct a raw Qdrant Filter.  These are the
# ONLY exceptions; adding a new path requires a PR that explains why
# the construction cannot go through ``QdrantFilterBuilder``.
_ALLOWED_FILES: Final[frozenset[str]] = frozenset(
    {
        # The filter-builder itself.
        "packages/index/src/omniscience_index/stores/qdrant_filters.py",
        # The lint test itself references Filter in strings / type names.
        "tests/test_qdrant_filter_builder_lint.py",
        # Unit tests for the builder that inspect .must / Filter shape.
        "tests/test_qdrant_filter_builder.py",
    }
)

_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_SCAN_DIRS: Final[tuple[Path, ...]] = (
    _ROOT / "apps",
    _ROOT / "packages",
    _ROOT / "tests",
)


def _is_filter_construction(node: ast.Call) -> bool:
    """Return True iff ``node`` is a call to ``Filter(...)`` or ``qm.Filter(...)``.

    This matches both the bare name (``from qdrant_client.models import
    Filter``) and the qualified attribute (``qm.Filter`` / ``models.Filter``).
    """
    func = node.func
    # Bare name: ``from qdrant_client.models import Filter``.
    if isinstance(func, ast.Name) and func.id == "Filter":
        return True
    # Attribute access: ``qm.Filter`` / ``models.Filter``.  Only flag
    # attribute access spelt ``.Filter``; other libs could reuse the
    # name but the ``qdrant_client`` import gate below keeps false
    # positives out of scope.
    return bool(isinstance(func, ast.Attribute) and func.attr == "Filter")


def _file_imports_qdrant(tree: ast.Module) -> bool:
    """Cheap check: the file imports from ``qdrant_client`` (or an alias)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("qdrant_client"):
            return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("qdrant_client"):
                    return True
    return False


def _scan_file(path: Path) -> list[int]:
    """Return the line numbers of offending ``Filter(...)`` calls in ``path``."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:  # pragma: no cover — defensive only
        return []
    if not _file_imports_qdrant(tree):
        return []
    offenders: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_filter_construction(node):
            offenders.append(node.lineno)
    return offenders


def test_no_raw_qdrant_filter_construction_outside_builder() -> None:
    """ADR-0006 §ACL: only ``qdrant_filters.py`` may construct a ``Filter``."""
    offenders: list[str] = []
    for scan_dir in _SCAN_DIRS:
        if not scan_dir.exists():
            continue
        for path in scan_dir.rglob("*.py"):
            rel = path.relative_to(_ROOT).as_posix()
            if rel in _ALLOWED_FILES:
                continue
            for lineno in _scan_file(path):
                offenders.append(f"{rel}:{lineno}")

    assert not offenders, (
        "Raw qdrant_client Filter construction detected outside the "
        "sanctioned filter-builder module.  Route through "
        "``QdrantFilterBuilder`` or, for admin sweeps, through "
        "``build_tombstone_sweep_filter``.  Offending sites: " + ", ".join(offenders)
    )
