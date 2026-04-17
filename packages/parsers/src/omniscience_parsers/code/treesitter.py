"""Tree-sitter backed code parser.

Extracts top-level symbols (functions, classes, methods) from source code
and maps each to a :class:`~omniscience_parsers.base.Section` with an FQN
``symbol`` and ``line_start``/``line_end`` from the AST node positions.

Supported languages (detected by file extension):
  Python, TypeScript, JavaScript, Go, Rust, Java

Unsupported extensions fall back to a single plain-text section.
Syntax errors produce a partial result with a ``parse_error`` warning in
``ParsedDocument.metadata`` rather than raising.
"""

from __future__ import annotations

import importlib
import os
from typing import Any

import structlog

from omniscience_parsers.base import ParsedDocument, Section

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Extension → language name
# ---------------------------------------------------------------------------

_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
}

_CODE_CONTENT_TYPES = frozenset(
    {
        "text/x-python",
        "application/typescript",
        "text/typescript",
        "application/javascript",
        "text/javascript",
        "text/x-go",
        "text/x-rustsrc",
        "text/x-java-source",
    }
)

# ---------------------------------------------------------------------------
# Per-language symbol node types
# ---------------------------------------------------------------------------

_SYMBOL_NODE_TYPES: dict[str, list[str]] = {
    "python": [
        "function_definition",
        "class_definition",
        "decorated_definition",
    ],
    "typescript": [
        "function_declaration",
        "class_declaration",
        "export_statement",
        "lexical_declaration",  # const arrowFn = (x) => ... at top level
        "method_definition",
    ],
    "javascript": [
        "function_declaration",
        "class_declaration",
        "export_statement",
        "lexical_declaration",  # const arrowFn = (x) => ... at top level
        "method_definition",
    ],
    "go": [
        "function_declaration",
        "method_declaration",
        "type_declaration",
    ],
    "rust": [
        "function_item",
        "impl_item",
        "struct_item",
        "enum_item",
    ],
    "java": [
        "method_declaration",
        "class_declaration",
        "interface_declaration",
    ],
}

# ---------------------------------------------------------------------------
# Language loader — uses importlib to avoid repeated `import X as mod` aliases
# that trigger mypy's no-redef check.
# ---------------------------------------------------------------------------

_LANG_MODULE_MAP: dict[str, tuple[str, str]] = {
    # language → (module_name, attr_to_call)
    "python": ("tree_sitter_python", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "go": ("tree_sitter_go", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "java": ("tree_sitter_java", "language"),
    # typescript uses a different attr name
    "typescript": ("tree_sitter_typescript", "language_typescript"),
}


def _load_language(language: str) -> Any:
    """Import and return the tree-sitter ``Language`` object for *language*.

    Uses :func:`importlib.import_module` to avoid repeated ``import X as mod``
    aliases that trigger mypy ``no-redef``.  All packages are untyped so this
    function returns ``Any``.
    """
    entry = _LANG_MODULE_MAP.get(language)
    if entry is None:
        raise ValueError(f"Unsupported language: {language}")

    module_name, attr = entry
    mod = importlib.import_module(module_name)
    ts_lang_fn = getattr(mod, attr)

    from tree_sitter import Language

    return Language(ts_lang_fn())


# ---------------------------------------------------------------------------
# Name extraction helpers
# ---------------------------------------------------------------------------


def _first_named_child(node: Any, *types: str) -> Any | None:
    """Return the first named child whose type is in *types*, or None."""
    for child in node.named_children:
        if child.type in types:
            return child
    return None


def _node_text(node: Any, src_bytes: bytes) -> str:
    return src_bytes[node.start_byte : node.end_byte].decode(errors="replace")


def _symbol_name_python(node: Any, src_bytes: bytes) -> str | None:
    """Extract name from a Python function/class/decorated node."""
    if node.type == "decorated_definition":
        inner = _first_named_child(node, "function_definition", "class_definition")
        if inner is None:
            return None
        node = inner
    name_node = _first_named_child(node, "identifier")
    return _node_text(name_node, src_bytes) if name_node else None


def _symbol_name_ts_js(node: Any, src_bytes: bytes) -> str | None:
    """Extract name from TypeScript/JavaScript function, class, or export."""
    if node.type == "export_statement":
        inner = _first_named_child(
            node,
            "function_declaration",
            "class_declaration",
            "lexical_declaration",
        )
        if inner is None:
            return None
        node = inner
    if node.type == "lexical_declaration":
        # const arrowFn = (x) => ...  — only named arrow functions
        declarator = _first_named_child(node, "variable_declarator")
        if declarator is None:
            return None
        arrow = _first_named_child(declarator, "arrow_function")
        if arrow is None:
            return None  # not a named arrow function — skip
        name_node = _first_named_child(declarator, "identifier")
        return _node_text(name_node, src_bytes) if name_node else None
    if node.type == "method_definition":
        name_node = _first_named_child(node, "property_identifier")
        return _node_text(name_node, src_bytes) if name_node else None
    # function_declaration, class_declaration
    name_node = _first_named_child(node, "identifier", "type_identifier")
    return _node_text(name_node, src_bytes) if name_node else None


def _symbol_name_go(node: Any, src_bytes: bytes) -> str | None:
    """Extract name from a Go function, method, or type declaration."""
    if node.type == "type_declaration":
        spec = _first_named_child(node, "type_spec")
        if spec is None:
            return None
        name_node = _first_named_child(spec, "type_identifier")
        return _node_text(name_node, src_bytes) if name_node else None
    if node.type == "method_declaration":
        recv = _first_named_child(node, "parameter_list")
        method_name_node = _first_named_child(node, "field_identifier")
        method_name = _node_text(method_name_node, src_bytes) if method_name_node else None
        if recv and method_name:
            recv_type = _first_named_child(recv, "parameter_declaration")
            if recv_type:
                type_id = _first_named_child(recv_type, "type_identifier")
                if type_id:
                    return f"{_node_text(type_id, src_bytes)}.{method_name}"
        return method_name
    # function_declaration
    name_node = _first_named_child(node, "identifier")
    return _node_text(name_node, src_bytes) if name_node else None


def _symbol_name_rust(node: Any, src_bytes: bytes) -> str | None:
    """Extract name from a Rust fn, impl, struct, or enum."""
    if node.type == "impl_item":
        type_id = _first_named_child(node, "type_identifier")
        return _node_text(type_id, src_bytes) if type_id else None
    name_node = _first_named_child(node, "identifier", "type_identifier")
    return _node_text(name_node, src_bytes) if name_node else None


def _symbol_name_java(node: Any, src_bytes: bytes) -> str | None:
    """Extract name from a Java method, class, or interface."""
    name_node = _first_named_child(node, "identifier")
    return _node_text(name_node, src_bytes) if name_node else None


_NAME_EXTRACTORS = {
    "python": _symbol_name_python,
    "typescript": _symbol_name_ts_js,
    "javascript": _symbol_name_ts_js,
    "go": _symbol_name_go,
    "rust": _symbol_name_rust,
    "java": _symbol_name_java,
}

# ---------------------------------------------------------------------------
# Recursive symbol walker
# ---------------------------------------------------------------------------


def _collect_symbols(
    node: Any,
    src_bytes: bytes,
    language: str,
    module_name: str,
    parent_name: str | None,
    target_types: list[str],
    sections: list[Section],
    depth: int = 0,
) -> None:
    """Recursively walk *node* and collect symbol sections."""
    if depth > 8:  # guard against pathological nesting
        return

    if node.type in target_types:
        extract = _NAME_EXTRACTORS[language]
        raw_name = extract(node, src_bytes)
        if raw_name:
            if parent_name:
                symbol_parts = [module_name, parent_name, raw_name]
                heading_path = [module_name, parent_name, raw_name]
            else:
                symbol_parts = [module_name, raw_name]
                heading_path = [module_name, raw_name]
            symbol = ".".join(symbol_parts)
            text = _node_text(node, src_bytes)
            line_start = node.start_point[0] + 1  # tree-sitter rows are 0-based
            line_end = node.end_point[0] + 1
            sections.append(
                Section(
                    heading_path=heading_path,
                    text=text,
                    line_start=line_start,
                    line_end=line_end,
                    symbol=symbol,
                )
            )
            # Descend into class/impl bodies to pick up methods
            is_container = "class" in node.type or "impl" in node.type
            next_parent = raw_name if is_container else parent_name
            for child in node.named_children:
                _collect_symbols(
                    child,
                    src_bytes,
                    language,
                    module_name,
                    next_parent,
                    target_types,
                    sections,
                    depth + 1,
                )
            return  # children handled above; avoid double-collecting

    for child in node.named_children:
        _collect_symbols(
            child,
            src_bytes,
            language,
            module_name,
            parent_name,
            target_types,
            sections,
            depth + 1,
        )


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class TreeSitterParser:
    """Parse source code into symbol-level sections using tree-sitter."""

    def can_handle(self, content_type: str, file_extension: str) -> bool:
        return content_type in _CODE_CONTENT_TYPES or file_extension.lower() in _EXT_TO_LANG

    def parse(self, content: bytes, file_path: str = "") -> ParsedDocument:
        ext = os.path.splitext(file_path)[1].lower() if file_path else ""
        language = _EXT_TO_LANG.get(ext)
        if language is None:
            return self._fallback(content, file_path)

        module_name = os.path.splitext(os.path.basename(file_path))[0] or "module"
        return self._parse_language(content, language, module_name, file_path)

    def _parse_language(
        self, content: bytes, language: str, module_name: str, file_path: str
    ) -> ParsedDocument:
        meta: dict[str, Any] = {}
        sections: list[Section] = []
        try:
            from tree_sitter import Parser as TSParser

            ts_lang = _load_language(language)
            parser = TSParser(ts_lang)
            tree = parser.parse(content)
            if tree.root_node.has_error:
                meta["parse_warning"] = "Tree-sitter reported syntax errors; result may be partial"
                log.warning("treesitter_syntax_error", file_path=file_path, language=language)
            target_types = _SYMBOL_NODE_TYPES.get(language, [])
            _collect_symbols(
                tree.root_node,
                content,
                language,
                module_name,
                None,
                target_types,
                sections,
            )
        except Exception as exc:
            log.error(
                "treesitter_parse_failed",
                file_path=file_path,
                language=language,
                error=str(exc),
            )
            meta["parse_error"] = str(exc)
            return self._fallback(content, file_path, language=language, extra_meta=meta)

        # If nothing was extracted (empty file, only comments, etc.) fall back
        if not sections:
            return self._fallback(content, file_path, language=language, extra_meta=meta)

        return ParsedDocument(
            sections=sections,
            content_type="text/x-source",
            language=language,
            metadata=meta,
        )

    def _fallback(
        self,
        content: bytes,
        file_path: str,
        language: str | None = None,
        extra_meta: dict[str, Any] | None = None,
    ) -> ParsedDocument:
        """Return a single-section document when extraction is not possible."""
        from omniscience_parsers.plaintext import PlainTextParser

        doc = PlainTextParser().parse(content, file_path=file_path)
        if language or extra_meta:
            return ParsedDocument(
                sections=doc.sections,
                content_type=doc.content_type,
                language=language,
                metadata={**(extra_meta or {})},
            )
        return doc


__all__ = ["TreeSitterParser"]
