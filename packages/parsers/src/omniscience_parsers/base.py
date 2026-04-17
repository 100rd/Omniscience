"""Base types for the parser framework.

Every parser returns a :class:`ParsedDocument` composed of :class:`Section`
objects.  The :class:`Parser` protocol is the contract all parsers satisfy.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field


class Section(BaseModel):
    """A structural section of a parsed document."""

    heading_path: list[str]
    """Heading hierarchy leading to this section, e.g. ["Architecture", "API"]."""

    text: str
    """Raw text content of the section."""

    line_start: int
    """1-based line number of the first line of this section."""

    line_end: int
    """1-based line number of the last line of this section (inclusive)."""

    symbol: str | None = None
    """Fully-qualified name for code symbols, e.g. ``module.ClassName.method``."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Arbitrary key/value metadata (language, fence info, decorator flags, …)."""


class ParsedDocument(BaseModel):
    """Output of a parser — structural representation of a document."""

    title: str | None = None
    """Document title if available (e.g. first H1 heading or frontmatter key)."""

    sections: list[Section]
    """Ordered list of sections extracted from the document."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Document-level metadata (e.g. frontmatter key/value pairs)."""

    content_type: str
    """MIME type or well-known type string, e.g. ``text/markdown``."""

    language: str | None = None
    """Programming language identifier for code documents, e.g. ``python``."""


class Parser(Protocol):
    """Protocol every parser must implement."""

    def can_handle(self, content_type: str, file_extension: str) -> bool:
        """Return True if this parser handles the given content-type and extension."""
        ...

    def parse(self, content: bytes, file_path: str = "") -> ParsedDocument:
        """Parse *content* and return a structured :class:`ParsedDocument`."""
        ...


__all__ = ["ParsedDocument", "Parser", "Section"]
