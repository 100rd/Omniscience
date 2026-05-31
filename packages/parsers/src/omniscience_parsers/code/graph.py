"""Symbol graph extractor (v0.3 Cross-File Resolution).

Transforms a ParsedDocument into entities and edges.
Now supports basic import-tracking to resolve cross-file calls.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from omniscience_parsers.base import ParsedDocument

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ExtractedEntity:
    id: uuid.UUID
    entity_type: str
    name: str
    display_name: str
    symbol: str
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class ExtractedEdge:
    source_entity_id: uuid.UUID
    target_name: str  # FQN or alias
    edge_type: str
    metadata: dict[str, Any] = field(default_factory=dict)

# ---------------------------------------------------------------------------
# Python Logic (AST-aware heuristics)
# ---------------------------------------------------------------------------

# Compiled patterns for Python import extraction
_PY_IMPORT_RE = re.compile(r"^\s*import\s+([\w.]+)(?:\s+as\s+(\w+))?", re.MULTILINE)
_PY_FROM_RE = re.compile(
    r"^\s*from\s+([\w.]+)\s+import\s+([\w*]+)(?:\s+as\s+(\w+))?",
    re.MULTILINE,
)


def _extract_python(
    parsed: ParsedDocument,
    source_text: bytes,
) -> tuple[list[ExtractedEntity], list[ExtractedEdge]]:
    entities: list[ExtractedEntity] = []
    edges: list[ExtractedEdge] = []

    full_text = source_text.decode(errors="replace") if source_text else ""
    module_name = parsed.sections[0].heading_path[0] if parsed.sections else "unknown"
    module_id = uuid.uuid4()

    entities.append(ExtractedEntity(module_id, "module", module_name, module_name, module_name))

    # 1. Build Import Map (alias -> FQN)
    import_map: dict[str, str] = {}
    # import X as Y
    for m in _PY_IMPORT_RE.finditer(full_text):
        fqn = m.group(1)
        alias = m.group(2) or fqn.split(".")[-1]
        import_map[alias] = fqn
        edges.append(ExtractedEdge(module_id, fqn, "imports"))

    # from X import Y as Z
    for m in _PY_FROM_RE.finditer(full_text):
        base_fqn = m.group(1)
        symbol = m.group(2)
        alias = m.group(3) or symbol
        if symbol != "*":
            fqn = f"{base_fqn}.{symbol}"
            import_map[alias] = fqn
            edges.append(ExtractedEdge(module_id, fqn, "imports"))
        else:
            edges.append(ExtractedEdge(module_id, base_fqn, "imports"))

    # 2. Extract Definitions
    symbol_to_id: dict[str, uuid.UUID] = {}
    for sec in parsed.sections:
        if not sec.symbol:
            continue
        ent_id = uuid.uuid4()
        etype = "class" if "class " in sec.text else "function"
        entities.append(ExtractedEntity(
            id=ent_id,
            entity_type=etype,
            name=sec.symbol,
            display_name=sec.symbol.split(".")[-1],
            symbol=sec.symbol,
            metadata={"line_start": sec.line_start}
        ))
        symbol_to_id[sec.symbol] = ent_id
        edges.append(ExtractedEdge(module_id, sec.symbol, "defines"))

    # 3. Extract Calls with Resolution
    for sec in parsed.sections:
        caller_id = symbol_to_id.get(sec.symbol or "")
        if not caller_id:
            continue

        # Heuristic for calls: name(
        for m in re.finditer(r"(?:^|[^\w.])([\w.]+)\s*\(", sec.text):
            raw_call = m.group(1)
            parts = raw_call.split(".")
            prefix = parts[0]

            resolved_target = raw_call
            if prefix in import_map:
                # It's an external call through an import
                resolved_target = import_map[prefix]
                if len(parts) > 1:
                    resolved_target = f"{resolved_target}.{'.'.join(parts[1:])}"
            elif len(parts) == 1:
                # It might be a local call
                local_fqn = f"{module_name}.{raw_call}"
                if local_fqn in symbol_to_id:
                    resolved_target = local_fqn

            if resolved_target != sec.symbol:
                edges.append(ExtractedEdge(caller_id, resolved_target, "calls"))

    return entities, edges

# ---------------------------------------------------------------------------
# TypeScript Logic (ESM-aware)
# ---------------------------------------------------------------------------

# Module-level compiled pattern — the character class [\'"] contains both quote
# chars, so a raw string with single-quote delimiters avoids a SyntaxError
# under Python 3.12 when the outer string is double-quoted.
_JS_IMPORT_RE = re.compile(r'import\s+.*\{?([\w\s,]+)\}?\s+from\s+[\'"](.*)[\'"]')


def _extract_typescript(
    parsed: ParsedDocument,
    source_text: bytes,
) -> tuple[list[ExtractedEntity], list[ExtractedEdge]]:
    entities: list[ExtractedEntity] = []
    edges: list[ExtractedEdge] = []

    full_text = source_text.decode(errors="replace") if source_text else ""
    module_name = parsed.sections[0].heading_path[0] if parsed.sections else "unknown"
    module_id = uuid.uuid4()
    entities.append(ExtractedEntity(module_id, "module", module_name, module_name, module_name))

    # 1. Import Map (simplified for ESM)
    # import { X as Y } from './path'
    for m in _JS_IMPORT_RE.finditer(full_text):
        path = m.group(2)
        # For now, we use path as the module FQN
        edges.append(ExtractedEdge(module_id, path, "imports"))

    # 2. Definitions & Calls (similar to Python logic)
    for sec in parsed.sections:
        if not sec.symbol:
            continue
        ent_id = uuid.uuid4()
        entities.append(ExtractedEntity(
            id=ent_id,
            entity_type="function" if "(" in sec.text else "class",
            name=sec.symbol,
            display_name=sec.symbol.split(".")[-1],
            symbol=sec.symbol
        ))
        edges.append(ExtractedEdge(module_id, sec.symbol, "defines"))
        # Simplified calls for TS v0.3
        for cm in re.finditer(r"([\w.]+)\s*\(", sec.text):
            target = cm.group(1)
            if target != sec.symbol:
                edges.append(ExtractedEdge(ent_id, target, "calls"))

    return entities, edges

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_symbol_graph(
    parsed: ParsedDocument,
    source_text: bytes = b"",
) -> tuple[list[ExtractedEntity], list[ExtractedEdge]]:
    if parsed.language == "python":
        return _extract_python(parsed, source_text)
    elif parsed.language in ("typescript", "javascript"):
        return _extract_typescript(parsed, source_text)
    return [], []

__all__ = ["ExtractedEdge", "ExtractedEntity", "extract_symbol_graph"]
