"""Cross-encoder re-ranker implementations for retrieval precision.

The re-ranker is an optional post-processing step that runs after the primary
retrieval strategy produces a candidate set.  It scores each candidate against
the query and re-orders the results before the final top-k slice is returned.

Architecture:
    - ``Reranker``       — structural Protocol; any conforming class can be used.
    - ``OllamaReranker`` — uses Ollama's embedding API as a proxy for cross-encoder
                           relevance: embeds the query and each candidate text,
                           then returns cosine similarity scores.
    - ``NoopReranker``   — pass-through; returns the original RRF scores unchanged.
                           Used when re-ranking is disabled.
"""

from __future__ import annotations

import logging
import math
from typing import Protocol, runtime_checkable

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Reranker(Protocol):
    """Structural protocol for all re-ranker backends.

    A re-ranker receives a query string and the *text content* of candidate
    chunks.  It returns a relevance score for each text, in the same order as
    the input list.  Higher scores indicate higher relevance.

    Implementations are responsible for closing any underlying network
    resources via :meth:`close`.
    """

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        """Score *texts* against *query*.

        Args:
            query: The user's search query.
            texts: Candidate chunk texts to score, in retrieval order.

        Returns:
            A list of relevance scores (floats), one per input text, in the
            same order as *texts*.  Scores need not be normalised to [0, 1].
        """
        ...

    async def close(self) -> None:
        """Release underlying resources (HTTP client, connection pool, etc.)."""
        ...


# ---------------------------------------------------------------------------
# Ollama cross-encoder re-ranker (embedding-based cosine similarity)
# ---------------------------------------------------------------------------


class OllamaReranker:
    """Cross-encoder scoring via Ollama embedding similarity.

    Uses the Ollama ``/api/embed`` endpoint to produce dense vectors for both
    the query and each candidate text, then returns the cosine similarity
    between the query vector and each candidate vector.

    This is a lightweight proxy for a true cross-encoder: it uses
    bi-encoder embeddings but scores each candidate independently, giving a
    reasonable precision lift over pure BM25/RRF scores at low latency.

    Args:
        base_url: Root URL of the Ollama server (default: http://localhost:11434).
        model:    Embedding model to use (default: ``nomic-embed-text``).
        timeout:  HTTP request timeout in seconds (default: 30.0).
        batch_size: Number of texts per embedding request (default: 32).
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "nomic-embed-text",
        timeout: float = 30.0,
        batch_size: int = 32,
    ) -> None:
        self._model = model
        self._batch_size = batch_size
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout),
        )

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        """Return cosine similarity scores between *query* and each text.

        Args:
            query: The user's search query.
            texts: Candidate texts to score.

        Returns:
            A list of cosine similarity scores in ``[-1, 1]``, one per input
            text.  Empty list when *texts* is empty.
        """
        if not texts:
            return []

        # Embed query and all candidate texts in one pass (query first).
        all_inputs = [query, *texts]
        all_vectors = await self._embed_all(all_inputs)

        query_vec = all_vectors[0]
        candidate_vecs = all_vectors[1:]

        scores = [_cosine_similarity(query_vec, cv) for cv in candidate_vecs]
        logger.debug(
            "reranker scored %d candidates for query %r (model=%s)",
            len(texts),
            query[:60],
            self._model,
        )
        return scores

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
        logger.debug("ollama_reranker_closed model=%s", self._model)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _embed_all(self, texts: list[str]) -> list[list[float]]:
        """Embed *texts* in batches, concatenating results in order."""
        all_vecs: list[list[float]] = []
        for batch in _chunk(texts, self._batch_size):
            vecs = await self._post_embed(batch)
            all_vecs.extend(vecs)
        return all_vecs

    async def _post_embed(self, batch: list[str]) -> list[list[float]]:
        """POST one batch to the Ollama /api/embed endpoint."""
        payload = {"model": self._model, "input": batch}
        response = await self._client.post("/api/embed", json=payload)
        response.raise_for_status()
        data: dict[str, list[list[float]]] = response.json()
        return data["embeddings"]


# ---------------------------------------------------------------------------
# Noop re-ranker (pass-through)
# ---------------------------------------------------------------------------


class NoopReranker:
    """Pass-through re-ranker that returns the original scores unchanged.

    Used when re-ranking is disabled (``reranker_enabled=False`` in Settings)
    so that callers do not need to branch on ``None``.  The scores returned are
    sequential rank placeholders (``1.0 / (rank + 1)``), reflecting the
    original retrieval order.
    """

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        """Return placeholder scores that preserve the original order.

        Args:
            query: Ignored.
            texts: Candidate texts (only the count matters).

        Returns:
            Decreasing scores ``[1.0, 0.5, 0.333, ...]``.
        """
        return [1.0 / (i + 1) for i in range(len(texts))]

    async def close(self) -> None:
        """No-op; nothing to release."""
        return


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Return the cosine similarity between vectors *a* and *b*.

    Returns 0.0 when either vector is zero-magnitude to avoid division by
    zero.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


def _chunk(items: list[str], size: int) -> list[list[str]]:
    """Split *items* into consecutive sub-lists of at most *size* elements."""
    return [items[i : i + size] for i in range(0, len(items), size)]


__all__ = [
    "NoopReranker",
    "OllamaReranker",
    "Reranker",
]
