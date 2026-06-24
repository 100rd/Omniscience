"""AP7 conformance tests — architecture invariants (single-writer + MCP retrieval-only).

AP7 invariants:
1. ``neo4j.AsyncDriver`` and ``AsyncQdrantClient`` must only be imported under
   ``omniscience_index/stores/`` (the single-writer boundary).  No other package
   or app may import these clients directly.
2. The MCP registered tool set is retrieval-only, OR any synthesis tools (e.g.
   ``generate_postmortem``) are explicitly categorised as synthesis with
   documented governance (not silently mixed with retrieval tools).

Implementation notes (for the implementer):
- Use ``ast`` to walk ``apps/`` and ``packages/`` (excluding
  ``omniscience_index/stores/``), asserting no import of
  ``neo4j.AsyncDriver`` or ``AsyncQdrantClient``.
- Enumerate MCP registered tools from ``mcp/server.py`` and assert each
  is tagged retrieval or synthesis (enum/literal field in tool registry).
- Governance doc for synthesis tools must exist in ``docs/decisions/`` or
  similar.

These tests are STUBS — they encode the intended invariant and are
marked xfail until AP7 is implemented in Batch F.  When the feature
lands: remove xfail, make tests pass.
"""

from __future__ import annotations

import ast
import pathlib

import pytest


@pytest.mark.xfail(reason="implemented in v9 Batch F", strict=False)
def test_neo4j_driver_only_imported_in_stores() -> None:
    """AP7: neo4j.AsyncDriver is imported only under omniscience_index/stores/.

    AST walk of apps/ and packages/ (excluding the stores/ path) must find
    no import of neo4j.AsyncDriver or neo4j.AsyncBoltDriver.
    """
    root = pathlib.Path(".")
    violations: list[str] = []

    allowed_prefix = "packages/index/src/omniscience_index/stores"

    for py_file in root.rglob("*.py"):
        rel = str(py_file)
        if allowed_prefix in rel:
            continue
        if "site-packages" in rel or ".venv" in rel or "__pycache__" in rel:
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("neo4j")
            ):
                for alias in node.names:
                    if "AsyncDriver" in alias.name or "AsyncBoltDriver" in alias.name:
                        violations.append(f"{rel}: imports {alias.name} from {node.module}")

    assert not violations, "neo4j AsyncDriver must only be imported in stores/:\n" + "\n".join(
        violations
    )


@pytest.mark.xfail(reason="implemented in v9 Batch F", strict=False)
def test_qdrant_client_only_imported_in_stores() -> None:
    """AP7: AsyncQdrantClient is imported only under omniscience_index/stores/.

    AST walk of apps/ and packages/ (excluding the stores/ path) must find
    no import of AsyncQdrantClient.
    """
    root = pathlib.Path(".")
    violations: list[str] = []

    allowed_prefix = "packages/index/src/omniscience_index/stores"

    for py_file in root.rglob("*.py"):
        rel = str(py_file)
        if allowed_prefix in rel:
            continue
        if "site-packages" in rel or ".venv" in rel or "__pycache__" in rel:
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "qdrant_client" in node.module:
                for alias in node.names:
                    if "AsyncQdrantClient" in alias.name:
                        violations.append(f"{rel}: imports {alias.name} from {node.module}")

    assert not violations, "AsyncQdrantClient must only be imported in stores/:\n" + "\n".join(
        violations
    )


@pytest.mark.xfail(reason="implemented in v9 Batch F", strict=False)
def test_mcp_tool_surface_is_documented() -> None:
    """AP7: every MCP-registered tool is tagged as retrieval or synthesis.

    No tool may appear on the MCP surface without an explicit category tag.
    Synthesis tools (e.g. generate_postmortem) must have documented governance.
    """
    raise NotImplementedError("AP7 MCP tool registry not yet implemented")


@pytest.mark.xfail(reason="implemented in v9 Batch F", strict=False)
def test_generate_postmortem_has_synthesis_governance_doc() -> None:
    """AP7: generate_postmortem is categorised as synthesis with a governance doc.

    The tool may remain on the MCP surface only if:
    1. It is tagged as ``category="synthesis"`` in the tool registry.
    2. A governance document in ``docs/decisions/`` or ``docs/adr/`` exists
       explaining the synthesis boundary and its review process.
    """
    raise NotImplementedError("AP7 generate_postmortem governance not yet documented")
