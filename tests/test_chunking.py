"""Tests for chunking strategies: CodeSymbolChunker, MarkdownSectionChunker, FixedWindowChunker."""

from __future__ import annotations

from omniscience_parsers import (
    CodeSymbolChunker,
    FixedWindowChunker,
    MarkdownSectionChunker,
    ParsedDocument,
    Section,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_doc(sections: list[Section], content_type: str = "text/x-source") -> ParsedDocument:
    return ParsedDocument(sections=sections, content_type=content_type)


def _word_section(
    words: int,
    heading_path: list[str] | None = None,
    symbol: str | None = None,
    line_start: int = 1,
    line_end: int = 10,
) -> Section:
    text = " ".join(f"word{i}" for i in range(words))
    return Section(
        heading_path=heading_path or [],
        text=text,
        symbol=symbol,
        line_start=line_start,
        line_end=line_end,
    )


def _count_tokens(text: str) -> int:
    return len(text.split())


# ---------------------------------------------------------------------------
# CodeSymbolChunker
# ---------------------------------------------------------------------------


class TestCodeSymbolChunker:
    def setup_method(self) -> None:
        self.chunker = CodeSymbolChunker(max_tokens=512, overlap_tokens=50)

    def test_one_chunk_per_small_symbol(self) -> None:
        sections = [
            _word_section(10, ["mod", "func_a"], symbol="mod.func_a"),
            _word_section(10, ["mod", "func_b"], symbol="mod.func_b"),
        ]
        chunks = self.chunker.chunk(_make_doc(sections))
        assert len(chunks) == 2

    def test_symbol_preserved(self) -> None:
        sections = [_word_section(5, symbol="mod.MyClass.method")]
        chunks = self.chunker.chunk(_make_doc(sections))
        assert chunks[0].symbol == "mod.MyClass.method"

    def test_oversized_symbol_splits(self) -> None:
        chunker = CodeSymbolChunker(max_tokens=10, overlap_tokens=2)
        sections = [_word_section(50, ["mod", "big_fn"], symbol="mod.big_fn")]
        chunks = chunker.chunk(_make_doc(sections))
        assert len(chunks) > 1

    def test_oversized_split_all_windows_have_symbol(self) -> None:
        chunker = CodeSymbolChunker(max_tokens=10, overlap_tokens=2)
        sections = [_word_section(50, symbol="mod.fn")]
        chunks = chunker.chunk(_make_doc(sections))
        assert all(c.symbol == "mod.fn" for c in chunks)

    def test_token_budget_respected(self) -> None:
        chunker = CodeSymbolChunker(max_tokens=20, overlap_tokens=5)
        sections = [_word_section(100, symbol="mod.fn")]
        chunks = chunker.chunk(_make_doc(sections))
        for chunk in chunks:
            # heading_prefix words + window words ≤ max + some slack for prefix
            assert _count_tokens(chunk.text) <= 30  # prefix + 20 words max

    def test_ord_is_sequential(self) -> None:
        sections = [_word_section(5, symbol=f"mod.fn{i}") for i in range(5)]
        chunks = self.chunker.chunk(_make_doc(sections))
        ords = [c.ord for c in chunks]
        assert ords == list(range(len(chunks)))

    def test_strategy_in_metadata(self) -> None:
        sections = [_word_section(5, symbol="m.f")]
        chunks = self.chunker.chunk(_make_doc(sections))
        assert chunks[0].strategy == "code_symbol"
        assert chunks[0].metadata.get("strategy") == "code_symbol"

    def test_line_range_in_metadata(self) -> None:
        section = _word_section(5, symbol="m.f", line_start=10, line_end=20)
        chunks = self.chunker.chunk(_make_doc([section]))
        assert chunks[0].metadata["line_range"] == [10, 20]
        assert chunks[0].line_start == 10
        assert chunks[0].line_end == 20

    def test_heading_path_prepended(self) -> None:
        section = _word_section(5, heading_path=["module", "MyClass", "method"])
        chunks = self.chunker.chunk(_make_doc([section]))
        assert "MyClass" in chunks[0].text
        assert "method" in chunks[0].text

    def test_empty_document(self) -> None:
        chunks = self.chunker.chunk(_make_doc([]))
        assert chunks == []

    def test_window_index_in_metadata_for_splits(self) -> None:
        chunker = CodeSymbolChunker(max_tokens=5, overlap_tokens=1)
        sections = [_word_section(30, symbol="mod.fn")]
        chunks = chunker.chunk(_make_doc(sections))
        assert len(chunks) > 1
        for i, chunk in enumerate(chunks):
            assert chunk.metadata.get("window") == i


# ---------------------------------------------------------------------------
# MarkdownSectionChunker
# ---------------------------------------------------------------------------


class TestMarkdownSectionChunker:
    def setup_method(self) -> None:
        self.chunker = MarkdownSectionChunker(max_tokens=512, overlap_tokens=50)

    def test_one_chunk_per_section(self) -> None:
        sections = [
            _word_section(10, heading_path=["A"]),
            _word_section(10, heading_path=["B"]),
        ]
        chunks = self.chunker.chunk(_make_doc(sections, "text/markdown"))
        assert len(chunks) == 2

    def test_heading_context_prepended(self) -> None:
        section = _word_section(5, heading_path=["Architecture", "API"])
        chunks = self.chunker.chunk(_make_doc([section]))
        assert "Architecture" in chunks[0].text
        assert "API" in chunks[0].text

    def test_no_heading_no_prefix(self) -> None:
        section = _word_section(5, heading_path=[])
        chunks = self.chunker.chunk(_make_doc([section]))
        # first token should be the actual content, not a separator
        assert chunks[0].text.strip().startswith("word0")

    def test_large_section_splits(self) -> None:
        chunker = MarkdownSectionChunker(max_tokens=10, overlap_tokens=2)
        sections = [_word_section(80, heading_path=["Big Section"])]
        chunks = chunker.chunk(_make_doc(sections, "text/markdown"))
        assert len(chunks) > 1

    def test_split_windows_all_have_heading(self) -> None:
        chunker = MarkdownSectionChunker(max_tokens=10, overlap_tokens=2)
        sections = [_word_section(80, heading_path=["Title"])]
        chunks = chunker.chunk(_make_doc(sections, "text/markdown"))
        assert all("Title" in c.text for c in chunks)

    def test_ord_sequential(self) -> None:
        sections = [_word_section(5, heading_path=[f"H{i}"]) for i in range(4)]
        chunks = self.chunker.chunk(_make_doc(sections, "text/markdown"))
        assert [c.ord for c in chunks] == list(range(len(chunks)))

    def test_strategy_in_chunk(self) -> None:
        sections = [_word_section(5, heading_path=["S"])]
        chunks = self.chunker.chunk(_make_doc(sections, "text/markdown"))
        assert chunks[0].strategy == "markdown_section"
        assert chunks[0].metadata.get("strategy") == "markdown_section"

    def test_section_path_in_metadata(self) -> None:
        path = ["Root", "Child"]
        sections = [_word_section(5, heading_path=path)]
        chunks = self.chunker.chunk(_make_doc(sections, "text/markdown"))
        assert chunks[0].metadata["section_path"] == path

    def test_line_range_in_metadata(self) -> None:
        section = _word_section(5, heading_path=["S"], line_start=5, line_end=15)
        chunks = self.chunker.chunk(_make_doc([section]))
        assert chunks[0].metadata["line_range"] == [5, 15]

    def test_empty_document(self) -> None:
        chunks = self.chunker.chunk(_make_doc([], "text/markdown"))
        assert chunks == []

    def test_token_budget_respected_after_split(self) -> None:
        chunker = MarkdownSectionChunker(max_tokens=15, overlap_tokens=3)
        sections = [_word_section(200, heading_path=["H"])]
        chunks = chunker.chunk(_make_doc(sections))
        # Heading prefix is "H\n\n" = 1 word; each window ≤ 15 words
        for chunk in chunks:
            assert _count_tokens(chunk.text) <= 20  # some slack for prefix


# ---------------------------------------------------------------------------
# FixedWindowChunker
# ---------------------------------------------------------------------------


class TestFixedWindowChunker:
    def setup_method(self) -> None:
        self.chunker = FixedWindowChunker(max_tokens=10, overlap_tokens=3)

    def test_single_window_for_small_content(self) -> None:
        sections = [_word_section(5)]
        chunks = self.chunker.chunk(_make_doc(sections, "text/plain"))
        assert len(chunks) == 1

    def test_multiple_windows_for_large_content(self) -> None:
        sections = [_word_section(100)]
        chunks = self.chunker.chunk(_make_doc(sections, "text/plain"))
        assert len(chunks) > 1

    def test_window_size_respected(self) -> None:
        chunker = FixedWindowChunker(max_tokens=5, overlap_tokens=0)
        sections = [_word_section(20)]
        chunks = chunker.chunk(_make_doc(sections))
        for chunk in chunks[:-1]:  # last may be smaller
            assert _count_tokens(chunk.text) <= 5

    def test_overlap_present(self) -> None:
        chunker = FixedWindowChunker(max_tokens=5, overlap_tokens=2)
        sections = [_word_section(20)]
        chunks = chunker.chunk(_make_doc(sections))
        if len(chunks) >= 2:
            words_0 = set(chunks[0].text.split())
            words_1 = set(chunks[1].text.split())
            assert len(words_0 & words_1) >= 2  # overlap

    def test_ord_sequential(self) -> None:
        sections = [_word_section(50)]
        chunks = self.chunker.chunk(_make_doc(sections))
        assert [c.ord for c in chunks] == list(range(len(chunks)))

    def test_strategy_field(self) -> None:
        sections = [_word_section(5)]
        chunks = self.chunker.chunk(_make_doc(sections))
        assert all(c.strategy == "fixed_window" for c in chunks)
        assert all(c.metadata.get("strategy") == "fixed_window" for c in chunks)

    def test_window_index_in_metadata(self) -> None:
        sections = [_word_section(30)]
        chunks = self.chunker.chunk(_make_doc(sections))
        for i, chunk in enumerate(chunks):
            assert chunk.metadata.get("window") == i

    def test_no_symbol_for_plain_text(self) -> None:
        sections = [_word_section(5)]
        chunks = self.chunker.chunk(_make_doc(sections))
        assert all(c.symbol is None for c in chunks)

    def test_concatenates_multiple_sections(self) -> None:
        chunker = FixedWindowChunker(max_tokens=100, overlap_tokens=0)
        sections = [_word_section(5, heading_path=["A"]), _word_section(5, heading_path=["B"])]
        chunks = chunker.chunk(_make_doc(sections))
        combined = " ".join(c.text for c in chunks)
        assert "word0" in combined

    def test_empty_document(self) -> None:
        chunks = self.chunker.chunk(_make_doc([]))
        assert chunks == []

    def test_empty_section_text(self) -> None:
        section = Section(heading_path=[], text="   ", line_start=1, line_end=1)
        chunks = self.chunker.chunk(_make_doc([section]))
        assert chunks == []

    def test_large_overlap_relative_to_max(self) -> None:
        # overlap >= max — should still make progress (step = max(1, max-overlap))
        chunker = FixedWindowChunker(max_tokens=5, overlap_tokens=5)
        sections = [_word_section(20)]
        chunks = chunker.chunk(_make_doc(sections))
        # Should not infinite loop and should have at least one chunk
        assert len(chunks) >= 1
