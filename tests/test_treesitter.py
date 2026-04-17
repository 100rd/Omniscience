"""Tests for TreeSitterParser — code symbol extraction."""

from __future__ import annotations

import pytest
from omniscience_parsers import TreeSitterParser


@pytest.fixture()
def parser() -> TreeSitterParser:
    return TreeSitterParser()


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


class TestPythonExtraction:
    def test_function_extracted(self, parser: TreeSitterParser) -> None:
        src = b"def my_func(x):\n    return x + 1\n"
        doc = parser.parse(src, "module.py")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert any("my_func" in sym for sym in symbols)

    def test_function_fqn(self, parser: TreeSitterParser) -> None:
        src = b"def greet(name):\n    return name\n"
        doc = parser.parse(src, "utils.py")
        section = next(s for s in doc.sections if s.symbol and "greet" in s.symbol)
        assert section.symbol == "utils.greet"

    def test_class_extracted(self, parser: TreeSitterParser) -> None:
        src = b"class Foo:\n    pass\n"
        doc = parser.parse(src, "models.py")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert any("Foo" in sym for sym in symbols)

    def test_method_fqn(self, parser: TreeSitterParser) -> None:
        src = b"class MyClass:\n    def my_method(self):\n        pass\n"
        doc = parser.parse(src, "mymod.py")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert any("MyClass" in sym and "my_method" in sym for sym in symbols)

    def test_decorated_function(self, parser: TreeSitterParser) -> None:
        src = b"@property\ndef value(self):\n    return self._v\n"
        doc = parser.parse(src, "mod.py")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert any("value" in sym for sym in symbols)

    def test_line_numbers_correct(self, parser: TreeSitterParser) -> None:
        src = b"\ndef foo():\n    pass\n"
        doc = parser.parse(src, "x.py")
        section = next(s for s in doc.sections if s.symbol and "foo" in s.symbol)
        assert section.line_start >= 1
        assert section.line_end >= section.line_start

    def test_section_text_contains_source(self, parser: TreeSitterParser) -> None:
        src = b"def add(a, b):\n    return a + b\n"
        doc = parser.parse(src, "math_utils.py")
        section = next(s for s in doc.sections if s.symbol and "add" in s.symbol)
        assert "return a + b" in section.text

    def test_heading_path_includes_module(self, parser: TreeSitterParser) -> None:
        src = b"def hello():\n    pass\n"
        doc = parser.parse(src, "greetings.py")
        section = next(s for s in doc.sections if s.symbol and "hello" in s.symbol)
        assert "greetings" in section.heading_path

    def test_language_set(self, parser: TreeSitterParser) -> None:
        src = b"def f(): pass\n"
        doc = parser.parse(src, "f.py")
        assert doc.language == "python"

    def test_syntax_error_partial_result(self, parser: TreeSitterParser) -> None:
        # Valid prefix + syntax error suffix
        src = b"def valid():\n    pass\n\ndef (:\n    pass\n"
        doc = parser.parse(src, "broken.py")
        # Should not raise and should return something
        assert doc is not None
        # parse_warning may be set
        # We either get sections or a warning in metadata — no crash is sufficient
        assert len(doc.sections) >= 0  # at minimum doesn't crash


# ---------------------------------------------------------------------------
# TypeScript
# ---------------------------------------------------------------------------


class TestTypeScriptExtraction:
    def test_function_extracted(self, parser: TreeSitterParser) -> None:
        src = b"function greet(name: string): string { return name; }\n"
        doc = parser.parse(src, "utils.ts")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert any("greet" in sym for sym in symbols)

    def test_class_extracted(self, parser: TreeSitterParser) -> None:
        src = b"class Greeter { greet(): void {} }\n"
        doc = parser.parse(src, "greeter.ts")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert any("Greeter" in sym for sym in symbols)

    def test_exported_function(self, parser: TreeSitterParser) -> None:
        src = b"export function hello(): string { return 'hello'; }\n"
        doc = parser.parse(src, "api.ts")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert any("hello" in sym for sym in symbols)

    def test_arrow_function_named(self, parser: TreeSitterParser) -> None:
        src = b"const double = (x: number) => x * 2;\n"
        doc = parser.parse(src, "fns.ts")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert any("double" in sym for sym in symbols)

    def test_language_set(self, parser: TreeSitterParser) -> None:
        src = b"const x = 1;\n"
        doc = parser.parse(src, "x.ts")
        assert doc.language == "typescript"

    def test_tsx_extension(self, parser: TreeSitterParser) -> None:
        src = b"export function Btn(): void {}\n"
        doc = parser.parse(src, "Btn.tsx")
        assert doc.language == "typescript"


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------


class TestGoExtraction:
    def test_function_extracted(self, parser: TreeSitterParser) -> None:
        src = b"package main\nfunc Greet(name string) string { return name }\n"
        doc = parser.parse(src, "main.go")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert any("Greet" in sym for sym in symbols)

    def test_method_extracted(self, parser: TreeSitterParser) -> None:
        src = b'package main\ntype Foo struct{}\nfunc (f Foo) Bar() string { return "" }\n'
        doc = parser.parse(src, "foo.go")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert any("Bar" in sym for sym in symbols)

    def test_type_declaration_extracted(self, parser: TreeSitterParser) -> None:
        src = b"package main\ntype MyStruct struct { Field string }\n"
        doc = parser.parse(src, "types.go")
        symbols = [s.symbol for s in doc.sections if s.symbol]
        assert any("MyStruct" in sym for sym in symbols)

    def test_language_set(self, parser: TreeSitterParser) -> None:
        src = b"package main\n"
        doc = parser.parse(src, "main.go")
        assert doc.language == "go"


# ---------------------------------------------------------------------------
# Language detection by extension
# ---------------------------------------------------------------------------


class TestLanguageDetection:
    def test_py_extension(self, parser: TreeSitterParser) -> None:
        doc = parser.parse(b"def f(): pass\n", "x.py")
        assert doc.language == "python"

    def test_ts_extension(self, parser: TreeSitterParser) -> None:
        doc = parser.parse(b"const x = 1;\n", "x.ts")
        assert doc.language == "typescript"

    def test_js_extension(self, parser: TreeSitterParser) -> None:
        doc = parser.parse(b"function f() {}\n", "x.js")
        assert doc.language == "javascript"

    def test_go_extension(self, parser: TreeSitterParser) -> None:
        doc = parser.parse(b"package main\n", "x.go")
        assert doc.language == "go"

    def test_rs_extension(self, parser: TreeSitterParser) -> None:
        doc = parser.parse(b"fn main() {}\n", "x.rs")
        assert doc.language == "rust"

    def test_java_extension(self, parser: TreeSitterParser) -> None:
        doc = parser.parse(b"class Foo {}\n", "Foo.java")
        assert doc.language == "java"

    def test_unsupported_extension_falls_back(self, parser: TreeSitterParser) -> None:
        src = b"some random content"
        doc = parser.parse(src, "file.xyz")
        # Should fall back to plain text (one section, no language)
        assert len(doc.sections) == 1
        assert doc.language is None

    def test_no_extension_falls_back(self, parser: TreeSitterParser) -> None:
        src = b"just text"
        doc = parser.parse(src, "README")
        assert len(doc.sections) == 1


# ---------------------------------------------------------------------------
# Graceful error handling
# ---------------------------------------------------------------------------


class TestGracefulErrors:
    def test_completely_invalid_syntax_no_crash(self, parser: TreeSitterParser) -> None:
        src = b"@@@@@@@@@@@ not valid python @@@@@@@@@"
        doc = parser.parse(src, "bad.py")
        assert doc is not None  # must not raise

    def test_empty_file(self, parser: TreeSitterParser) -> None:
        doc = parser.parse(b"", "empty.py")
        assert doc is not None
        assert len(doc.sections) >= 0

    def test_binary_content_no_crash(self, parser: TreeSitterParser) -> None:
        content = bytes(range(256))
        doc = parser.parse(content, "data.py")
        assert doc is not None

    def test_can_handle_returns_true_for_known_extensions(self, parser: TreeSitterParser) -> None:
        assert parser.can_handle("", ".py") is True
        assert parser.can_handle("", ".ts") is True
        assert parser.can_handle("", ".go") is True
        assert parser.can_handle("", ".rs") is True
        assert parser.can_handle("", ".java") is True

    def test_can_handle_returns_false_for_unknown(self, parser: TreeSitterParser) -> None:
        assert parser.can_handle("text/plain", ".txt") is False
        assert parser.can_handle("application/json", ".json") is False
