"""Document parsers and chunkers for the Omniscience ingestion pipeline.

Public API
----------
Parsers:
    Parser          — protocol all parsers satisfy
    ParsedDocument  — structured output of a parser
    Section         — one structural section within a document
    ParserDispatch  — selects the best parser by content-type / extension
    default_dispatch — pre-configured dispatch with all built-in parsers
    MarkdownParser  — markdown-it-py + frontmatter parser
    PlainTextParser — fallback single-section parser
    TreeSitterParser — tree-sitter backed code symbol extractor
    TerraformParser — Terraform HCL / JSON block extractor
    KubernetesParser — Kubernetes YAML manifest parser

Chunkers:
    Chunker                  — protocol all chunkers satisfy
    ChunkOutput              — one chunk ready for embedding
    CodeSymbolChunker        — one chunk per code symbol
    MarkdownSectionChunker   — one chunk per markdown section
    FixedWindowChunker       — sliding window for plain text

Infrastructure graph:
    EntityData       — node in the infrastructure graph
    EdgeData         — directed dependency edge
    extract_infra_graph — extract graph from a parsed infra document

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
from omniscience_parsers.dispatch import ParserDispatch, default_dispatch
from omniscience_parsers.infra.graph import EdgeData, EntityData, extract_infra_graph
from omniscience_parsers.infra.kubernetes import KubernetesParser
from omniscience_parsers.infra.terraform import TerraformParser
from omniscience_parsers.markdown import MarkdownParser
from omniscience_parsers.plaintext import PlainTextParser

__all__ = [
    "ChunkOutput",
    "Chunker",
    "CodeSymbolChunker",
    "EdgeData",
    "EntityData",
    "FixedWindowChunker",
    "KubernetesParser",
    "MarkdownParser",
    "MarkdownSectionChunker",
    "ParsedDocument",
    "Parser",
    "ParserDispatch",
    "PlainTextParser",
    "Section",
    "TerraformParser",
    "TreeSitterParser",
    "default_dispatch",
    "extract_infra_graph",
]
