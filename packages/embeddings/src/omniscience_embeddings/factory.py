"""Factory for constructing embedding providers from application settings."""

from __future__ import annotations

from omniscience_core.config import Settings
from omniscience_core.errors import ConfigError

from omniscience_embeddings.base import EmbeddingProvider
from omniscience_embeddings.ollama import OllamaEmbeddingProvider
from omniscience_embeddings.openai import OpenAIEmbeddingProvider

_SUPPORTED_PROVIDERS = ("ollama", "openai")


def create_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """Instantiate and return the embedding provider specified in *settings*.

    Routes on ``settings.embedding_provider``:

    * ``"ollama"`` → :class:`OllamaEmbeddingProvider` using ``settings.ollama_url``
    * ``"openai"`` → :class:`OpenAIEmbeddingProvider` with default model

    Args:
        settings: Application settings instance.

    Returns:
        A ready-to-use :class:`EmbeddingProvider`.

    Raises:
        ConfigError: When ``settings.embedding_provider`` names an unknown backend.
    """
    provider = settings.embedding_provider.lower()

    if provider == "ollama":
        return OllamaEmbeddingProvider(base_url=settings.ollama_url)

    if provider == "openai":
        return OpenAIEmbeddingProvider()

    raise ConfigError(
        f"Unknown embedding provider '{settings.embedding_provider}'. "
        f"Supported values: {', '.join(_SUPPORTED_PROVIDERS)}."
    )
