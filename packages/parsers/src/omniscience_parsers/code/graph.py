"""Symbol graph extractor for code documents.

Transforms a :class:`~omniscience_parsers.base.ParsedDocument` produced by
:class:`~omniscience_parsers.code.treesitter.TreeSitterParser` into a pair of
lists — entities and edges — that represent the symbol graph of the source
file.

Extraction is **heuristic-based** (no LLM): the raw source text of each
section and the section ``symbol`` field are analysed with lightweight regex
and string matching.

For Python files the extractor identifies:
  - ``imports``   — ``import X`` / ``from X import Y`` statements
  - ``inherits``  — base classes listed in ``class Foo(Base1, Base2):``
  - ``calls``     — bare ``name(...)`` call expressions at the start of a line
                    or as the only expression (best-effort; avoids full AST)

Supported language: Python (``language == "python"``).
Other languages return empty lists so the pipeline degrades gracefully.

Entity / Edge are plain dataclasses (no SQLAlchemy) so this module has no
dependency on ``omniscience_core``.  The ingestion layer converts them to ORM
objects before persisting.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from omniscience_parsers.base import ParsedDocument, Section

# ---------------------------------------------------------------------------
# Data classes — lightweight; ORM-free
# ---------------------------------------------------------------------------


@dataclass
class ExtractedEntity:
    """A code entity extracted from a parsed document section.

    Attributes:
        id:           Stable UUID generated at extraction time.
        entity_type:  One of "function", "class", "module".
        name:         Fully-qualified name (FQN), e.g. "mymod.MyClass.method".
        display_name: Short name without module prefix, e.g. "method".
        symbol:       Raw symbol string from the :class:`Section` (same as *name*
                      in most cases; kept for traceability).
        metadata:     Arbitrary extra info (line range, language, …).
    """

    id: uuid.UUID
    entity_type: str
    name: str
    display_name: str
    symbol: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractedEdge:
    """A directed relationship between two :class:`ExtractedEntity` objects.

    Attributes:
        source_entity_id: UUID of the entity that originates the relationship.
        target_name:      FQN (or bare name) of the target entity.  The
                          ingestion layer resolves this to a real UUID after
                          all entities from all files are persisted.
        edge_type:        Relationship kind: "imports", "calls", "inherits",
                          "defines".
        metadata:         Arbitrary extra info.
    """

    source_entity_id: uuid.UUID
    target_name: str
    edge_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Regex patterns for Python source analysis
# ---------------------------------------------------------------------------

# import X  or  import X.Y.Z  or  import X as Y
_RE_IMPORT_SIMPLE = re.compile(r"^\s*import\s+([\w.]+)(?:\s+as\s+\w+)?\s*$", re.MULTILINE)

# from X import Y[, Z]  or  from X import *
_RE_IMPORT_FROM = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+", re.MULTILINE)

# class Foo(Base1, Base2):  — capture everything inside parens
_RE_CLASS_BASES = re.compile(r"^class\s+\w+\s*\(([^)]*)\)\s*:", re.MULTILINE)

# Bare call:  name(  or  obj.method(  at start-of-line or as statement
_RE_CALL = re.compile(r"(?:^|\s)(\w[\w.]*)\s*\(", re.MULTILINE)

# Keywords / builtins that must not generate "calls" edges
_DEFINITION_KEYWORDS: frozenset[str] = frozenset(
    {"def", "class", "return", "if", "elif", "while", "for", "with"}
)

_BUILTIN_NAMES: frozenset[str] = frozenset(
    {
        "print", "len", "range", "int", "str", "float", "bool", "list",
        "dict", "set", "tuple", "type", "super", "isinstance", "issubclass",
        "hasattr", "getattr", "setattr", "delattr", "repr", "id", "hash",
        "abs", "min", "max", "sum", "zip", "map", "filter", "sorted",
        "enumerate", "iter", "next", "open", "input", "format", "round",
        "hex", "oct", "bin", "chr", "ord", "any", "all", "vars", "dir",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entity_type_from_symbol(symbol: str, section: Section) -> str:
    """Infer entity type from the symbol FQN and section text heuristics."""
    text = section.text.lstrip()
    if text.startswith("class ") or (
        text.startswith("@") and "\nclass " in text
    ):
        return "class"
    if text.startswith("def ") or text.startswith("async def ") or (
        text.startswith("@") and ("\ndef " in text or "\nasync def " in text)
    ):
        return "function"
    return "function"


def _display_name(fqn: str) -> str:
    """Return the last segment of a dotted FQN."""
    return fqn.split(".")[-1]


def _extract_python_imports(source_text: str, module_entity_id: uuid.UUID) -> list[ExtractedEdge]:
    """Parse import statements and return edges from the module entity."""
    edges: list[ExtractedEdge] = []

    for m in _RE_IMPORT_SIMPLE.finditer(source_text):
        target = m.group(1).strip()
        if target:
            edges.append(
                ExtractedEdge(
                    source_entity_id=module_entity_id,
                    target_name=target,
                    edge_type="imports",
                )
            )

    for m in _RE_IMPORT_FROM.finditer(source_text):
        target = m.group(1).strip()
        if target:
            edges.append(
                ExtractedEdge(
                    source_entity_id=module_entity_id,
                    target_name=target,
                    edge_type="imports",
                )
            )

    return edges


def _extract_class_inheritance(
    section: Section,
    entity_id: uuid.UUID,
) -> list[ExtractedEdge]:
    """Parse class bases from a class section and emit ``inherits`` edges."""
    edges: list[ExtractedEdge] = []
    m = _RE_CLASS_BASES.search(section.text)
    if not m:
        return edges
    bases_raw = m.group(1)
    for base in bases_raw.split(","):
        base = base.strip().split("[")[0].strip()  # strip generics like Base[T]
        if base and base not in ("object", ""):
            edges.append(
                ExtractedEdge(
                    source_entity_id=entity_id,
                    target_name=base,
                    edge_type="inherits",
                )
            )
    return edges


def _extract_calls(
    section: Section,
    entity_id: uuid.UUID,
    own_name: str,
) -> list[ExtractedEdge]:
    """Extract best-effort call edges from a function/method section body.

    Strategy:
    - Find all ``name(`` patterns in the section text.
    - Skip built-ins, the function's own name, and definition keywords.
    - Deduplicate: emit one edge per unique callee per caller.
    """
    seen: set[str] = set()
    edges: list[ExtractedEdge] = []

    # Skip the first line of the section (the def/async def line itself)
    body_lines = section.text.split("\n", 1)
    body = body_lines[1] if len(body_lines) > 1 else ""

    for m in _RE_CALL.finditer(body):
        raw = m.group(1)
        # Take the last component of a dotted chain
        callee = raw.split(".")[-1]
        if (
            callee in _BUILTIN_NAMES
            or callee in _DEFINITION_KEYWORDS
            or callee == own_name
            or callee in seen
            or not callee
        ):
            continue
        seen.add(callee)
        edges.append(
            ExtractedEdge(
                source_entity_id=entity_id,
                target_name=callee,
                edge_type="calls",
            )
        )

    return edges


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_symbol_graph(
    parsed: ParsedDocument,
    source_text: bytes = b"",
) -> tuple[list[ExtractedEntity], list[ExtractedEdge]]:
    """Extract entities and edges from a parsed code document.

    Currently supports Python only.  For other languages (or when
    ``parsed.language`` is ``None``) the function returns two empty lists
    so callers can always call this unconditionally.

    Args:
        parsed:      A :class:`~omniscience_parsers.base.ParsedDocument`
                     produced by the tree-sitter parser.
        source_text: Original source bytes (used for import parsing).
                     If empty the raw section text is used as a fallback.

    Returns:
        A ``(entities, edges)`` pair.  Entities have stable UUIDs assigned
        at extraction time.  Edges reference entity UUIDs for intra-file
        relationships; cross-file edges carry a ``target_name`` string that
        the ingestion layer resolves after bulk insert.
    """
    if parsed.language != "python":
        return [], []

    entities: list[ExtractedEntity] = []
    edges: list[ExtractedEdge] = []

    # ------------------------------------------------------------------
    # 1. Derive the module name from the sections' heading paths.
    #    The first segment of any heading_path is the module name.
    # ------------------------------------------------------------------
    module_name: str = "unknown"
    if parsed.sections:
        hp = parsed.sections[0].heading_path
        if hp:
            module_name = hp[0]

    # ------------------------------------------------------------------
    # 2. Create a synthetic "module" entity as the anchor for imports.
    # ------------------------------------------------------------------
    module_entity_id = uuid.uuid4()
    module_entity = ExtractedEntity(
        id=module_entity_id,
        entity_type="module",
        name=module_name,
        display_name=module_name,
        symbol=module_name,
    )
    entities.append(module_entity)

    # ------------------------------------------------------------------
    # 3. One entity per section that has a symbol.
    # ------------------------------------------------------------------
    symbol_to_entity: dict[str, ExtractedEntity] = {}
    for sec in parsed.sections:
        if not sec.symbol:
            continue
        fqn = sec.symbol
        etype = _entity_type_from_symbol(fqn, sec)
        new_ent = ExtractedEntity(
            id=uuid.uuid4(),
            entity_type=etype,
            name=fqn,
            display_name=_display_name(fqn),
            symbol=fqn,
            metadata={
                "line_start": sec.line_start,
                "line_end": sec.line_end,
                "language": parsed.language,
            },
        )
        entities.append(new_ent)
        symbol_to_entity[fqn] = new_ent

        # module "defines" every top-level symbol
        edges.append(
            ExtractedEdge(
                source_entity_id=module_entity_id,
                target_name=fqn,
                edge_type="defines",
            )
        )

    # ------------------------------------------------------------------
    # 4. Extract imports from the full source text (or joined section text).
    # ------------------------------------------------------------------
    full_text = source_text.decode(errors="replace") if source_text else "\n".join(
        s.text for s in parsed.sections
    )
    import_edges = _extract_python_imports(full_text, module_entity_id)
    edges.extend(import_edges)

    # ------------------------------------------------------------------
    # 5. For each class section, extract inheritance edges.
    # ------------------------------------------------------------------
    for sec in parsed.sections:
        if not sec.symbol:
            continue
        cls_ent: ExtractedEntity | None = symbol_to_entity.get(sec.symbol)
        if cls_ent is None or cls_ent.entity_type != "class":
            continue
        inh_edges = _extract_class_inheritance(sec, cls_ent.id)
        edges.extend(inh_edges)

    # ------------------------------------------------------------------
    # 6. For each function/method section, extract call edges (best-effort).
    # ------------------------------------------------------------------
    for sec in parsed.sections:
        if not sec.symbol:
            continue
        fn_ent: ExtractedEntity | None = symbol_to_entity.get(sec.symbol)
        if fn_ent is None or fn_ent.entity_type != "function":
            continue
        call_edges = _extract_calls(sec, fn_ent.id, fn_ent.display_name)
        edges.extend(call_edges)

    return entities, edges


__all__ = ["ExtractedEdge", "ExtractedEntity", "extract_symbol_graph"]
