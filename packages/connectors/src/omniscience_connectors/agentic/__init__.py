"""Agentic connector framework — LLM-driven discovery for Omniscience.

Public surface::

    from omniscience_connectors.agentic import AgentConfig, AgenticConnector
    from omniscience_connectors.agentic import LLMProvider, OllamaLLMProvider
    from omniscience_connectors.agentic import K8sAgenticConnector, K8sAgenticConfig

The ``AgenticConnector`` base class extends ``Connector`` with an LLM-driven
``discover()`` loop.  Subclasses implement three abstract helpers:

- ``_build_discovery_prompt()`` — produce the prompt from config + context.
- ``_parse_llm_response()`` — convert LLM text to ``DocumentRef`` list.
- ``_default_document_refs()`` — fallback when the LLM fails.

Built-in agentic connectors:

- ``k8s-agentic`` — discovers which Kubernetes resource kinds to index.
"""

from omniscience_connectors.agentic.base import AgentConfig, AgenticConnector
from omniscience_connectors.agentic.k8s import K8sAgenticConfig, K8sAgenticConnector
from omniscience_connectors.agentic.llm import LLMProvider, OllamaLLMProvider, build_provider

__all__ = [
    "AgentConfig",
    "AgenticConnector",
    "K8sAgenticConfig",
    "K8sAgenticConnector",
    "LLMProvider",
    "OllamaLLMProvider",
    "build_provider",
]
