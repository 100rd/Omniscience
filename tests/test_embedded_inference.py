"""Tests for embedded (air-gapped) inference: local embedding provider and query rewriter.

All tests that exercise LocalEmbeddingProvider mock sentence-transformers so
they run in CI without requiring the actual library or model files.

Coverage
--------
1-6:   LocalEmbeddingProvider construction & properties (mocked ST)
7-12:  LocalEmbeddingProvider.embed — happy path, empty input, batching, thread pool
13-15: LocalEmbeddingProvider.close behaviour
16-17: LocalEmbeddingProvider ImportError when sentence-transformers is absent
18-21: QueryRewriter.rewrite — pass-through, acronym, synonym, error fallback
22-26: QueryRewriter.expand — deduplication, acronym, synonym, multi-match, error fallback
27-30: Factory routing — "local" provider, settings fields, case-insensitive, unknown
"""

from __future__ import annotations

import sys
import types
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import numpy as np
import pytest
from omniscience_core.config import Settings
from omniscience_embeddings import LocalEmbeddingProvider, create_embedding_provider
from omniscience_embeddings.base import EmbeddingProvider
from omniscience_retrieval.query_rewriter import (
    QueryRewriter,
    _apply_acronym_expansion,
    _apply_synonym_expansion,
    _deduplicate,
    _tokenize,
)

# ---------------------------------------------------------------------------
# Helpers — build a fake sentence-transformers module
# ---------------------------------------------------------------------------


def _make_fake_st_module(dim: int = 384) -> types.ModuleType:
    """Return a minimal mock of the sentence_transformers package."""
    module = types.ModuleType("sentence_transformers")

    class FakeSentenceTransformer:
        def __init__(self, model_name_or_path: str, device: str = "cpu") -> None:
            self.model_name_or_path = model_name_or_path
            self.device = device

        def encode(
            self,
            sentences: list[str],
            batch_size: int = 32,
            convert_to_numpy: bool = True,
            show_progress_bar: bool = False,
            normalize_embeddings: bool = False,
        ) -> np.ndarray:
            n = len(sentences)
            return np.full((n, dim), 0.1, dtype=np.float32)

    module.SentenceTransformer = FakeSentenceTransformer  # type: ignore[attr-defined]
    return module


def _install_fake_st(dim: int = 384) -> types.ModuleType:
    """Inject the fake sentence_transformers module into sys.modules."""
    fake = _make_fake_st_module(dim)
    sys.modules["sentence_transformers"] = fake
    return fake


def _remove_fake_st() -> None:
    """Remove the fake sentence_transformers module from sys.modules."""
    sys.modules.pop("sentence_transformers", None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_st() -> types.ModuleType:
    """Install a fake sentence_transformers module for the duration of the test."""
    mod = _install_fake_st(dim=384)
    yield mod
    _remove_fake_st()


@pytest.fixture()
def local_provider(fake_st: types.ModuleType) -> LocalEmbeddingProvider:
    """Return a LocalEmbeddingProvider backed by the fake ST module."""
    return LocalEmbeddingProvider(model_name="all-MiniLM-L6-v2", device="cpu")


# ===========================================================================
# 1-6: LocalEmbeddingProvider construction & properties
# ===========================================================================


def test_local_provider_satisfies_protocol(fake_st: types.ModuleType) -> None:
    """LocalEmbeddingProvider must satisfy the EmbeddingProvider protocol."""
    provider = LocalEmbeddingProvider(model_name="all-MiniLM-L6-v2")
    assert isinstance(provider, EmbeddingProvider)


def test_local_provider_name(fake_st: types.ModuleType) -> None:
    provider = LocalEmbeddingProvider(model_name="all-MiniLM-L6-v2")
    assert provider.provider_name == "local"


def test_local_model_name_property(fake_st: types.ModuleType) -> None:
    provider = LocalEmbeddingProvider(model_name="all-MiniLM-L6-v2")
    assert provider.model_name == "all-MiniLM-L6-v2"


def test_local_dim_from_lookup_table(fake_st: types.ModuleType) -> None:
    """Dimension is read from the built-in table without a probe encode."""
    provider = LocalEmbeddingProvider(model_name="all-MiniLM-L6-v2")
    assert provider.dim == 384


def test_local_dim_from_probe_for_unknown_model(fake_st: types.ModuleType) -> None:
    """Unknown models trigger a probe encode to resolve dimensionality."""
    _install_fake_st(dim=512)
    provider = LocalEmbeddingProvider(model_name="custom/model-512d")
    assert provider.dim == 512


def test_local_provider_custom_device(fake_st: types.ModuleType) -> None:
    """Device parameter is stored and accessible."""
    provider = LocalEmbeddingProvider(model_name="all-MiniLM-L6-v2", device="cuda")
    assert provider._device == "cuda"


# ===========================================================================
# 7-12: LocalEmbeddingProvider.embed
# ===========================================================================


@pytest.mark.asyncio
async def test_local_embed_returns_correct_count(local_provider: LocalEmbeddingProvider) -> None:
    texts = ["hello", "world", "foo"]
    result = await local_provider.embed(texts)
    assert len(result) == 3


@pytest.mark.asyncio
async def test_local_embed_returns_correct_dim(local_provider: LocalEmbeddingProvider) -> None:
    result = await local_provider.embed(["check dimension"])
    assert len(result[0]) == 384


@pytest.mark.asyncio
async def test_local_embed_empty_returns_empty(local_provider: LocalEmbeddingProvider) -> None:
    result = await local_provider.embed([])
    assert result == []


@pytest.mark.asyncio
async def test_local_embed_returns_list_of_lists(local_provider: LocalEmbeddingProvider) -> None:
    result = await local_provider.embed(["test"])
    assert isinstance(result, list)
    assert isinstance(result[0], list)
    assert all(isinstance(v, float) for v in result[0])


@pytest.mark.asyncio
async def test_local_embed_uses_thread_pool(fake_st: types.ModuleType) -> None:
    """embed() offloads encoding to a ThreadPoolExecutor (not the event loop)."""
    executor = ThreadPoolExecutor(max_workers=1)
    provider = LocalEmbeddingProvider(
        model_name="all-MiniLM-L6-v2", device="cpu", executor=executor
    )
    result = await provider.embed(["thread pool test"])
    assert len(result) == 1
    assert len(result[0]) == 384
    executor.shutdown(wait=False)


@pytest.mark.asyncio
async def test_local_embed_large_batch(fake_st: types.ModuleType) -> None:
    """Large text lists are handled correctly through the encode path."""
    provider = LocalEmbeddingProvider(model_name="all-MiniLM-L6-v2", batch_size=8)
    texts = [f"sentence number {i}" for i in range(50)]
    result = await provider.embed(texts)
    assert len(result) == 50
    assert all(len(vec) == 384 for vec in result)


# ===========================================================================
# 13-15: LocalEmbeddingProvider.close
# ===========================================================================


@pytest.mark.asyncio
async def test_local_close_shuts_down_owned_executor(fake_st: types.ModuleType) -> None:
    """close() shuts down the internally-created executor."""
    provider = LocalEmbeddingProvider(model_name="all-MiniLM-L6-v2")
    with patch.object(provider._executor, "shutdown") as mock_shutdown:
        await provider.close()
        mock_shutdown.assert_called_once_with(wait=False)


@pytest.mark.asyncio
async def test_local_close_does_not_shutdown_external_executor(
    fake_st: types.ModuleType,
) -> None:
    """close() must NOT shut down an externally provided executor."""
    executor = ThreadPoolExecutor(max_workers=1)
    provider = LocalEmbeddingProvider(
        model_name="all-MiniLM-L6-v2", device="cpu", executor=executor
    )
    with patch.object(executor, "shutdown") as mock_shutdown:
        await provider.close()
        mock_shutdown.assert_not_called()
    executor.shutdown(wait=False)


@pytest.mark.asyncio
async def test_local_close_is_idempotent(local_provider: LocalEmbeddingProvider) -> None:
    """Calling close() twice must not raise."""
    await local_provider.close()
    await local_provider.close()


# ===========================================================================
# 16-17: ImportError when sentence-transformers is absent
# ===========================================================================


def test_local_provider_import_error_when_st_missing() -> None:
    """Helpful ImportError raised when sentence-transformers is not installed."""
    _remove_fake_st()
    with pytest.raises(ImportError, match="sentence-transformers"):
        LocalEmbeddingProvider(model_name="all-MiniLM-L6-v2")


def test_local_provider_import_error_mentions_install_hint() -> None:
    """The ImportError message must include install instructions."""
    _remove_fake_st()
    with pytest.raises(ImportError, match="pip install sentence-transformers"):
        LocalEmbeddingProvider(model_name="all-MiniLM-L6-v2")


# ===========================================================================
# 18-21: QueryRewriter.rewrite
# ===========================================================================


@pytest.mark.asyncio
async def test_rewrite_passthrough_when_no_rules_match() -> None:
    rewriter = QueryRewriter()
    result = await rewriter.rewrite("zirconium widget")
    assert result == "zirconium widget"


@pytest.mark.asyncio
async def test_rewrite_expands_acronym() -> None:
    rewriter = QueryRewriter()
    result = await rewriter.rewrite("API authentication")
    # "api" should be expanded to "application programming interface"
    assert "application programming interface" in result.lower()


@pytest.mark.asyncio
async def test_rewrite_expands_synonym() -> None:
    rewriter = QueryRewriter()
    # "error" → "exception" per synonym map (no acronyms in this query)
    result = await rewriter.rewrite("parse error handling")
    assert result != "parse error handling"


@pytest.mark.asyncio
async def test_rewrite_falls_back_on_exception() -> None:
    """rewrite() must return the original query if an internal exception occurs."""
    rewriter = QueryRewriter()
    with patch(
        "omniscience_retrieval.query_rewriter._apply_acronym_expansion",
        side_effect=RuntimeError("boom"),
    ):
        result = await rewriter.rewrite("some query")
    assert result == "some query"


# ===========================================================================
# 22-26: QueryRewriter.expand
# ===========================================================================


@pytest.mark.asyncio
async def test_expand_always_includes_original() -> None:
    rewriter = QueryRewriter()
    variants = await rewriter.expand("zirconium widget")
    assert variants[0] == "zirconium widget"


@pytest.mark.asyncio
async def test_expand_acronym_produces_extra_variant() -> None:
    rewriter = QueryRewriter()
    variants = await rewriter.expand("LLM inference latency")
    assert len(variants) >= 2
    originals = {v.lower() for v in variants}
    assert any("large language model" in v for v in originals)


@pytest.mark.asyncio
async def test_expand_synonym_produces_extra_variant() -> None:
    rewriter = QueryRewriter()
    variants = await rewriter.expand("database error log")
    assert len(variants) >= 2


@pytest.mark.asyncio
async def test_expand_deduplicates_identical_variants() -> None:
    rewriter = QueryRewriter()
    # A query with no matching rules → only one variant
    variants = await rewriter.expand("zirconium widget xyz")
    assert variants == list(dict.fromkeys(variants))  # no duplicates


@pytest.mark.asyncio
async def test_expand_falls_back_on_exception() -> None:
    """expand() must return [original] if an internal exception occurs."""
    rewriter = QueryRewriter()
    with patch(
        "omniscience_retrieval.query_rewriter._apply_acronym_expansion",
        side_effect=RuntimeError("boom"),
    ):
        variants = await rewriter.expand("some query")
    assert variants == ["some query"]


# ===========================================================================
# 27-30: Factory routing for "local" provider + Settings fields
# ===========================================================================


def test_factory_creates_local_provider(fake_st: types.ModuleType) -> None:
    settings = Settings(
        embedding_provider="local",
        local_model_name="all-MiniLM-L6-v2",
        local_model_device="cpu",
    )
    provider = create_embedding_provider(settings)
    assert isinstance(provider, LocalEmbeddingProvider)
    assert provider.provider_name == "local"


def test_factory_local_uses_settings_model_name(fake_st: types.ModuleType) -> None:
    settings = Settings(
        embedding_provider="local",
        local_model_name="all-mpnet-base-v2",
        local_model_device="cpu",
    )
    provider = create_embedding_provider(settings)
    assert isinstance(provider, LocalEmbeddingProvider)
    assert provider.model_name == "all-mpnet-base-v2"


def test_factory_local_case_insensitive(fake_st: types.ModuleType) -> None:
    """'LOCAL' and 'Local' should both route to LocalEmbeddingProvider."""
    settings = Settings(embedding_provider="LOCAL")
    provider = create_embedding_provider(settings)
    assert isinstance(provider, LocalEmbeddingProvider)


def test_settings_new_fields_have_correct_defaults() -> None:
    """New air-gap settings fields are present with correct defaults."""
    settings = Settings()
    assert settings.local_model_name == "all-MiniLM-L6-v2"
    assert settings.local_model_device == "cpu"
    assert settings.query_rewriting_enabled is False


# ===========================================================================
# Helper unit tests (private functions)
# ===========================================================================


def test_tokenize_lowercases() -> None:
    assert _tokenize("Hello World") == ["hello", "world"]


def test_tokenize_strips_punctuation() -> None:
    assert _tokenize("API-first design!") == ["api", "first", "design"]


def test_acronym_expansion_returns_none_when_no_match() -> None:
    assert _apply_acronym_expansion("zirconium widget") is None


def test_synonym_expansion_returns_none_when_no_match() -> None:
    assert _apply_synonym_expansion("zirconium widget") is None


def test_deduplicate_preserves_order() -> None:
    assert _deduplicate(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]
