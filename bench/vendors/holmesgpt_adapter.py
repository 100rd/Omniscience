"""HolmesGPT adapter for the benchmark runner.

HolmesGPT (https://github.com/HolmesGPT/holmesgpt) is the closest
incident-investigation peer to Omniscience: an open-source agent that
ingests alerts and emits a root-cause hypothesis with supporting
evidence. It runs locally via Docker.

Local setup (mirrors what the CI nightly matrix does)
-----------------------------------------------------

.. code-block:: bash

    HOLMES_IMAGE="$(jq -r '.vendors.holmesgpt.ref' bench/vendors/versions.json)"
    docker pull "$HOLMES_IMAGE"
    docker run -d --name holmes -p 8090:5050 \\
        -e OPENAI_API_KEY=$OPENAI_API_KEY \\
        --entrypoint python "$HOLMES_IMAGE" /app/server.py

    export BENCH_LIVE_VENDORS=1
    export HOLMESGPT_ENDPOINT=http://localhost:8090
    python bench/runner_matrix.py --vendor holmesgpt

In CI we point the matrix job at the docker image declared in
``bench/vendors/versions.json`` rather than calling Docker Hub at run
time; ``actions/cache`` keeps the layer warm across runs.

HolmesGPT's HTTP API is ``POST /api/investigate`` returning JSON with
``analysis``, ``tool_calls``, and ``evidence[]``. We flatten the
ranked evidence URIs into the IR-shape :class:`bench.runner.MCPResponse`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final

from bench.runner import MCPResponse
from bench.vendors._base import MockTable, is_live_mode, load_mock_table
from bench.vendors.versions import VENDOR_VERSIONS

_DEFAULT_MOCKS: Final[Path] = Path(__file__).resolve().parent / "mocks" / "holmesgpt.json"


class HolmesGPTAdapter:
    """MCP-client-shaped adapter that calls a local HolmesGPT server.

    Mock-mode (the default in CI) loads ``mocks/holmesgpt.json``.
    Live-mode requires ``BENCH_LIVE_VENDORS=1`` and a reachable
    HolmesGPT endpoint.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        mock_table: MockTable | None = None,
    ) -> None:
        pin = VENDOR_VERSIONS["holmesgpt"]
        self.endpoint: str = endpoint or os.environ.get(pin.endpoint_env) or pin.endpoint_default
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
    # Live mode — only used when BENCH_LIVE_VENDORS=1
    # ------------------------------------------------------------------

    def _call_live(self, alert_payload: dict[str, Any], *, timeout: float) -> MCPResponse:
        import httpx

        body = {
            "alert": alert_payload,
            "source": alert_payload.get("provider", "generic"),
            "include_evidence": True,
        }
        response = httpx.post(
            f"{self.endpoint.rstrip('/')}/api/investigate",
            json=body,
            timeout=timeout,
        )
        response.raise_for_status()
        return _holmes_to_response(response.json())


def _holmes_to_response(bundle: dict[str, Any]) -> MCPResponse:
    """Map HolmesGPT's investigate-response into the IR shape.

    HolmesGPT returns a flat ``evidence`` array, each with a ``uri`` and
    a ``score`` in [0, 1]. We take the URIs in their natural rank order
    and use the top-scored item's score as the overall confidence.
    """
    evidence = bundle.get("evidence", []) or []
    ranked: list[str] = []
    for item in evidence:
        if isinstance(item, dict) and isinstance(item.get("uri"), str):
            ranked.append(item["uri"])
    confidence = 0.0
    if evidence and isinstance(evidence[0], dict):
        confidence = float(evidence[0].get("score", 0.0))
    return MCPResponse(retrieved_entities=ranked, confidence=confidence)
