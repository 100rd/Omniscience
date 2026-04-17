"""Markdown parser using markdown-it-py + python-frontmatter.

Heading hierarchy is tracked as a stack so that every section carries the
full path of its ancestor headings.  Code fences are preserved inside the
section text and their ``info`` string is stored in ``section.metadata``.
"""

from __future__ import annotations

from typing import Any

import frontmatter  # type: ignore[import-untyped]
import markdown_it

from omniscience_parsers.base import ParsedDocument, Section

_CONTENT_TYPES = frozenset({"text/markdown", "text/x-markdown"})
_EXTENSIONS = frozenset({".md", ".mdx", ".markdown"})

# Heading tag → nesting depth (h1 = 1, h2 = 2, …)
_HEADING_DEPTH = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


class MarkdownParser:
    """Parse Markdown documents into hierarchical sections."""

    def __init__(self) -> None:
        self._md = markdown_it.MarkdownIt()

    def can_handle(self, content_type: str, file_extension: str) -> bool:
        return content_type in _CONTENT_TYPES or file_extension.lower() in _EXTENSIONS

    def parse(self, content: bytes, file_path: str = "") -> ParsedDocument:
        raw_text = content.decode(errors="replace")
        post = frontmatter.loads(raw_text)
        body: str = post.content
        fm_meta: dict[str, Any] = dict(post.metadata)

        tokens = self._md.parse(body)
        sections: list[Section] = []
        title: str | None = fm_meta.get("title")

        # heading_stack: list of (depth, label) representing the current path
        heading_stack: list[tuple[int, str]] = []
        current_lines: list[str] = []
        current_line_start: int = 1
        current_meta: dict[str, Any] = {}

        body_lines = body.splitlines(keepends=True)

        def _flush_section(end_line: int) -> None:
            """Emit a section if there is accumulated content."""
            text = "".join(current_lines).strip()
            if not text:
                return
            path = [lbl for _, lbl in heading_stack]
            sections.append(
                Section(
                    heading_path=path,
                    text=text,
                    line_start=current_line_start,
                    line_end=end_line,
                    metadata={**current_meta},
                )
            )

        def _body_lines_for(token: markdown_it.token.Token) -> list[str]:
            """Return body lines that correspond to this token's map."""
            if token.map is None:
                return []
            start, end = token.map
            return body_lines[start:end]

        i = 0
        while i < len(tokens):
            token = tokens[i]

            if token.type == "heading_open":
                depth = _HEADING_DEPTH.get(token.tag, 1)
                # Grab the inline content from the next token
                if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
                    label = tokens[i + 1].content.strip()
                else:
                    label = ""

                # Flush what came before this heading
                end_line = (token.map[0] if token.map else len(body_lines)) + 1
                _flush_section(end_line - 1)

                # If first H1 and no frontmatter title, capture it
                if depth == 1 and title is None:
                    title = label

                # Trim the heading stack to this depth
                heading_stack = [(d, lbl) for d, lbl in heading_stack if d < depth]
                heading_stack.append((depth, label))

                current_lines = []
                current_meta = {}
                # Line start is right after the heading line
                current_line_start = (token.map[1] if token.map else 0) + 1

            elif token.type == "fence":
                fence_lines = _body_lines_for(token)
                current_lines.extend(fence_lines)
                # Store language hint from the fence info
                info = token.info.strip().split()[0] if token.info.strip() else ""
                if info:
                    current_meta.setdefault("fence_languages", [])
                    langs: list[str] = current_meta["fence_languages"]
                    langs.append(info)

            elif token.type not in ("heading_close",):
                tok_lines = _body_lines_for(token)
                if tok_lines:
                    current_lines.extend(tok_lines)

            i += 1

        # Flush the last section
        _flush_section(len(body_lines))

        # If no heading was ever seen, wrap everything in a single section
        if not sections and body.strip():
            sections.append(
                Section(
                    heading_path=[],
                    text=body.strip(),
                    line_start=1,
                    line_end=len(body_lines) or 1,
                )
            )

        return ParsedDocument(
            title=title,
            sections=sections,
            metadata=fm_meta,
            content_type="text/markdown",
        )


__all__ = ["MarkdownParser"]
