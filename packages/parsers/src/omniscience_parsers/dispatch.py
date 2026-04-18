"""Parser dispatch — selects the best parser for a given content-type + extension.

Usage::

    dispatch = ParserDispatch([MarkdownParser(), TreeSitterParser(), PlainTextParser()])
    parser = dispatch.get_parser("text/markdown", ".md")
    doc = parser.parse(content)

For the standard set of all built-in parsers use :func:`default_dispatch`::

    dispatch = default_dispatch()
    doc = dispatch.parse(content, content_type="", file_extension=".tf")
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


def default_dispatch() -> ParserDispatch:
    """Return a :class:`ParserDispatch` pre-loaded with all built-in parsers.

    Parser priority (first-match wins):
    1. TerraformParser  — .tf, .tf.json
    2. KubernetesParser — .yaml / .yml with a ``kind:`` field
    3. MarkdownParser   — .md, .mdx, .markdown
    4. TreeSitterParser — .py, .ts, .js, .go, .rs, .java
    5. PlainTextParser  — everything else (fallback)
    """
    from omniscience_parsers.code.treesitter import TreeSitterParser
    from omniscience_parsers.infra.kubernetes import KubernetesParser
    from omniscience_parsers.infra.terraform import TerraformParser
    from omniscience_parsers.markdown import MarkdownParser

    return ParserDispatch(
        [
            TerraformParser(),
            KubernetesParser(),
            MarkdownParser(),
            TreeSitterParser(),
            PlainTextParser(),
        ]
    )


__all__ = ["ParserDispatch", "default_dispatch"]
