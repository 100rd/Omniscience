"""AgenticConnector base — LLM-driven discovery extension of the Connector protocol.

AgenticConnector is for sources where discovery scope cannot be expressed
declaratively.  The ``discover()`` method runs a lightweight LLM agent loop:

    prompt → LLM response → parse decision → yield DocumentRefs → repeat

Design constraints:
- Inherits full ``Connector`` interface; ``fetch()`` and ``webhook_handler()``
  are unchanged.
- ``agent_config`` is a ClassVar; subclasses provide their own default.
- No LangGraph or CrewAI dependency for v0.2 — uses simple structured prompts
  via the ``LLMProvider`` protocol.
- Falls back to a deterministic default when the LLM fails or returns
  unparsable JSON.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from omniscience_connectors.base import Connector, DocumentRef

__all__ = [
    "AgentConfig",
    "AgenticConnector",
]

logger = logging.getLogger(__name__)


class AgentConfig(BaseModel):
    """Configuration for the LLM agent used during discovery.

    Passed as a class variable on every ``AgenticConnector`` subclass so it
    can be overridden per-connector without touching the shared default.
    """

    instructions: str = Field(
        default=(
            "You are a discovery agent.  Inspect the source and decide which "
            "documents should be indexed.  Return a JSON object with an "
            "'include' list of resource kinds/paths to index and an 'exclude' "
            "list of resource kinds/paths to skip."
        ),
        description="System prompt / instructions sent to the LLM on every iteration.",
    )

    model_name: str = Field(
        default="llama3",
        description="Name of the model to use with the configured provider.",
    )

    max_iterations: int = Field(
        default=3,
        ge=1,
        le=20,
        description=(
            "Maximum number of LLM round-trips during a single discovery run. "
            "Prevents runaway loops when the source is large."
        ),
    )

    provider: str = Field(
        default="ollama",
        description=(
            "LLM provider key.  Supported: 'ollama'.  Additional providers "
            "may be registered by subclasses."
        ),
    )


class AgenticConnector(Connector):
    """Base class for connectors whose discovery phase is LLM-driven.

    Subclasses MUST:
    1. Declare ``agent_config: ClassVar[AgentConfig]`` with defaults.
    2. Implement ``_build_discovery_prompt()`` to produce the initial prompt.
    3. Implement ``_parse_llm_response()`` to convert LLM text → DocumentRefs.
    4. Implement ``_default_document_refs()`` as the fallback when LLM fails.

    The standard ``fetch()`` and ``webhook_handler()`` methods are left
    abstract — subclasses implement them the same way as a regular Connector.

    Discovery loop (executed by ``discover()``)::

        iteration 0..max_iterations:
            prompt = _build_discovery_prompt(context)
            response = await llm.complete(prompt)
            refs = _parse_llm_response(response)
            if refs:
                yield all refs
                break
        else:
            yield from _default_document_refs(config, secrets)
    """

    agent_config: ClassVar[AgentConfig]

    # ------------------------------------------------------------------
    # Abstract helpers — subclasses implement these
    # ------------------------------------------------------------------

    def _build_discovery_prompt(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        context: dict[str, Any],
    ) -> str:
        """Build the prompt to send to the LLM.

        Args:
            config: Validated public configuration for this source.
            secrets: Runtime secrets (must not be embedded in the prompt).
            context: Accumulated context from previous iterations (initially
                empty).  Subclasses may add hints across iterations.

        Returns:
            Plain-text prompt string sent to the LLM.
        """
        raise NotImplementedError

    def _parse_llm_response(
        self,
        response: str,
        config: BaseModel,
    ) -> list[DocumentRef]:
        """Parse the LLM's text response into a list of DocumentRefs.

        Must not raise — return an empty list when the response is
        unparsable so the loop can retry or fall back to the default.

        Args:
            response: Raw text returned by the LLM provider.
            config: Validated public configuration (for context / base URLs).

        Returns:
            List of ``DocumentRef`` objects.  Empty list signals parse failure.
        """
        raise NotImplementedError

    async def _default_document_refs(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Yield the fallback document set when the LLM fails.

        Called when all LLM iterations produce empty results.  Subclasses
        should yield a safe, conservative set of refs.
        """
        raise NotImplementedError
        yield  # pragma: no cover  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Core discovery loop
    # ------------------------------------------------------------------

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Run the LLM-driven discovery loop.

        Attempts up to ``agent_config.max_iterations`` LLM round-trips.
        Yields DocumentRefs from the first successful parse.  Falls back to
        ``_default_document_refs()`` when all iterations fail.

        Args:
            config: Validated public configuration.
            secrets: Runtime-resolved secret values.

        Yields:
            :class:`~omniscience_connectors.base.DocumentRef` for each
            document the LLM decides to include.
        """
        from omniscience_connectors.agentic.llm import build_provider

        agent_cfg = self.__class__.agent_config
        provider = build_provider(agent_cfg)

        context: dict[str, Any] = {}
        refs: list[DocumentRef] = []

        for iteration in range(agent_cfg.max_iterations):
            prompt = self._build_discovery_prompt(config, secrets, context)
            logger.debug(
                "agentic.discover.iteration",
                extra={
                    "connector_type": self.connector_type,
                    "iteration": iteration,
                    "model": agent_cfg.model_name,
                },
            )

            try:
                response = await provider.complete(prompt)
            except Exception as exc:
                logger.warning(
                    "agentic.discover.llm_error",
                    extra={
                        "connector_type": self.connector_type,
                        "iteration": iteration,
                        "error": str(exc),
                    },
                )
                context["last_error"] = str(exc)
                continue

            refs = self._parse_llm_response(response, config)
            if refs:
                logger.info(
                    "agentic.discover.success",
                    extra={
                        "connector_type": self.connector_type,
                        "iteration": iteration,
                        "ref_count": len(refs),
                    },
                )
                for ref in refs:
                    yield ref
                return

            logger.debug(
                "agentic.discover.empty_parse",
                extra={"connector_type": self.connector_type, "iteration": iteration},
            )
            context["last_response"] = response[:500]  # truncate for context safety

        # All iterations exhausted — fall back to default
        logger.warning(
            "agentic.discover.fallback",
            extra={
                "connector_type": self.connector_type,
                "max_iterations": agent_cfg.max_iterations,
            },
        )
        async for ref in self._default_document_refs(config, secrets):
            yield ref
