"""Cross-vendor MCP-client adapters for the retrieval-quality benchmark.

Each adapter implements the :class:`bench.runner.MCPClient` Protocol so
the existing runner + scorers apply unchanged. Live vendor calls are
gated behind environment variables so CI can run the same code path
against canned mock responses without depending on external services.

Adapters
========

* :class:`OmniscienceAdapter` — local Omniscience MCP HTTP endpoint
* :class:`HolmesGPTAdapter`   — locally-hosted HolmesGPT
* :class:`SourcegraphMCPAdapter` — Sourcegraph MCP server
* :class:`GitHubMCPAdapter`  — vanilla GitHub MCP server

Pinned vendor versions live in :mod:`bench.vendors.versions` and the
on-disk ``versions.json`` so historical results stay comparable.
"""

from __future__ import annotations

from bench.vendors.github_mcp_adapter import GitHubMCPAdapter
from bench.vendors.holmesgpt_adapter import HolmesGPTAdapter
from bench.vendors.omniscience_adapter import OmniscienceAdapter
from bench.vendors.sourcegraph_mcp_adapter import SourcegraphMCPAdapter
from bench.vendors.versions import VENDOR_VERSIONS, VendorPin, load_vendor_pins

__all__ = [
    "VENDOR_VERSIONS",
    "GitHubMCPAdapter",
    "HolmesGPTAdapter",
    "OmniscienceAdapter",
    "SourcegraphMCPAdapter",
    "VendorPin",
    "load_vendor_pins",
]
