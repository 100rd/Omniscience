"""Cohere embedding provider implementation."""

from __future__ import annotations

import os
from typing import Any, Literal

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_COHERE_BASE_URL = "https://api.cohere.com"

# Known model dimensions for Cohere embedding models
_MODEL_DIMS: dict[str, int] = {
    "embed-english-v3.0": 1024,
    "embed-multilingual-v3.0": 1024,
    "embed-english-light-v3.0": 384,
    "embed-multilingual-light-v3.0": 384,
}

# Cohere v3 models require an explicit input_type
CohereInputType = Literal["search_document", "search_query", "classification", "clustering"]

_RETRYABLE = (
    httpx.ConnectError,
    httpx.TimeoutException,
    httpx.RemoteProtocolError,
    httpx.HTTPStatusError,
)


class CohereEmbeddingProvider:
    """Embedding provider backed by the Cohere Embeddings API (v2).

    Supports ``embed-english-v3.0`` (1024-d),
    ``embed-multilingual-v3.0`` (1024-d), and
    ``embed-english-light-v3.0`` (384-d).

    Cohere v3 models require an *input_type* that signals how the text will
    be used: ``"search_document"`` for texts being indexed into a corpus and
    ``"search_query"`` for user queries at retrieval time.

    The API key is read from the ``COHERE_API_KEY`` environment variable by
    default; you can also pass it explicitly via the *api_key* argument.

    Args:
        model: Cohere embedding model name (default: 'embed-english-v3.0').
        api_key: Cohere API key.  Falls back to ``COHERE_API_KEY`` env var.
        base_url: API base URL (override for proxies / testing).
        input_type: Semantic role for the texts.  Use ``'search_document'``
            when building an index and ``'search_query'`` for query vectors.
            Defaults to ``'search_document'``.
        dim: Expected embedding dimensionality.  Inferred from *model* when
            the model is in the built-in table.
        batch_size: Maximum texts per request (default: 32).
        max_attempts: Total retry attempts (default: 3).
        min_backoff: Minimum exponential back-off seconds (default: 1.0).
        max_backoff: Maximum exponential back-off seconds (default: 10.0).
        timeout: HTTP request timeout seconds (default: 30.0).
    """

    def __init__(
        self,
        *,
        model: str = "embed-english-v3.0",
        api_key: str | None = None,
        base_url: str = _COHERE_BASE_URL,
        input_type: CohereInputType = "search_document",
        dim: int | None = None,
        batch_size: int = 32,
        max_attempts: int = 3,
        min_backoff: float = 1.0,
        max_backoff: float = 10.0,
        timeout: float = 30.0,
    ) -> None:
        resolved_key = api_key or os.environ.get("COHERE_API_KEY", "")
        self._model = model
        self._input_type = input_type
        self._dim = dim if dim is not None else _MODEL_DIMS.get(model, 1024)
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout),
            headers={
                "Authorization": f"Bearer {resolved_key}",
                "Content-Type": "application/json",
            },
        )

    # --- Protocol properties ------------------------------------------------

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return "cohere"

    # --- Public API ---------------------------------------------------------

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed *texts* in batches, returning one vector per input text."""
        if not texts:
            return []

        all_embeddings: list[list[float]] = []
        for batch in _chunk(texts, self._batch_size):
            vectors = await self._embed_batch_with_retry(batch)
            all_embeddings.extend(vectors)
        return all_embeddings

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
        log.debug("cohere_client_closed", model=self._model)

    # --- Internal -----------------------------------------------------------

    async def _embed_batch_with_retry(self, batch: list[str]) -> list[list[float]]:
        """Embed a single batch with exponential back-off retry.

        Uses tenacity with ``reraise=True`` so the original exception surfaces
        after all attempts are exhausted rather than a ``RetryError``.
        """
        wrapped = retry(
            retry=retry_if_exception_type(_RETRYABLE),
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(min=self._min_backoff, max=self._max_backoff),
            reraise=True,
        )(self._post_embed)
        return await wrapped(batch)

    async def _post_embed(self, batch: list[str]) -> list[list[float]]:
        """POST one batch to the Cohere /v2/embed endpoint."""
        payload: dict[str, Any] = {
            "model": self._model,
            "texts": batch,
            "input_type": self._input_type,
            "embedding_types": ["float"],
        }
        log.debug("cohere_embed_request", model=self._model, batch_size=len(batch))

        response = await self._client.post("/v2/embed", json=payload)
        response.raise_for_status()

        data: dict[str, Any] = response.json()
        # Cohere v2 returns embeddings nested under embeddings.float
        vectors: list[list[float]] = data["embeddings"]["float"]
        _validate_dimensions(vectors, self._dim, self._model)

        log.debug("cohere_embed_ok", model=self._model, count=len(vectors))
        return vectors


# --- Helpers ----------------------------------------------------------------


def _chunk(items: list[str], size: int) -> list[list[str]]:
    """Split *items* into consecutive sub-lists of at most *size* elements."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def _validate_dimensions(
    vectors: list[list[float]],
    expected_dim: int,
    model: str,
) -> None:
    """Raise ValueError if any returned vector has the wrong dimensionality."""
    for i, vec in enumerate(vectors):
        if len(vec) != expected_dim:
            raise ValueError(
                f"Dimension mismatch from model '{model}': "
                f"expected {expected_dim}, got {len(vec)} at index {i}"
            )
