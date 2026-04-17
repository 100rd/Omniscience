"""Parser dispatch — selects the best parser for a given content-type + extension.

Usage::

    dispatch = ParserDispatch([MarkdownParser(), TreeSitterParser(), PlainTextParser()])
    parser = dispatch.get_parser("text/markdown", ".md")
    doc = parser.parse(content)
"""

from __future__ import annotations

from omniscience_parsers.base import ParsedDocument, Parser
from omniscience_parsers.plaintext import PlainTextParser


class ParserDispatch:
    """Selects a parser from a registry, falling back to :class:`PlainTextParser`.

    Parsers are evaluated in registration order.  The first one that returns
    ``True`` from :meth:`~Parser.can_handle` wins.  :class:`PlainTextParser`
    is always appended as the final fallback.
    """

    def __init__(self, parsers: list[Parser]) -> None:
        self._parsers: list[Parser] = list(parsers)
        # Ensure PlainTextParser is last
        if not any(isinstance(p, PlainTextParser) for p in self._parsers):
            self._parsers.append(PlainTextParser())

    def get_parser(self, content_type: str, file_extension: str) -> Parser:
        """Return the first parser that claims the content-type / extension."""
        for parser in self._parsers:
            if parser.can_handle(content_type, file_extension):
                return parser
        # PlainTextParser.can_handle always returns True so this is unreachable,
        # but mypy needs a return on all paths.
        return PlainTextParser()  # pragma: no cover

    def parse(
        self,
        content: bytes,
        content_type: str,
        file_extension: str,
        file_path: str = "",
    ) -> ParsedDocument:
        """Convenience: pick a parser and parse in one call."""
        parser = self.get_parser(content_type, file_extension)
        return parser.parse(content, file_path=file_path)


__all__ = ["ParserDispatch"]
