"""Tests for parser framework: MarkdownParser, PlainTextParser, ParserDispatch."""

from __future__ import annotations

from omniscience_parsers import (
    MarkdownParser,
    ParserDispatch,
    PlainTextParser,
    TreeSitterParser,
)

# ---------------------------------------------------------------------------
# MarkdownParser
# ---------------------------------------------------------------------------


class TestMarkdownParser:
    def setup_method(self) -> None:
        self.parser = MarkdownParser()

    def test_can_handle_by_content_type(self) -> None:
        assert self.parser.can_handle("text/markdown", "") is True
        assert self.parser.can_handle("text/x-markdown", "") is True
        assert self.parser.can_handle("application/json", "") is False

    def test_can_handle_by_extension(self) -> None:
        assert self.parser.can_handle("", ".md") is True
        assert self.parser.can_handle("", ".mdx") is True
        assert self.parser.can_handle("", ".markdown") is True
        assert self.parser.can_handle("", ".txt") is False

    def test_heading_hierarchy(self) -> None:
        content = (
            b"# Root\n\nRoot text.\n\n## Child\n\nChild text.\n\n### Grandchild\n\nDeep text.\n"
        )
        doc = self.parser.parse(content)
        headings = [s.heading_path for s in doc.sections]
        # Root section
        assert ["Root"] in headings
        # Child section must include parent
        assert any("Child" in path for path in headings)
        # Grandchild section must include ancestors
        assert any("Grandchild" in path for path in headings)

    def test_heading_path_nesting(self) -> None:
        content = b"# A\n\nText A.\n\n## B\n\nText B.\n"
        doc = self.parser.parse(content)
        b_section = next(s for s in doc.sections if "B" in s.heading_path)
        # Path should include A and B
        assert "A" in b_section.heading_path
        assert "B" in b_section.heading_path

    def test_frontmatter_metadata(self) -> None:
        content = b"---\ntitle: My Doc\nauthor: Alice\n---\n\n# Hello\n\nBody text.\n"
        doc = self.parser.parse(content)
        assert doc.metadata.get("author") == "Alice"

    def test_frontmatter_title(self) -> None:
        content = b"---\ntitle: Frontmatter Title\n---\n\n# H1 Title\n\nBody.\n"
        doc = self.parser.parse(content)
        # frontmatter title wins
        assert doc.title == "Frontmatter Title"

    def test_first_h1_becomes_title_without_frontmatter(self) -> None:
        content = b"# My Title\n\nSome text.\n"
        doc = self.parser.parse(content)
        assert doc.title == "My Title"

    def test_code_fence_preserved(self) -> None:
        content = b"# Section\n\n```python\nprint('hello')\n```\n\nAfter fence.\n"
        doc = self.parser.parse(content)
        # The section text should contain the fence content
        section_texts = " ".join(s.text for s in doc.sections)
        assert "print" in section_texts or "python" in section_texts

    def test_code_fence_language_in_metadata(self) -> None:
        content = b"# Code\n\n```typescript\nconst x = 1;\n```\n"
        doc = self.parser.parse(content)
        metas = [s.metadata for s in doc.sections]
        langs = [m.get("fence_languages", []) for m in metas]
        flat = [lang for lst in langs for lang in lst]
        assert "typescript" in flat

    def test_no_heading_single_section(self) -> None:
        content = b"Just some plain markdown without headings.\n"
        doc = self.parser.parse(content)
        assert len(doc.sections) == 1
        assert doc.sections[0].heading_path == []

    def test_content_type(self) -> None:
        doc = self.parser.parse(b"# Hello\n")
        assert doc.content_type == "text/markdown"

    def test_empty_content(self) -> None:
        doc = self.parser.parse(b"")
        assert doc.sections == [] or all(s.text.strip() == "" for s in doc.sections)

    def test_sibling_sections_separate(self) -> None:
        content = b"# A\n\nText A.\n\n# B\n\nText B.\n"
        doc = self.parser.parse(content)
        texts = [s.text for s in doc.sections if s.text.strip()]
        assert any("Text A" in t for t in texts)
        assert any("Text B" in t for t in texts)


# ---------------------------------------------------------------------------
# PlainTextParser
# ---------------------------------------------------------------------------


class TestPlainTextParser:
    def setup_method(self) -> None:
        self.parser = PlainTextParser()

    def test_can_handle_anything(self) -> None:
        assert self.parser.can_handle("application/octet-stream", ".bin") is True
        assert self.parser.can_handle("", "") is True

    def test_single_section(self) -> None:
        content = b"Line one.\nLine two.\nLine three.\n"
        doc = self.parser.parse(content)
        assert len(doc.sections) == 1
        assert "Line one" in doc.sections[0].text
        assert "Line three" in doc.sections[0].text

    def test_heading_path_is_empty(self) -> None:
        doc = self.parser.parse(b"Hello world\n")
        assert doc.sections[0].heading_path == []

    def test_line_range(self) -> None:
        content = b"A\nB\nC\n"
        doc = self.parser.parse(content)
        assert doc.sections[0].line_start == 1
        assert doc.sections[0].line_end == 3

    def test_content_type(self) -> None:
        doc = self.parser.parse(b"text")
        assert doc.content_type == "text/plain"

    def test_empty_content(self) -> None:
        doc = self.parser.parse(b"")
        assert len(doc.sections) == 1

    def test_binary_like_content_decodes(self) -> None:
        # Should not raise on invalid UTF-8
        content = b"\xff\xfe hello"
        doc = self.parser.parse(content)
        assert len(doc.sections) == 1


# ---------------------------------------------------------------------------
# ParserDispatch
# ---------------------------------------------------------------------------


class TestParserDispatch:
    def setup_method(self) -> None:
        self.dispatch = ParserDispatch([MarkdownParser(), TreeSitterParser(), PlainTextParser()])

    def test_routes_markdown_by_content_type(self) -> None:
        parser = self.dispatch.get_parser("text/markdown", "")
        assert isinstance(parser, MarkdownParser)

    def test_routes_markdown_by_extension(self) -> None:
        parser = self.dispatch.get_parser("", ".md")
        assert isinstance(parser, MarkdownParser)

    def test_routes_python_to_treesitter(self) -> None:
        parser = self.dispatch.get_parser("", ".py")
        assert isinstance(parser, TreeSitterParser)

    def test_routes_typescript_to_treesitter(self) -> None:
        parser = self.dispatch.get_parser("", ".ts")
        assert isinstance(parser, TreeSitterParser)

    def test_fallback_to_plaintext_for_unknown(self) -> None:
        parser = self.dispatch.get_parser("application/octet-stream", ".xyz")
        assert isinstance(parser, PlainTextParser)

    def test_fallback_to_plaintext_empty_inputs(self) -> None:
        parser = self.dispatch.get_parser("", "")
        assert isinstance(parser, PlainTextParser)

    def test_plaintext_not_duplicated_as_fallback(self) -> None:
        # When PlainTextParser is already in the list, should not double up
        dispatch = ParserDispatch([PlainTextParser()])
        plain_count = sum(1 for p in dispatch._parsers if isinstance(p, PlainTextParser))
        assert plain_count == 1

    def test_parse_convenience_method_markdown(self) -> None:
        content = b"# Hello\n\nWorld.\n"
        doc = self.dispatch.parse(content, "text/markdown", ".md")
        assert doc.content_type == "text/markdown"
        assert any(s.text for s in doc.sections)

    def test_parse_convenience_method_unknown(self) -> None:
        content = b"just text"
        doc = self.dispatch.parse(content, "unknown/type", ".zzz")
        assert doc.content_type == "text/plain"
        assert len(doc.sections) == 1

    def test_no_explicit_plaintext_gets_fallback_added(self) -> None:
        dispatch = ParserDispatch([MarkdownParser()])
        # Should auto-append PlainTextParser
        assert any(isinstance(p, PlainTextParser) for p in dispatch._parsers)
