"""Sourcegraph MCP adapter for the benchmark runner.

Sourcegraph MCP (https://sourcegraph.com/mcp) exposes Sourcegraph's
universal-code-search index over MCP. For SRE incident response it's
narrower than Omniscience (no operational graph, no Slack, no PagerDuty)
but it's an honest "code-only" baseline.

Auth: requires a Sourcegraph access token in ``$SOURCEGRAPH_ACCESS_TOKEN``.
Endpoint: ``https://sourcegraph.com/.api/mcp`` by default; configurable
via ``$SOURCEGRAPH_MCP_ENDPOINT`` (private instances).

Wire format
-----------

Sourcegraph MCP's ``search`` tool takes a free-text query and returns a
flat ``results[]`` with ``{repository, file, snippet, score}``. We
construct a query from the alert ``summary`` + ``target_entity`` and
flatten the top-K results into the canonical IR shape.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final

from bench.runner import MCPResponse
from bench.vendors._base import MockTable, is_live_mode, load_mock_table
from bench.vendors.versions import VENDOR_VERSIONS

_DEFAULT_MOCKS: Final[Path] = Path(__file__).resolve().parent / "mocks" / "sourcegraph_mcp.json"


class SourcegraphMCPAdapter:
    def __init__(
        self,
        endpoint: str | None = None,
        access_token: str | None = None,
        mock_table: MockTable | None = None,
    ) -> None:
        pin = VENDOR_VERSIONS["sourcegraph_mcp"]
        self.endpoint: str = endpoint or os.environ.get(pin.endpoint_env) or pin.endpoint_default
        self._token: str | None = access_token or os.environ.get(
            pin.raw.get("auth_env", "SOURCEGRAPH_ACCESS_TOKEN")
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
        if self._token is None:
            raise RuntimeError(
                "SourcegraphMCPAdapter: live-mode requires SOURCEGRAPH_ACCESS_TOKEN"
            )
        import httpx

        query_terms: list[str] = []
        summary = alert_payload.get("summary")
        if isinstance(summary, str):
            query_terms.append(summary)
        target = alert_payload.get("target_entity")
        if isinstance(target, str):
            query_terms.append(target)
        query = " ".join(query_terms) or "incident"

        response = httpx.post(
            f"{self.endpoint.rstrip('/')}/tools/search",
            headers={"Authorization": f"token {self._token}"},
            json={"query": query, "limit": 20},
            timeout=timeout,
        )
        response.raise_for_status()
        return _sourcegraph_to_response(response.json())


def _sourcegraph_to_response(bundle: dict[str, Any]) -> MCPResponse:
    """Flatten a Sourcegraph MCP ``search`` response into the IR shape.

    Each result is a ``{repository, file, score}`` triple; we synthesise
    a canonical URI ``https://<host>/<repository>/-/blob/<file>`` so it
    matches the ``canonical_entities`` shape of the fixture goldens (we
    use raw github.com URLs in the fixture corpus).
    """
    results = bundle.get("results", []) or []
    host = bundle.get("host", "github.com")
    ranked: list[str] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        repo = item.get("repository")
        path = item.get("file")
        if isinstance(repo, str) and isinstance(path, str):
            ranked.append(f"https://{host}/{repo}/blob/HEAD/{path}")
    confidence = 0.0
    if results and isinstance(results[0], dict):
        confidence = float(results[0].get("score", 0.0))
    return MCPResponse(retrieved_entities=ranked, confidence=confidence)
