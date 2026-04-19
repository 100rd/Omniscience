"""Pluggable embedding providers for Omniscience.

Provider is selected at runtime via :func:`create_embedding_provider` which
reads ``Settings.embedding_provider`` and constructs the appropriate backend.

Supported backends
------------------
* **Ollama** (default) — local or self-hosted, privacy-preserving
* **OpenAI** — ``text-embedding-3-small`` / ``text-embedding-3-large``
* **Voyage** — ``voyage-3`` / ``voyage-3-lite`` / ``voyage-code-3``
* **Cohere** — ``embed-english-v3.0`` / ``embed-multilingual-v3.0`` / ``embed-english-light-v3.0``

Example::

    from omniscience_core.config import Settings
    from omniscience_embeddings import create_embedding_provider

    provider = create_embedding_provider(Settings())
    vectors = await provider.embed(["hello world"])
    await provider.close()
"""

from omniscience_embeddings.base import EmbeddingProvider
from omniscience_embeddings.cohere import CohereEmbeddingProvider
from omniscience_embeddings.factory import create_embedding_provider
from omniscience_embeddings.ollama import OllamaEmbeddingProvider
from omniscience_embeddings.openai import OpenAIEmbeddingProvider
from omniscience_embeddings.voyage import VoyageEmbeddingProvider

__all__ = [
    "CohereEmbeddingProvider",
    "EmbeddingProvider",
    "OllamaEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "VoyageEmbeddingProvider",
    "create_embedding_provider",
]
