"""Shared scaffolding for vendor adapters.

Two things every adapter needs and that would otherwise be copy-pasted:

1. A uniform "mock-mode" gate. CI runs adapters against canned response
   files so external service outages don't break the benchmark pipeline;
   the dev who *wants* to hit live vendors flips an env var.

2. A small in-memory cache keyed on a stable JSON dump of the alert
   payload so a fixture re-run within one process doesn't pay double the
   network cost.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from bench.runner import MCPResponse

#: Set this env var to "1" (or any truthy value) to allow real outbound
#: calls. Default is mock-mode — the CI default — so adapters never
#: hit production vendor endpoints unintentionally.
LIVE_ENV_VAR: Final[str] = "BENCH_LIVE_VENDORS"


def is_live_mode() -> bool:
    """True if the user has explicitly opted in to live vendor calls.

    Centralised so every adapter's mock-vs-live decision goes through
    the same env var and the same truthiness rules.
    """
    raw = os.environ.get(LIVE_ENV_VAR, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def stable_key(payload: dict[str, Any]) -> str:
    """Deterministic JSON dump for mock-table lookup + cache keys."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class MockTable:
    """Bounded lookup of (alert_payload_key -> canned MCPResponse).

    Adapters use this in CI so the benchmark exercises the *same code
    path* as a live run — JSON parsing, normalisation, error handling —
    without depending on a third-party endpoint being up.

    The default-on no-network mode of CI plus the explicit per-vendor
    mock fixtures means we get reproducible numbers; the published
    leaderboard ``bench/results/2026-Q2.md`` is always sourced from a
    single live run, not from these mocks.
    """

    by_key: dict[str, MCPResponse]
    on_miss: Callable[[dict[str, Any]], MCPResponse] | None = None

    def lookup(self, payload: dict[str, Any]) -> MCPResponse:
        """Return canned response or raise KeyError.

        If ``on_miss`` is set, defer to it (used in adapter unit tests
        to assert that mock-mode never silently fakes a result for a
        payload the test author forgot to register).
        """
        key = stable_key(payload)
        if key in self.by_key:
            return self.by_key[key]
        if self.on_miss is not None:
            return self.on_miss(payload)
        raise KeyError(f"mock-mode adapter has no canned response for payload key {key[:80]}...")


def load_mock_table(json_path: Path) -> MockTable:
    """Load a per-vendor mock fixture file.

    File format::

        {
          "<json-encoded alert_payload>": {
            "retrieved_entities": ["...", "..."],
            "confidence": 0.85
          },
          ...
        }

    The keys are produced by :func:`stable_key` so a fixture file
    auto-generated from a real run round-trips deterministically.
    """
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{json_path}: top-level mock fixture must be an object")
    by_key: dict[str, MCPResponse] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            raise ValueError(f"{json_path}: entry for {key!r} must be an object")
        retrieved = entry.get("retrieved_entities", [])
        confidence = entry.get("confidence", 0.0)
        if not isinstance(retrieved, list) or not all(isinstance(e, str) for e in retrieved):
            raise ValueError(f"{json_path}: entry {key!r} retrieved_entities must be list[str]")
        by_key[key] = MCPResponse(
            retrieved_entities=tuple(retrieved),
            confidence=float(confidence),
        )
    return MockTable(by_key=by_key)
