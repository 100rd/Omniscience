"""Local (in-process) embedding provider using sentence-transformers.

No external API calls — fully air-gapped compatible.  The sentence-transformers
library is an optional dependency; a helpful ImportError is raised at
instantiation time if it is not installed rather than at import time so the
rest of the embeddings package remains importable in environments where the
library is not needed.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Known dimensions for common sentence-transformers models
_MODEL_DIMS: dict[str, int] = {
    "all-MiniLM-L6-v2": 384,
    "all-MiniLM-L12-v2": 384,
    "all-mpnet-base-v2": 768,
    "paraphrase-multilingual-MiniLM-L12-v2": 384,
    "paraphrase-multilingual-mpnet-base-v2": 768,
    "all-distilroberta-v1": 768,
    "multi-qa-MiniLM-L6-cos-v1": 384,
    "multi-qa-mpnet-base-dot-v1": 768,
}


def _require_sentence_transformers() -> Any:
    """Import and return the SentenceTransformer class, raising a clear error if absent."""
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

        return SentenceTransformer
    except ImportError as exc:
        raise ImportError(
            "The 'sentence-transformers' package is required for the local embedding provider "
            "but is not installed.\n\n"
            "Install it with one of:\n"
            "  pip install sentence-transformers\n"
            "  uv add sentence-transformers\n\n"
            "For air-gapped environments, download the model files in advance and point\n"
            "'local_model_name' at the local directory path."
        ) from exc


class LocalEmbeddingProvider:
    """Runs embedding models directly in-process using sentence-transformers.

    No external API calls — fully air-gapped compatible.  Inference is offloaded
    to a ``ThreadPoolExecutor`` so the async event loop stays unblocked during
    potentially CPU-intensive encoding.

    The ``sentence-transformers`` library is an *optional* dependency.  If it is
    not installed, a helpful :class:`ImportError` is raised at instantiation
    time rather than at import time, so the rest of the embeddings package
    remains importable in environments that do not need local inference.

    Args:
        model_name: HuggingFace model ID or local path to a downloaded model.
            Defaults to ``"all-MiniLM-L6-v2"`` (384-d, fast, good quality).
        device: Torch device string passed to sentence-transformers.
            ``"cpu"`` works everywhere; use ``"cuda"`` or ``"mps"`` for GPU/Apple
            Silicon acceleration.
        batch_size: Number of texts passed to the model per encoding call.
            Larger batches are faster on GPU; keep small on CPU to avoid
            memory pressure.
        executor: Optional pre-existing :class:`~concurrent.futures.ThreadPoolExecutor`.
            When *None* a dedicated single-thread executor is created and owned
            by this instance.
    """

    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        device: str = "cpu",
        batch_size: int = 32,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        sentence_transformer_cls = _require_sentence_transformers()

        self._model_name = model_name
        self._device = device
        self._batch_size = batch_size
        self._owns_executor = executor is None
        self._executor: ThreadPoolExecutor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="local-embed"
        )

        log.info(
            "local_embedding_provider_init",
            model=model_name,
            device=device,
            batch_size=batch_size,
        )

        # Load model synchronously at construction time so first embed() call
        # does not pay the cold-start cost.
        self._model: Any = sentence_transformer_cls(model_name, device=device)

        # Resolve dimensionality: prefer the lookup table, fall back to encoding
        # a sentinel text (one inference call at startup, once ever).
        if model_name in _MODEL_DIMS:
            self._dim = _MODEL_DIMS[model_name]
        else:
            probe: Any = self._model.encode(["probe"], convert_to_numpy=True)
            self._dim = int(probe.shape[1])

        log.info("local_embedding_provider_ready", model=model_name, dim=self._dim)

    # --- Protocol properties ------------------------------------------------

    @property
    def dim(self) -> int:
        """Dimensionality of the embedding vectors produced by this provider."""
        return self._dim

    @property
    def model_name(self) -> str:
        """Model identifier (HuggingFace ID or local path)."""
        return self._model_name

    @property
    def provider_name(self) -> str:
        """Human-readable provider identifier."""
        return "local"

    # --- Public API ---------------------------------------------------------

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed *texts* using sentence-transformers in a thread pool.

        Batching is applied inside the thread so the single blocking call
        benefits from the model's native batch encoding support.

        Args:
            texts: Non-empty list of strings to embed.

        Returns:
            A list of float vectors, one per input text, each of length :attr:`dim`.
        """
        if not texts:
            return []

        loop = asyncio.get_event_loop()
        results: list[list[float]] = await loop.run_in_executor(
            self._executor, self._encode_sync, texts
        )
        log.debug("local_embed_ok", model=self._model_name, count=len(results))
        return results

    async def close(self) -> None:
        """Release the thread pool executor (if owned by this instance)."""
        if self._owns_executor:
            self._executor.shutdown(wait=False)
        log.debug("local_embedding_provider_closed", model=self._model_name)

    # --- Internal -----------------------------------------------------------

    def _encode_sync(self, texts: list[str]) -> list[list[float]]:
        """Synchronous encoding — runs inside the thread pool."""
        import numpy as np  # lazy import, numpy ships with sentence-transformers

        raw: Any = self._model.encode(
            texts,
            batch_size=self._batch_size,
            convert_to_numpy=True,
            show_progress_bar=False,
            normalize_embeddings=False,
        )
        # sentence-transformers returns ndarray of shape (N, dim)
        arr = np.asarray(raw, dtype=np.float32)
        result: list[list[float]] = arr.tolist()
        return result
