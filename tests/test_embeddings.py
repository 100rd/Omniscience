"""Tests for the omniscience_embeddings package.

Covers:
- OllamaEmbeddingProvider: happy path, batching, retry, dimension validation
- OpenAIEmbeddingProvider: happy path, batching, dimension validation
- Factory routing: ollama, openai, voyage, cohere, unknown provider
- close() cleanup
"""

from __future__ import annotations

import json

import httpx
import pytest
from omniscience_core.config import Settings
from omniscience_core.errors import ConfigError
from omniscience_embeddings import (
    CohereEmbeddingProvider,
    EmbeddingProvider,
    OllamaEmbeddingProvider,
    OpenAIEmbeddingProvider,
    VoyageEmbeddingProvider,
    create_embedding_provider,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OLLAMA_DIM = 768
_OPENAI_DIM = 1536


def _make_ollama_response(texts: list[str], dim: int = _OLLAMA_DIM) -> httpx.Response:
    """Build a mock Ollama /api/embed response for *texts*."""
    body = {"embeddings": [[0.1] * dim for _ in texts]}
    return httpx.Response(200, json=body)


def _make_openai_response(texts: list[str], dim: int = _OPENAI_DIM) -> httpx.Response:
    """Build a mock OpenAI /v1/embeddings response for *texts*."""
    body = {
        "data": [{"index": i, "embedding": [0.1] * dim} for i in range(len(texts))],
        "model": "text-embedding-3-small",
    }
    return httpx.Response(200, json=body)


def _make_transport(responses: list[httpx.Response]) -> httpx.MockTransport:
    """Return a MockTransport that yields *responses* in order."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        resp = responses[call_count]
        call_count += 1
        return resp

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


def test_ollama_satisfies_protocol() -> None:
    provider = OllamaEmbeddingProvider()
    assert isinstance(provider, EmbeddingProvider)


def test_openai_satisfies_protocol() -> None:
    provider = OpenAIEmbeddingProvider(api_key="sk-test")
    assert isinstance(provider, EmbeddingProvider)


# ---------------------------------------------------------------------------
# OllamaEmbeddingProvider — properties
# ---------------------------------------------------------------------------


def test_ollama_properties_nomic() -> None:
    p = OllamaEmbeddingProvider(model="nomic-embed-text")
    assert p.model_name == "nomic-embed-text"
    assert p.provider_name == "ollama"
    assert p.dim == 768


def test_ollama_properties_bge() -> None:
    p = OllamaEmbeddingProvider(model="bge-large-en-v1.5")
    assert p.dim == 1024


def test_ollama_explicit_dim_overrides_default() -> None:
    p = OllamaEmbeddingProvider(model="some-custom-model", dim=512)
    assert p.dim == 512


# ---------------------------------------------------------------------------
# OllamaEmbeddingProvider — embed (happy path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_embed_single_batch() -> None:
    texts = ["hello", "world"]
    transport = _make_transport([_make_ollama_response(texts)])
    provider = OllamaEmbeddingProvider(model="nomic-embed-text")
    provider._client = httpx.AsyncClient(base_url="http://localhost:11434", transport=transport)

    result = await provider.embed(texts)

    assert len(result) == 2
    assert len(result[0]) == _OLLAMA_DIM
    await provider.close()


@pytest.mark.asyncio
async def test_ollama_embed_empty_returns_empty() -> None:
    provider = OllamaEmbeddingProvider()
    result = await provider.embed([])
    assert result == []
    await provider.close()


# ---------------------------------------------------------------------------
# OllamaEmbeddingProvider — batching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_batching_splits_into_correct_calls() -> None:
    """100 texts with batch_size=32 → 4 HTTP calls (32, 32, 32, 4)."""
    texts = [f"text_{i}" for i in range(100)]
    batch_sizes_seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        batch = payload["input"]
        batch_sizes_seen.append(len(batch))
        return _make_ollama_response(batch)

    transport = httpx.MockTransport(handler)
    provider = OllamaEmbeddingProvider(model="nomic-embed-text", batch_size=32)
    provider._client = httpx.AsyncClient(base_url="http://localhost:11434", transport=transport)

    result = await provider.embed(texts)

    assert len(result) == 100
    assert len(batch_sizes_seen) == 4
    assert batch_sizes_seen == [32, 32, 32, 4]
    await provider.close()


# ---------------------------------------------------------------------------
# OllamaEmbeddingProvider — retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_retry_on_transient_failure() -> None:
    """First call raises ConnectError; second call succeeds."""
    texts = ["retry me"]
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("transient failure")
        return _make_ollama_response(texts)

    transport = httpx.MockTransport(handler)
    provider = OllamaEmbeddingProvider(
        model="nomic-embed-text",
        max_attempts=3,
        min_backoff=0.0,
        max_backoff=0.0,
    )
    provider._client = httpx.AsyncClient(base_url="http://localhost:11434", transport=transport)

    result = await provider.embed(texts)

    assert len(result) == 1
    assert call_count == 2
    await provider.close()


@pytest.mark.asyncio
async def test_ollama_retry_exhausted_raises() -> None:
    """All retry attempts fail → exception is re-raised."""
    texts = ["fail me"]

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("always fails")

    transport = httpx.MockTransport(handler)
    provider = OllamaEmbeddingProvider(
        model="nomic-embed-text",
        max_attempts=2,
        min_backoff=0.0,
        max_backoff=0.0,
    )
    provider._client = httpx.AsyncClient(base_url="http://localhost:11434", transport=transport)

    with pytest.raises(httpx.ConnectError):
        await provider.embed(texts)

    await provider.close()


# ---------------------------------------------------------------------------
# OllamaEmbeddingProvider — dimension validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_wrong_dimension_raises() -> None:
    """Server returns vectors with wrong dim → ValueError."""
    texts = ["check dim"]
    wrong_dim = 999

    def handler(request: httpx.Request) -> httpx.Response:
        return _make_ollama_response(texts, dim=wrong_dim)

    transport = httpx.MockTransport(handler)
    provider = OllamaEmbeddingProvider(model="nomic-embed-text")  # expects 768
    provider._client = httpx.AsyncClient(base_url="http://localhost:11434", transport=transport)

    with pytest.raises(ValueError, match="Dimension mismatch"):
        await provider.embed(texts)

    await provider.close()


# ---------------------------------------------------------------------------
# OllamaEmbeddingProvider — close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_close_releases_client() -> None:
    provider = OllamaEmbeddingProvider()
    closed_called = False
    original_aclose = provider._client.aclose

    async def mock_aclose() -> None:
        nonlocal closed_called
        closed_called = True
        await original_aclose()

    provider._client.aclose = mock_aclose  # type: ignore[method-assign]
    await provider.close()
    assert closed_called


# ---------------------------------------------------------------------------
# OpenAIEmbeddingProvider — properties
# ---------------------------------------------------------------------------


def test_openai_properties_small() -> None:
    p = OpenAIEmbeddingProvider(model="text-embedding-3-small", api_key="sk-test")
    assert p.model_name == "text-embedding-3-small"
    assert p.provider_name == "openai"
    assert p.dim == 1536


def test_openai_properties_large() -> None:
    p = OpenAIEmbeddingProvider(model="text-embedding-3-large", api_key="sk-test")
    assert p.dim == 3072


# ---------------------------------------------------------------------------
# OpenAIEmbeddingProvider — embed (happy path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_embed_single_batch() -> None:
    texts = ["foo", "bar", "baz"]
    transport = _make_transport([_make_openai_response(texts)])
    provider = OpenAIEmbeddingProvider(model="text-embedding-3-small", api_key="sk-test")
    provider._client = httpx.AsyncClient(base_url="https://api.openai.com", transport=transport)

    result = await provider.embed(texts)

    assert len(result) == 3
    assert len(result[0]) == _OPENAI_DIM
    await provider.close()


@pytest.mark.asyncio
async def test_openai_embed_empty_returns_empty() -> None:
    provider = OpenAIEmbeddingProvider(api_key="sk-test")
    result = await provider.embed([])
    assert result == []
    await provider.close()


# ---------------------------------------------------------------------------
# OpenAIEmbeddingProvider — batching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_batching_splits_into_correct_calls() -> None:
    """100 texts with batch_size=32 → 4 HTTP calls."""
    texts = [f"text_{i}" for i in range(100)]
    batch_sizes_seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        batch = payload["input"]
        batch_sizes_seen.append(len(batch))
        return _make_openai_response(batch)

    transport = httpx.MockTransport(handler)
    provider = OpenAIEmbeddingProvider(
        model="text-embedding-3-small", api_key="sk-test", batch_size=32
    )
    provider._client = httpx.AsyncClient(base_url="https://api.openai.com", transport=transport)

    result = await provider.embed(texts)

    assert len(result) == 100
    assert len(batch_sizes_seen) == 4
    assert batch_sizes_seen == [32, 32, 32, 4]
    await provider.close()


# ---------------------------------------------------------------------------
# OpenAIEmbeddingProvider — dimension validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_wrong_dimension_raises() -> None:
    texts = ["check dim"]
    wrong_dim = 42

    def handler(request: httpx.Request) -> httpx.Response:
        return _make_openai_response(texts, dim=wrong_dim)

    transport = httpx.MockTransport(handler)
    provider = OpenAIEmbeddingProvider(
        model="text-embedding-3-small", api_key="sk-test"
    )  # expects 1536
    provider._client = httpx.AsyncClient(base_url="https://api.openai.com", transport=transport)

    with pytest.raises(ValueError, match="Dimension mismatch"):
        await provider.embed(texts)

    await provider.close()


# ---------------------------------------------------------------------------
# OpenAIEmbeddingProvider — close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_close_releases_client() -> None:
    provider = OpenAIEmbeddingProvider(api_key="sk-test")
    closed_called = False
    original_aclose = provider._client.aclose

    async def mock_aclose() -> None:
        nonlocal closed_called
        closed_called = True
        await original_aclose()

    provider._client.aclose = mock_aclose  # type: ignore[method-assign]
    await provider.close()
    assert closed_called


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_factory_creates_ollama() -> None:
    settings = Settings(embedding_provider="ollama", ollama_url="http://localhost:11434")
    provider = create_embedding_provider(settings)
    assert isinstance(provider, OllamaEmbeddingProvider)
    assert provider.provider_name == "ollama"


def test_factory_creates_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings = Settings(embedding_provider="openai")
    provider = create_embedding_provider(settings)
    assert isinstance(provider, OpenAIEmbeddingProvider)
    assert provider.provider_name == "openai"


def test_factory_creates_voyage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
    settings = Settings(embedding_provider="voyage")
    provider = create_embedding_provider(settings)
    assert isinstance(provider, VoyageEmbeddingProvider)
    assert provider.provider_name == "voyage"


def test_factory_creates_cohere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COHERE_API_KEY", "co-test")
    settings = Settings(embedding_provider="cohere")
    provider = create_embedding_provider(settings)
    assert isinstance(provider, CohereEmbeddingProvider)
    assert provider.provider_name == "cohere"


def test_factory_unknown_provider_raises() -> None:
    settings = Settings(embedding_provider="unknown-provider-xyz")
    with pytest.raises(ConfigError, match="Unknown embedding provider"):
        create_embedding_provider(settings)


def test_factory_case_insensitive() -> None:
    """Provider name matching should be case-insensitive."""
    settings = Settings(embedding_provider="OLLAMA", ollama_url="http://localhost:11434")
    provider = create_embedding_provider(settings)
    assert isinstance(provider, OllamaEmbeddingProvider)


def test_factory_ollama_uses_settings_url() -> None:
    custom_url = "http://ollama-host:11434"
    settings = Settings(embedding_provider="ollama", ollama_url=custom_url)
    provider = create_embedding_provider(settings)
    assert isinstance(provider, OllamaEmbeddingProvider)
    # Verify the client's base_url incorporates the custom URL
    assert custom_url in str(provider._client.base_url)
