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
    TfStateParser   — Terraform state file (JSON v4) parser

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
    extract_tfstate_graph — extract graph from a parsed Terraform state document
    InfraDocument    — fetched infra document (connector metadata + body)
    extract_aws_live_graph — extract graph from a live AWS inventory document
    AWS_LIVE_KIND    — entity kind stamped on live AWS entities

Graph-extraction routing:
    GraphRoute       — which graph extractor owns a document
    route_document   — pick the route from source type + document metadata

Code symbol graph:
    ExtractedEntity      — node in the code symbol graph
    ExtractedEdge        — directed edge in the code symbol graph
    extract_symbol_graph — extract symbol graph from a parsed code document
"""

from omniscience_parsers.base import ParsedDocument, Parser, Section
from omniscience_parsers.chunking import (
    Chunker,
    ChunkOutput,
    CodeSymbolChunker,
    FixedWindowChunker,
    MarkdownSectionChunker,
)
from omniscience_parsers.code.graph import ExtractedEdge, ExtractedEntity, extract_symbol_graph
from omniscience_parsers.code.treesitter import TreeSitterParser
from omniscience_parsers.dispatch import ParserDispatch, default_dispatch
from omniscience_parsers.graph_dispatch import GraphRoute, route_document
from omniscience_parsers.infra.aws_live import (
    AWS_LIVE_KIND,
    InfraDocument,
    extract_aws_live_graph,
)
from omniscience_parsers.infra.graph import (
    EdgeData,
    EntityData,
    extract_infra_graph,
    extract_tfstate_graph,
)
from omniscience_parsers.infra.kubernetes import KubernetesParser
from omniscience_parsers.infra.terraform import TerraformParser
from omniscience_parsers.infra.tfstate import TfStateParser
from omniscience_parsers.markdown import MarkdownParser
from omniscience_parsers.plaintext import PlainTextParser

__all__ = [
    "AWS_LIVE_KIND",
    "ChunkOutput",
    "Chunker",
    "CodeSymbolChunker",
    "EdgeData",
    "EntityData",
    "ExtractedEdge",
    "ExtractedEntity",
    "FixedWindowChunker",
    "GraphRoute",
    "InfraDocument",
    "KubernetesParser",
    "MarkdownParser",
    "MarkdownSectionChunker",
    "ParsedDocument",
    "Parser",
    "ParserDispatch",
    "PlainTextParser",
    "Section",
    "TerraformParser",
    "TfStateParser",
    "TreeSitterParser",
    "default_dispatch",
    "extract_aws_live_graph",
    "extract_infra_graph",
    "extract_symbol_graph",
    "extract_tfstate_graph",
    "route_document",
]
