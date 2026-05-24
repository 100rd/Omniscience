"""Vanilla GitHub MCP adapter for the benchmark runner.

The reference GitHub MCP server lives at
https://github.com/modelcontextprotocol/servers/tree/main/src/github
and offers ``search_code``, ``search_issues``, and ``get_pull_request``
tools backed by the GitHub Search API.

For the benchmark we use ``search_code`` + ``search_issues`` with a
combined query derived from the alert. This is the weakest baseline —
no operational context, no graph, no recency model — but it represents
the cheapest "what if I just plugged my LLM into GitHub directly?"
alternative an honest customer would compare us to.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final

from bench.runner import MCPResponse
from bench.vendors._base import MockTable, is_live_mode, load_mock_table
from bench.vendors.versions import VENDOR_VERSIONS

_DEFAULT_MOCKS: Final[Path] = Path(__file__).resolve().parent / "mocks" / "github_mcp.json"


class GitHubMCPAdapter:
    def __init__(
        self,
        endpoint: str | None = None,
        github_token: str | None = None,
        mock_table: MockTable | None = None,
    ) -> None:
        pin = VENDOR_VERSIONS["github_mcp"]
        self.endpoint: str = endpoint or os.environ.get(pin.endpoint_env) or pin.endpoint_default
        self._token: str | None = github_token or os.environ.get(
            pin.raw.get("auth_env", "GITHUB_TOKEN")
        )
        self._live = is_live_mode()
        self._mocks: MockTable | None = None
        if not self._live:
            self._mocks = mock_table or self._maybe_load_default_mocks()

    @staticmethod
    def _maybe_load_default_mocks() -> MockTable | None:
        if _DEFAULT_MOCKS.exists():
            return load_mock_table(_DEFAULT_MOCKS)
        return None

    def resolve_incident(self, alert_payload: dict[str, Any], *, timeout: float) -> MCPResponse:
        if not self._live:
            if self._mocks is None:
                return MCPResponse(retrieved_entities=(), confidence=0.0)
            return self._mocks.lookup(alert_payload)
        return self._call_live(alert_payload, timeout=timeout)

    # ------------------------------------------------------------------

    def _call_live(self, alert_payload: dict[str, Any], *, timeout: float) -> MCPResponse:
        import httpx

        headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        summary = alert_payload.get("summary", "")
        target = alert_payload.get("target_entity", "")
        query_terms: list[str] = []
        if isinstance(summary, str) and summary:
            query_terms.append(summary)
        if isinstance(target, str) and target:
            query_terms.append(target)
        query = " ".join(query_terms) or "incident"

        body = {
            "tool": "search_issues",
            "arguments": {"query": query, "per_page": 20},
        }
        response = httpx.post(
            f"{self.endpoint.rstrip('/')}/tools/call",
            headers=headers,
            json=body,
            timeout=timeout,
        )
        response.raise_for_status()
        return _github_to_response(response.json())


def _github_to_response(bundle: dict[str, Any]) -> MCPResponse:
    """Map GitHub MCP search-issues output into the IR shape.

    Each ``items[]`` entry from GitHub Search has ``html_url`` and
    ``score``; we use ``html_url`` directly as the canonical entity.
    """
    items = bundle.get("items") or bundle.get("result", {}).get("items") or []
    ranked: list[str] = []
    for item in items:
        if isinstance(item, dict):
            url = item.get("html_url") or item.get("url")
            if isinstance(url, str):
                ranked.append(url)
    confidence = 0.0
    if items and isinstance(items[0], dict):
        raw_score = items[0].get("score", 0.0)
        # GitHub Search API scores are unbounded floats; normalise into [0, 1]
        # with a soft cap of 10.0 (95th percentile in our test runs).
        confidence = min(1.0, max(0.0, float(raw_score) / 10.0))
    return MCPResponse(retrieved_entities=ranked, confidence=confidence)
