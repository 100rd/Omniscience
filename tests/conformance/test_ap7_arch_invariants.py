"""AP7 conformance tests — architecture invariants (single-writer + MCP retrieval-only).

AP7 invariants:
1. ``neo4j.AsyncDriver`` and ``AsyncQdrantClient`` must only be imported under
   ``omniscience_index/stores/`` (the single-writer boundary).  No other package
   or app may import these clients directly.
2. The MCP registered tool set is fully documented: every tool is tagged as
   retrieval or synthesis.  ``generate_postmortem`` is explicitly categorised
   as synthesis with a governance document in ``docs/decisions/``.

Implementation notes
--------------------
- (a) uses ``ast`` to walk ``apps/`` + ``packages/`` asserting no import of
  ``neo4j.AsyncDriver`` or ``AsyncQdrantClient`` outside the store layer.
- (b) enumerates MCP registered tools and checks each against an explicit
  categorisation table.  ``generate_postmortem`` is in the synthesis allowlist
  referencing ADR-0016 for governance.

The tests run from the project root (``Omniscience/``).  They are pure-static
(no containers) and complete in milliseconds.
"""

from __future__ import annotations

import ast
import pathlib

# ---------------------------------------------------------------------------
# Root of the repository (tests run from Omniscience/ working directory).
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(".")

# ---------------------------------------------------------------------------
# AP7 invariant (a): store-layer import boundary
# ---------------------------------------------------------------------------

#: Only files under this prefix are allowed to import the raw store drivers.
_STORE_LAYER_PREFIX = "packages/index/src/omniscience_index/stores"

#: Directories to skip (virtualenv, compiled bytecode, test fixtures).
_SKIP_DIR_FRAGMENTS = ("site-packages", ".venv", "__pycache__", "node_modules")


def _should_skip_file(rel: str) -> bool:
    return any(frag in rel for frag in _SKIP_DIR_FRAGMENTS)


def test_neo4j_driver_only_imported_in_stores() -> None:
    """AP7: neo4j.AsyncDriver is imported only under omniscience_index/stores/.

    AST walk of apps/ and packages/ (excluding the stores/ path) must find
    no import of neo4j.AsyncDriver or neo4j.AsyncBoltDriver.
    """
    violations: list[str] = []

    for py_file in _REPO_ROOT.rglob("*.py"):
        rel = str(py_file)
        if _STORE_LAYER_PREFIX in rel:
            continue
        if _should_skip_file(rel):
            continue
        # Only scan apps/ and packages/ — skip scripts/, tests/, etc.
        if not (rel.startswith("apps/") or rel.startswith("packages/")):
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

    assert not violations, (
        "neo4j AsyncDriver must only be imported in omniscience_index/stores/:\n"
        + "\n".join(violations)
    )


def test_qdrant_client_only_imported_in_stores() -> None:
    """AP7: AsyncQdrantClient is imported only under omniscience_index/stores/.

    AST walk of apps/ and packages/ (excluding the stores/ path) must find
    no import of AsyncQdrantClient.
    """
    violations: list[str] = []

    for py_file in _REPO_ROOT.rglob("*.py"):
        rel = str(py_file)
        if _STORE_LAYER_PREFIX in rel:
            continue
        if _should_skip_file(rel):
            continue
        # Only scan apps/ and packages/ — skip scripts/, tests/, etc.
        if not (rel.startswith("apps/") or rel.startswith("packages/")):
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

    assert not violations, (
        "AsyncQdrantClient must only be imported in omniscience_index/stores/:\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# AP7 invariant (b): MCP tool surface documentation
# ---------------------------------------------------------------------------

# Retrieval tools: read-only graph / vector / DB lookups.
_RETRIEVAL_TOOLS = frozenset(
    {
        "search",
        "get_document",
        "get_entity",
        "get_related_entities",
        "list_entities",
        "list_sources",
        "source_stats",
        "resolve_incident",
        "blast_radius",
        "replay_context",
        "incident_timeline",
        "suggest_runbook",
        "find_similar_incidents",
    }
)

# Synthesis tools: produce new artefacts / write through the ingestion pipeline.
# Each entry must reference an ADR that documents the governance boundary.
# format: {tool_name: "ADR-XXXX"}
_SYNTHESIS_TOOLS: dict[str, str] = {
    # generate_postmortem: assembles a post-mortem document from the bitemporal
    # timeline and persists FollowUp entities through the standard outbox.
    # Governance: docs/decisions/0016-generate-postmortem-synthesis-governance.md
    "generate_postmortem": "ADR-0016",
}

_ALL_KNOWN_TOOLS = _RETRIEVAL_TOOLS | frozenset(_SYNTHESIS_TOOLS)


def test_mcp_tool_surface_is_documented() -> None:
    """AP7: every MCP-registered tool is tagged as retrieval or synthesis.

    No tool may appear on the MCP surface without an explicit category tag.
    Synthesis tools must have documented governance (an ADR in docs/decisions/).
    """
    # We introspect the FastMCP tool registry to enumerate registered tools.
    # This import is fast (no IO, no DB connections).
    from omniscience_server.mcp.server import mcp_server

    registered_tools = {t.name for t in mcp_server._tool_manager.list_tools()}

    unknown = registered_tools - _ALL_KNOWN_TOOLS
    assert not unknown, (
        "The following MCP tools are registered but not categorised as retrieval "
        "or synthesis.  Add them to _RETRIEVAL_TOOLS or _SYNTHESIS_TOOLS in "
        "tests/conformance/test_ap7_arch_invariants.py with a governance note:\n"
        + "\n".join(sorted(unknown))
    )


def test_generate_postmortem_has_synthesis_governance_doc() -> None:
    """AP7: generate_postmortem is categorised as synthesis with a governance doc.

    The tool may remain on the MCP surface only if:
    1. It is in the _SYNTHESIS_TOOLS allowlist in this file.
    2. A governance document in docs/decisions/ exists referencing ADR-0016.
    """
    # 1. Must be in the allowlist.
    assert "generate_postmortem" in _SYNTHESIS_TOOLS, (
        "generate_postmortem must be in _SYNTHESIS_TOOLS allowlist"
    )
    assert _SYNTHESIS_TOOLS["generate_postmortem"] == "ADR-0016", (
        "generate_postmortem must reference ADR-0016 for governance"
    )

    # 2. Governance doc must exist.
    governance_doc = _REPO_ROOT / "docs/decisions/0016-generate-postmortem-synthesis-governance.md"
    assert governance_doc.exists(), (
        f"Governance document not found: {governance_doc}\n"
        "Create docs/decisions/0016-generate-postmortem-synthesis-governance.md "
        "to document the synthesis boundary for generate_postmortem."
    )

    # 3. Governance doc must reference generate_postmortem.
    content = governance_doc.read_text(encoding="utf-8")
    assert "generate_postmortem" in content, (
        "Governance document must reference generate_postmortem"
    )
    assert "synthesis" in content.lower(), "Governance document must use the word 'synthesis'"
