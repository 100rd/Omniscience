"""LLM provider protocol and built-in implementations for AgenticConnector.

v0.2 uses a simple text-in / text-out interface — no native tool-calling.
Structured output is achieved via prompt engineering (ask the LLM to return
JSON) and ``_parse_llm_response()`` in each connector subclass.

Providers
---------
``OllamaLLMProvider``
    Calls the Ollama local API (``POST /api/generate``).  Default base URL is
    ``http://localhost:11434``.  Configurable via ``AgentConfig.model_name``.

Adding a new provider
---------------------
Implement the ``LLMProvider`` protocol and register it in ``_PROVIDER_REGISTRY``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from omniscience_connectors.agentic.base import AgentConfig

__all__ = [
    "LLMProvider",
    "OllamaLLMProvider",
    "build_provider",
]

logger = logging.getLogger(__name__)

# Default Ollama base URL — override via OllamaLLMProvider(base_url=...)
_OLLAMA_DEFAULT_URL = "http://localhost:11434"


@runtime_checkable
class LLMProvider(Protocol):
    """Minimal protocol for LLM providers used by AgenticConnector.

    All providers must implement a single async method that takes a plain
    text prompt and returns a plain text response.  The connector subclass
    is responsible for embedding structured-output instructions in the prompt
    and parsing the response.
    """

    async def complete(self, prompt: str) -> str:
        """Send *prompt* to the LLM and return the response text.

        Args:
            prompt: Complete prompt string (system instructions + user query).

        Returns:
            Raw text response from the LLM.

        Raises:
            RuntimeError: If the provider cannot reach the LLM service.
            ValueError: If the service returns an unexpected response format.
        """
        ...


class OllamaLLMProvider:
    """LLM provider backed by the Ollama local inference server.

    Calls ``POST /api/generate`` with ``stream=false`` and extracts the
    ``response`` field.

    Args:
        model_name: Ollama model tag (e.g. ``"llama3"``, ``"mistral"``).
        base_url: Base URL of the Ollama server.  Defaults to
            ``http://localhost:11434``.
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        model_name: str,
        base_url: str = _OLLAMA_DEFAULT_URL,
        timeout: float = 60.0,
    ) -> None:
        self._model_name = model_name
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def complete(self, prompt: str) -> str:
        """Send *prompt* to the Ollama generate endpoint.

        Args:
            prompt: Full prompt text.

        Returns:
            Model's response string.

        Raises:
            RuntimeError: On HTTP error or network failure.
            ValueError: If the response JSON is missing the ``response`` field.
        """
        import httpx

        url = f"{self._base_url}/api/generate"
        payload = {
            "model": self._model_name,
            "prompt": prompt,
            "stream": False,
        }

        logger.debug(
            "ollama.complete.request",
            extra={"model": self._model_name, "url": url},
        )

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"Ollama API returned HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"Ollama request failed: {exc}") from exc

        data = response.json()
        if "response" not in data:
            raise ValueError(
                f"Ollama response missing 'response' field. Keys: {list(data.keys())}"
            )

        text: str = data["response"]
        logger.debug(
            "ollama.complete.response",
            extra={"model": self._model_name, "response_len": len(text)},
        )
        return text


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

_PROVIDER_REGISTRY: dict[str, type[OllamaLLMProvider]] = {
    "ollama": OllamaLLMProvider,
}


def build_provider(agent_config: AgentConfig) -> LLMProvider:
    """Construct an ``LLMProvider`` from an ``AgentConfig``.

    Args:
        agent_config: The connector's agent configuration.

    Returns:
        A configured ``LLMProvider`` instance.

    Raises:
        ValueError: If ``agent_config.provider`` is not registered.
    """
    provider_key = agent_config.provider
    provider_cls = _PROVIDER_REGISTRY.get(provider_key)
    if provider_cls is None:
        registered = sorted(_PROVIDER_REGISTRY)
        raise ValueError(
            f"Unknown LLM provider {provider_key!r}.  Registered providers: {registered}"
        )

    if provider_key == "ollama":
        return OllamaLLMProvider(model_name=agent_config.model_name)

    # Future providers may require different constructor signatures.
    return provider_cls(model_name=agent_config.model_name)
