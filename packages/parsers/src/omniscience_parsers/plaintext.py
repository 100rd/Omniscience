"""Plain-text parser — fallback for any content without a specialised parser."""

from __future__ import annotations

from omniscience_parsers.base import ParsedDocument, Section


class PlainTextParser:
    """Wraps the entire content in a single section.

    Used as the fallback when no other parser claims the content-type or
    file extension.
    """

    def can_handle(self, content_type: str, file_extension: str) -> bool:
        """Accept everything — this parser is the final fallback."""
        return True

    def parse(self, content: bytes, file_path: str = "") -> ParsedDocument:
        """Return a :class:`ParsedDocument` with one section spanning the file."""
        text = content.decode(errors="replace")
        line_count = len(text.splitlines()) or 1
        section = Section(
            heading_path=[],
            text=text,
            line_start=1,
            line_end=line_count,
        )
        return ParsedDocument(
            sections=[section],
            content_type="text/plain",
        )


__all__ = ["PlainTextParser"]
