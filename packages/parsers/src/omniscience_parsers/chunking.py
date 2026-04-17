"""Chunking strategies for the Omniscience ingestion pipeline.

Each strategy implements the :class:`Chunker` protocol and consumes a
:class:`~omniscience_parsers.base.ParsedDocument` produced by a parser,
emitting an ordered list of :class:`ChunkOutput` objects ready for embedding.

Token counting uses ``len(text.split())`` as a word-count approximation.
This is intentional for v0.1 — replace with ``tiktoken`` in v0.2.

Strategies
----------
- :class:`CodeSymbolChunker`   — one chunk per symbol; splits oversized ones.
- :class:`MarkdownSectionChunker` — one chunk per section with heading context.
- :class:`FixedWindowChunker`  — sliding window for plain text.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from omniscience_parsers.base import ParsedDocument, Section

# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------


class ChunkOutput(BaseModel):
    """A single chunk ready for embedding and indexing."""

    ord: int
    """Sequential position of this chunk within the document (0-based)."""

    text: str
    """Text content to be embedded."""

    symbol: str | None = None
    """Fully-qualified symbol name for code chunks."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Strategy-specific metadata (line_range, heading_path, section_path, …)."""

    line_start: int | None = None
    line_end: int | None = None

    strategy: str
    """Identifier for the chunking strategy that produced this chunk."""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class Chunker(Protocol):
    """Protocol every chunker must implement."""

    def chunk(self, parsed: ParsedDocument) -> list[ChunkOutput]: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_tokens(text: str) -> int:
    """Approximate token count via whitespace splitting."""
    return len(text.split())


def _split_text(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    """Split *text* into overlapping windows of at most *max_tokens* words."""
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    step = max(1, max_tokens - overlap_tokens)
    start = 0
    while start < len(words):
        end = min(start + max_tokens, len(words))
        chunks.append(" ".join(words[start:end]))
        if end == len(words):
            break
        start += step
    return chunks


def _heading_prefix(heading_path: list[str]) -> str:
    """Return a breadcrumb prefix string for a heading path, or empty string."""
    return " > ".join(heading_path) + "\n\n" if heading_path else ""


# ---------------------------------------------------------------------------
# CodeSymbolChunker
# ---------------------------------------------------------------------------


class CodeSymbolChunker:
    """One chunk per function/class symbol; splits oversized symbols.

    For code documents parsed by
    :class:`~omniscience_parsers.code.treesitter.TreeSitterParser`.
    """

    STRATEGY = "code_symbol"

    def __init__(self, max_tokens: int = 512, overlap_tokens: int = 50) -> None:
        self._max = max_tokens
        self._overlap = overlap_tokens

    def chunk(self, parsed: ParsedDocument) -> list[ChunkOutput]:
        outputs: list[ChunkOutput] = []
        for section in parsed.sections:
            outputs.extend(self._chunk_section(section, len(outputs)))
        return outputs

    def _chunk_section(self, section: Section, start_ord: int) -> list[ChunkOutput]:
        prefix = _heading_prefix(section.heading_path)
        full_text = prefix + section.text
        base_meta = {
            "strategy": self.STRATEGY,
            "line_range": [section.line_start, section.line_end],
        }

        if _count_tokens(section.text) <= self._max:
            return [
                ChunkOutput(
                    ord=start_ord,
                    text=full_text,
                    symbol=section.symbol,
                    metadata={**base_meta, **section.metadata},
                    line_start=section.line_start,
                    line_end=section.line_end,
                    strategy=self.STRATEGY,
                )
            ]

        # Oversized — split the raw text and prepend heading prefix to each window
        windows = _split_text(section.text, self._max, self._overlap)
        results: list[ChunkOutput] = []
        for i, window in enumerate(windows):
            results.append(
                ChunkOutput(
                    ord=start_ord + i,
                    text=prefix + window,
                    symbol=section.symbol,
                    metadata={**base_meta, "window": i, **section.metadata},
                    line_start=section.line_start,
                    line_end=section.line_end,
                    strategy=self.STRATEGY,
                )
            )
        return results


# ---------------------------------------------------------------------------
# MarkdownSectionChunker
# ---------------------------------------------------------------------------


class MarkdownSectionChunker:
    """One chunk per markdown section with ancestor heading context prepended.

    For documents parsed by :class:`~omniscience_parsers.markdown.MarkdownParser`.
    """

    STRATEGY = "markdown_section"

    def __init__(self, max_tokens: int = 512, overlap_tokens: int = 50) -> None:
        self._max = max_tokens
        self._overlap = overlap_tokens

    def chunk(self, parsed: ParsedDocument) -> list[ChunkOutput]:
        outputs: list[ChunkOutput] = []
        for section in parsed.sections:
            outputs.extend(self._chunk_section(section, len(outputs)))
        return outputs

    def _chunk_section(self, section: Section, start_ord: int) -> list[ChunkOutput]:
        prefix = _heading_prefix(section.heading_path)
        full_text = prefix + section.text
        base_meta: dict[str, Any] = {
            "strategy": self.STRATEGY,
            "section_path": list(section.heading_path),
        }
        if section.line_start:
            base_meta["line_range"] = [section.line_start, section.line_end]

        if _count_tokens(full_text) <= self._max:
            return [
                ChunkOutput(
                    ord=start_ord,
                    text=full_text,
                    symbol=section.symbol,
                    metadata={**base_meta, **section.metadata},
                    line_start=section.line_start,
                    line_end=section.line_end,
                    strategy=self.STRATEGY,
                )
            ]

        # Section too large — split body and prefix heading to each window
        windows = _split_text(section.text, self._max, self._overlap)
        results: list[ChunkOutput] = []
        for i, window in enumerate(windows):
            results.append(
                ChunkOutput(
                    ord=start_ord + i,
                    text=prefix + window,
                    symbol=section.symbol,
                    metadata={**base_meta, "window": i, **section.metadata},
                    line_start=section.line_start,
                    line_end=section.line_end,
                    strategy=self.STRATEGY,
                )
            )
        return results


# ---------------------------------------------------------------------------
# FixedWindowChunker
# ---------------------------------------------------------------------------


class FixedWindowChunker:
    """Sliding fixed-window chunker for plain text documents.

    Concatenates all section text and slides a window with overlap.
    """

    STRATEGY = "fixed_window"

    def __init__(self, max_tokens: int = 512, overlap_tokens: int = 128) -> None:
        self._max = max_tokens
        self._overlap = overlap_tokens

    def chunk(self, parsed: ParsedDocument) -> list[ChunkOutput]:
        full_text = "\n\n".join(s.text for s in parsed.sections if s.text.strip())
        if not full_text.strip():
            return []

        windows = _split_text(full_text, self._max, self._overlap)
        return [
            ChunkOutput(
                ord=i,
                text=window,
                symbol=None,
                metadata={"strategy": self.STRATEGY, "window": i},
                strategy=self.STRATEGY,
            )
            for i, window in enumerate(windows)
        ]


__all__ = [
    "ChunkOutput",
    "Chunker",
    "CodeSymbolChunker",
    "FixedWindowChunker",
    "MarkdownSectionChunker",
]
