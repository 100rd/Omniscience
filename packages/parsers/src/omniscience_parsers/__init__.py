"""Document parsers and chunkers for the Omniscience ingestion pipeline.

Public API
----------
Parsers:
    Parser          — protocol all parsers satisfy
    ParsedDocument  — structured output of a parser
    Section         — one structural section within a document
    ParserDispatch  — selects the best parser by content-type / extension
    MarkdownParser  — markdown-it-py + frontmatter parser
    PlainTextParser — fallback single-section parser
    TreeSitterParser — tree-sitter backed code symbol extractor

Chunkers:
    Chunker                  — protocol all chunkers satisfy
    ChunkOutput              — one chunk ready for embedding
    CodeSymbolChunker        — one chunk per code symbol
    MarkdownSectionChunker   — one chunk per markdown section
    FixedWindowChunker       — sliding window for plain text
"""

from omniscience_parsers.base import ParsedDocument, Parser, Section
from omniscience_parsers.chunking import (
    Chunker,
    ChunkOutput,
    CodeSymbolChunker,
    FixedWindowChunker,
    MarkdownSectionChunker,
)
from omniscience_parsers.code.treesitter import TreeSitterParser
from omniscience_parsers.dispatch import ParserDispatch
from omniscience_parsers.markdown import MarkdownParser
from omniscience_parsers.plaintext import PlainTextParser

__all__ = [
    "ChunkOutput",
    "Chunker",
    "CodeSymbolChunker",
    "FixedWindowChunker",
    "MarkdownParser",
    "MarkdownSectionChunker",
    "ParsedDocument",
    "Parser",
    "ParserDispatch",
    "PlainTextParser",
    "Section",
    "TreeSitterParser",
]
