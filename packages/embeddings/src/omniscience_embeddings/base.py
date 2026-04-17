"""Protocol definition for embedding providers."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Structural protocol that all embedding backends must satisfy.

    Implementations must be async-capable and support graceful shutdown
    via ``close()``.  All methods are coroutines so callers can ``await``
    them uniformly regardless of the backend.
    """

    @property
    def dim(self) -> int:
        """Dimensionality of the embedding vectors produced by this provider."""
        ...

    @property
    def model_name(self) -> str:
        """Model identifier as known to the backing service (e.g. 'nomic-embed-text')."""
        ...

    @property
    def provider_name(self) -> str:
        """Human-readable provider identifier (e.g. 'ollama', 'openai')."""
        ...

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Produce embedding vectors for a list of input texts.

        Args:
            texts: Non-empty list of strings to embed.

        Returns:
            A list of float vectors, one per input text, each of length ``dim``.
        """
        ...

    async def close(self) -> None:
        """Release underlying resources (HTTP client, connection pool, etc.)."""
        ...
