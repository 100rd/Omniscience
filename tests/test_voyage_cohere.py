"""Tests for VoyageEmbeddingProvider and CohereEmbeddingProvider.

Covers:
- Protocol compliance
- Property accessors (model_name, provider_name, dim)
- embed(): happy path, empty input, batching, dimension validation
- Retry behaviour: transient failure recovery, exhausted retries
- close(): client cleanup
- Factory routing for 'voyage' and 'cohere'
- Settings integration: voyage_api_key / cohere_api_key fields
"""

from __future__ import annotations

import json

import httpx
import pytest
from omniscience_core.config import Settings
from omniscience_embeddings import (
    CohereEmbeddingProvider,
    EmbeddingProvider,
    VoyageEmbeddingProvider,
    create_embedding_provider,
)

# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------

_VOYAGE_DIM = 1024
_VOYAGE_LITE_DIM = 512
_COHERE_DIM = 1024
_COHERE_LIGHT_DIM = 384


def _make_voyage_response(texts: list[str], dim: int = _VOYAGE_DIM) -> httpx.Response:
    """Build a mock Voyage /v1/embeddings response for *texts*."""
    body = {
        "data": [{"index": i, "embedding": [0.1] * dim} for i in range(len(texts))],
        "model": "voyage-3",
    }
    return httpx.Response(200, json=body)


def _make_cohere_response(texts: list[str], dim: int = _COHERE_DIM) -> httpx.Response:
    """Build a mock Cohere /v2/embed response for *texts*."""
    body = {
        "embeddings": {
            "float": [[0.1] * dim for _ in texts],
        },
        "model": "embed-english-v3.0",
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


# ===========================================================================
# VoyageEmbeddingProvider
# ===========================================================================

# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


def test_voyage_satisfies_protocol() -> None:
    provider = VoyageEmbeddingProvider(api_key="pa-test")
    assert isinstance(provider, EmbeddingProvider)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


def test_voyage_properties_default_model() -> None:
    p = VoyageEmbeddingProvider(api_key="pa-test")
    assert p.model_name == "voyage-3"
    assert p.provider_name == "voyage"
    assert p.dim == 1024


def test_voyage_properties_lite_model() -> None:
    p = VoyageEmbeddingProvider(model="voyage-3-lite", api_key="pa-test")
    assert p.model_name == "voyage-3-lite"
    assert p.dim == 512


def test_voyage_properties_code_model() -> None:
    p = VoyageEmbeddingProvider(model="voyage-code-3", api_key="pa-test")
    assert p.model_name == "voyage-code-3"
    assert p.dim == 1024


def test_voyage_explicit_dim_overrides_table() -> None:
    p = VoyageEmbeddingProvider(model="voyage-3", api_key="pa-test", dim=256)
    assert p.dim == 256


def test_voyage_unknown_model_falls_back_to_1024() -> None:
    p = VoyageEmbeddingProvider(model="voyage-custom", api_key="pa-test")
    assert p.dim == 1024


# ---------------------------------------------------------------------------
# embed — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_voyage_embed_single_batch() -> None:
    texts = ["hello", "world"]
    transport = _make_transport([_make_voyage_response(texts)])
    provider = VoyageEmbeddingProvider(model="voyage-3", api_key="pa-test")
    provider._client = httpx.AsyncClient(base_url="https://api.voyageai.com", transport=transport)

    result = await provider.embed(texts)

    assert len(result) == 2
    assert len(result[0]) == _VOYAGE_DIM
    await provider.close()


@pytest.mark.asyncio
async def test_voyage_embed_empty_returns_empty() -> None:
    provider = VoyageEmbeddingProvider(api_key="pa-test")
    result = await provider.embed([])
    assert result == []
    await provider.close()


@pytest.mark.asyncio
async def test_voyage_embed_preserves_order() -> None:
    """Response items returned out-of-order must be sorted by index."""
    texts = ["a", "b", "c"]

    def handler(request: httpx.Request) -> httpx.Response:
        # Return items in reverse index order
        body = {
            "data": [
                {"index": 2, "embedding": [0.3] * _VOYAGE_DIM},
                {"index": 0, "embedding": [0.1] * _VOYAGE_DIM},
                {"index": 1, "embedding": [0.2] * _VOYAGE_DIM},
            ],
            "model": "voyage-3",
        }
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    provider = VoyageEmbeddingProvider(model="voyage-3", api_key="pa-test")
    provider._client = httpx.AsyncClient(base_url="https://api.voyageai.com", transport=transport)

    result = await provider.embed(texts)

    assert result[0][0] == pytest.approx(0.1)
    assert result[1][0] == pytest.approx(0.2)
    assert result[2][0] == pytest.approx(0.3)
    await provider.close()


# ---------------------------------------------------------------------------
# embed — batching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_voyage_batching_splits_into_correct_calls() -> None:
    """100 texts with batch_size=32 → 4 HTTP calls (32, 32, 32, 4)."""
    texts = [f"text_{i}" for i in range(100)]
    batch_sizes_seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        batch = payload["input"]
        batch_sizes_seen.append(len(batch))
        return _make_voyage_response(batch)

    transport = httpx.MockTransport(handler)
    provider = VoyageEmbeddingProvider(model="voyage-3", api_key="pa-test", batch_size=32)
    provider._client = httpx.AsyncClient(base_url="https://api.voyageai.com", transport=transport)

    result = await provider.embed(texts)

    assert len(result) == 100
    assert batch_sizes_seen == [32, 32, 32, 4]
    await provider.close()


# ---------------------------------------------------------------------------
# embed — dimension validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_voyage_wrong_dimension_raises() -> None:
    texts = ["check dim"]
    wrong_dim = 999

    def handler(request: httpx.Request) -> httpx.Response:
        return _make_voyage_response(texts, dim=wrong_dim)

    transport = httpx.MockTransport(handler)
    provider = VoyageEmbeddingProvider(model="voyage-3", api_key="pa-test")  # expects 1024
    provider._client = httpx.AsyncClient(base_url="https://api.voyageai.com", transport=transport)

    with pytest.raises(ValueError, match="Dimension mismatch"):
        await provider.embed(texts)

    await provider.close()


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_voyage_retry_on_transient_failure() -> None:
    """First call raises ConnectError; second succeeds."""
    texts = ["retry me"]
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("transient failure")
        return _make_voyage_response(texts)

    transport = httpx.MockTransport(handler)
    provider = VoyageEmbeddingProvider(
        model="voyage-3",
        api_key="pa-test",
        max_attempts=3,
        min_backoff=0.0,
        max_backoff=0.0,
    )
    provider._client = httpx.AsyncClient(base_url="https://api.voyageai.com", transport=transport)

    result = await provider.embed(texts)

    assert len(result) == 1
    assert call_count == 2
    await provider.close()


@pytest.mark.asyncio
async def test_voyage_retry_exhausted_raises() -> None:
    """All retry attempts fail → exception is re-raised."""
    texts = ["fail me"]

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("always fails")

    transport = httpx.MockTransport(handler)
    provider = VoyageEmbeddingProvider(
        model="voyage-3",
        api_key="pa-test",
        max_attempts=2,
        min_backoff=0.0,
        max_backoff=0.0,
    )
    provider._client = httpx.AsyncClient(base_url="https://api.voyageai.com", transport=transport)

    with pytest.raises(httpx.ConnectError):
        await provider.embed(texts)

    await provider.close()


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_voyage_close_releases_client() -> None:
    provider = VoyageEmbeddingProvider(api_key="pa-test")
    closed_called = False
    original_aclose = provider._client.aclose

    async def mock_aclose() -> None:
        nonlocal closed_called
        closed_called = True
        await original_aclose()

    provider._client.aclose = mock_aclose  # type: ignore[method-assign]
    await provider.close()
    assert closed_called


# ===========================================================================
# CohereEmbeddingProvider
# ===========================================================================

# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


def test_cohere_satisfies_protocol() -> None:
    provider = CohereEmbeddingProvider(api_key="co-test")
    assert isinstance(provider, EmbeddingProvider)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


def test_cohere_properties_default_model() -> None:
    p = CohereEmbeddingProvider(api_key="co-test")
    assert p.model_name == "embed-english-v3.0"
    assert p.provider_name == "cohere"
    assert p.dim == 1024


def test_cohere_properties_multilingual_model() -> None:
    p = CohereEmbeddingProvider(model="embed-multilingual-v3.0", api_key="co-test")
    assert p.model_name == "embed-multilingual-v3.0"
    assert p.dim == 1024


def test_cohere_properties_light_model() -> None:
    p = CohereEmbeddingProvider(model="embed-english-light-v3.0", api_key="co-test")
    assert p.model_name == "embed-english-light-v3.0"
    assert p.dim == 384


def test_cohere_explicit_dim_overrides_table() -> None:
    p = CohereEmbeddingProvider(model="embed-english-v3.0", api_key="co-test", dim=512)
    assert p.dim == 512


def test_cohere_default_input_type_is_search_document() -> None:
    p = CohereEmbeddingProvider(api_key="co-test")
    assert p._input_type == "search_document"


def test_cohere_search_query_input_type() -> None:
    p = CohereEmbeddingProvider(api_key="co-test", input_type="search_query")
    assert p._input_type == "search_query"


# ---------------------------------------------------------------------------
# embed — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cohere_embed_single_batch() -> None:
    texts = ["hello", "world"]
    transport = _make_transport([_make_cohere_response(texts)])
    provider = CohereEmbeddingProvider(model="embed-english-v3.0", api_key="co-test")
    provider._client = httpx.AsyncClient(base_url="https://api.cohere.com", transport=transport)

    result = await provider.embed(texts)

    assert len(result) == 2
    assert len(result[0]) == _COHERE_DIM
    await provider.close()


@pytest.mark.asyncio
async def test_cohere_embed_empty_returns_empty() -> None:
    provider = CohereEmbeddingProvider(api_key="co-test")
    result = await provider.embed([])
    assert result == []
    await provider.close()


@pytest.mark.asyncio
async def test_cohere_request_includes_input_type() -> None:
    """Verify the 'input_type' field is sent in the request payload."""
    texts = ["doc text"]
    received_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal received_payload
        received_payload = json.loads(request.content)
        return _make_cohere_response(texts)

    transport = httpx.MockTransport(handler)
    provider = CohereEmbeddingProvider(
        model="embed-english-v3.0",
        api_key="co-test",
        input_type="search_query",
    )
    provider._client = httpx.AsyncClient(base_url="https://api.cohere.com", transport=transport)

    await provider.embed(texts)

    assert received_payload.get("input_type") == "search_query"
    assert received_payload.get("model") == "embed-english-v3.0"
    assert "texts" in received_payload
    await provider.close()


@pytest.mark.asyncio
async def test_cohere_request_includes_embedding_types() -> None:
    """Verify 'embedding_types' contains 'float' in request payload."""
    texts = ["check payload"]
    received_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal received_payload
        received_payload = json.loads(request.content)
        return _make_cohere_response(texts)

    transport = httpx.MockTransport(handler)
    provider = CohereEmbeddingProvider(model="embed-english-v3.0", api_key="co-test")
    provider._client = httpx.AsyncClient(base_url="https://api.cohere.com", transport=transport)

    await provider.embed(texts)

    assert received_payload.get("embedding_types") == ["float"]
    await provider.close()


# ---------------------------------------------------------------------------
# embed — batching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cohere_batching_splits_into_correct_calls() -> None:
    """100 texts with batch_size=32 → 4 HTTP calls (32, 32, 32, 4)."""
    texts = [f"text_{i}" for i in range(100)]
    batch_sizes_seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        batch = payload["texts"]
        batch_sizes_seen.append(len(batch))
        return _make_cohere_response(batch)

    transport = httpx.MockTransport(handler)
    provider = CohereEmbeddingProvider(
        model="embed-english-v3.0", api_key="co-test", batch_size=32
    )
    provider._client = httpx.AsyncClient(base_url="https://api.cohere.com", transport=transport)

    result = await provider.embed(texts)

    assert len(result) == 100
    assert batch_sizes_seen == [32, 32, 32, 4]
    await provider.close()


# ---------------------------------------------------------------------------
# embed — dimension validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cohere_wrong_dimension_raises() -> None:
    texts = ["check dim"]
    wrong_dim = 999

    def handler(request: httpx.Request) -> httpx.Response:
        return _make_cohere_response(texts, dim=wrong_dim)

    transport = httpx.MockTransport(handler)
    provider = CohereEmbeddingProvider(
        model="embed-english-v3.0", api_key="co-test"
    )  # expects 1024
    provider._client = httpx.AsyncClient(base_url="https://api.cohere.com", transport=transport)

    with pytest.raises(ValueError, match="Dimension mismatch"):
        await provider.embed(texts)

    await provider.close()


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cohere_retry_on_transient_failure() -> None:
    """First call raises ConnectError; second succeeds."""
    texts = ["retry me"]
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("transient failure")
        return _make_cohere_response(texts)

    transport = httpx.MockTransport(handler)
    provider = CohereEmbeddingProvider(
        model="embed-english-v3.0",
        api_key="co-test",
        max_attempts=3,
        min_backoff=0.0,
        max_backoff=0.0,
    )
    provider._client = httpx.AsyncClient(base_url="https://api.cohere.com", transport=transport)

    result = await provider.embed(texts)

    assert len(result) == 1
    assert call_count == 2
    await provider.close()


@pytest.mark.asyncio
async def test_cohere_retry_exhausted_raises() -> None:
    """All retry attempts fail → exception is re-raised."""
    texts = ["fail me"]

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("always fails")

    transport = httpx.MockTransport(handler)
    provider = CohereEmbeddingProvider(
        model="embed-english-v3.0",
        api_key="co-test",
        max_attempts=2,
        min_backoff=0.0,
        max_backoff=0.0,
    )
    provider._client = httpx.AsyncClient(base_url="https://api.cohere.com", transport=transport)

    with pytest.raises(httpx.ConnectError):
        await provider.embed(texts)

    await provider.close()


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cohere_close_releases_client() -> None:
    provider = CohereEmbeddingProvider(api_key="co-test")
    closed_called = False
    original_aclose = provider._client.aclose

    async def mock_aclose() -> None:
        nonlocal closed_called
        closed_called = True
        await original_aclose()

    provider._client.aclose = mock_aclose  # type: ignore[method-assign]
    await provider.close()
    assert closed_called


# ===========================================================================
# Factory — Voyage and Cohere routing
# ===========================================================================


def test_factory_voyage_from_settings_key() -> None:
    """voyage_api_key in Settings flows through to the provider."""
    settings = Settings(embedding_provider="voyage", voyage_api_key="pa-settings-key")
    provider = create_embedding_provider(settings)
    assert isinstance(provider, VoyageEmbeddingProvider)
    assert provider.provider_name == "voyage"


def test_factory_voyage_from_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """VOYAGE_API_KEY env var is used when settings key is None."""
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-env-key")
    settings = Settings(embedding_provider="voyage", voyage_api_key=None)
    provider = create_embedding_provider(settings)
    assert isinstance(provider, VoyageEmbeddingProvider)


def test_factory_cohere_from_settings_key() -> None:
    """cohere_api_key in Settings flows through to the provider."""
    settings = Settings(embedding_provider="cohere", cohere_api_key="co-settings-key")
    provider = create_embedding_provider(settings)
    assert isinstance(provider, CohereEmbeddingProvider)
    assert provider.provider_name == "cohere"


def test_factory_cohere_from_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """COHERE_API_KEY env var is used when settings key is None."""
    monkeypatch.setenv("COHERE_API_KEY", "co-env-key")
    settings = Settings(embedding_provider="cohere", cohere_api_key=None)
    provider = create_embedding_provider(settings)
    assert isinstance(provider, CohereEmbeddingProvider)


# ===========================================================================
# Settings — new API key fields
# ===========================================================================


def test_settings_voyage_api_key_defaults_to_none() -> None:
    settings = Settings()
    assert settings.voyage_api_key is None


def test_settings_cohere_api_key_defaults_to_none() -> None:
    settings = Settings()
    assert settings.cohere_api_key is None


def test_settings_voyage_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-from-env")
    settings = Settings(voyage_api_key=None)
    # The field is None because env-var fallback lives in the provider, not Settings
    # (Settings.voyage_api_key is an optional explicit override)
    assert settings.voyage_api_key is None


def test_settings_voyage_api_key_explicit_value() -> None:
    settings = Settings(voyage_api_key="pa-explicit")
    assert settings.voyage_api_key == "pa-explicit"


def test_settings_cohere_api_key_explicit_value() -> None:
    settings = Settings(cohere_api_key="co-explicit")
    assert settings.cohere_api_key == "co-explicit"
