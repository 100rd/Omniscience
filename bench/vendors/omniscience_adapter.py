"""Omniscience MCP HTTP adapter for the benchmark runner.

Thin wrapper over :class:`bench.runner.OmniscienceHTTPClient` that:

* picks up the endpoint from ``OMNISCIENCE_MCP_ENDPOINT`` if not set,
  falling back to the pinned default in ``versions.json``;
* supports a mock-mode (``BENCH_LIVE_VENDORS`` unset) for CI runs.

This adapter is the "reference" — the live Omniscience leaderboard
numbers in ``bench/results/2026-Q2.md`` come from a single run of this
adapter against a docker-compose'd Omniscience.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from bench.runner import MCPResponse, OmniscienceHTTPClient
from bench.vendors._base import MockTable, is_live_mode, load_mock_table
from bench.vendors.versions import VENDOR_VERSIONS

_DEFAULT_MOCKS: Final[Path] = Path(__file__).resolve().parent / "mocks" / "omniscience.json"


class OmniscienceAdapter:
    """MCP client adapter for Omniscience.

    Parameters
    ----------
    endpoint:
        Base URL. If ``None``, uses ``$OMNISCIENCE_MCP_ENDPOINT`` and
        then the pinned default from ``versions.json``.
    mock_table:
        Pre-loaded :class:`MockTable`. If ``None`` and we're not in
        live-mode, the adapter loads ``mocks/omniscience.json``.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        mock_table: MockTable | None = None,
    ) -> None:
        pin = VENDOR_VERSIONS["omniscience"]
        import os

        self.endpoint: str = endpoint or os.environ.get(pin.endpoint_env) or pin.endpoint_default
        self._live = is_live_mode()
        if self._live:
            self._client = OmniscienceHTTPClient(self.endpoint)
            self._mocks: MockTable | None = None
        else:
            self._client = OmniscienceHTTPClient(self.endpoint)  # unused in mock-mode
            self._mocks = mock_table or self._maybe_load_default_mocks()

    @staticmethod
    def _maybe_load_default_mocks() -> MockTable | None:
        if _DEFAULT_MOCKS.exists():
            return load_mock_table(_DEFAULT_MOCKS)
        return None

    def resolve_incident(self, alert_payload: dict[str, Any], *, timeout: float) -> MCPResponse:
        if self._live:
            return self._client.resolve_incident(alert_payload, timeout=timeout)
        if self._mocks is None:
            # No mock file shipped — return a deterministic empty response so
            # the runner reports an honest zero rather than a hard failure.
            return MCPResponse(retrieved_entities=(), confidence=0.0)
        return self._mocks.lookup(alert_payload)
