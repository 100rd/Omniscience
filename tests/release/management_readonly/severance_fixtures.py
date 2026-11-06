"""AC-SP86-4 severance fixture kit -- Python side.

Single source of truth is
``contracts/releases/management-readonly-v1/severance-fixtures.json`` (loaded
here verbatim, never re-authored) so the Python test suite and the Go
consumer kit (``consumer-kit-go/severance.go``) exercise the exact same
loss/token-expiry/freshness-expiry/contract-skew matrix -- mirrors
``tests/conformance/consumer_severance/fixtures.py``'s role for the SP-10 MCP
severance kit.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

FIXTURES_PATH = (
    Path(__file__).resolve().parents[3]
    / "contracts"
    / "releases"
    / "management-readonly-v1"
    / "severance-fixtures.json"
)


def _load() -> dict[str, Any]:
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))


def all_fixtures() -> tuple[dict[str, Any], ...]:
    return tuple(_load()["fixtures"])


def all_ids() -> frozenset[str]:
    return frozenset(row["id"] for row in all_fixtures())


def expected_decisions() -> dict[str, str]:
    return {row["id"]: row["expected_decision"] for row in all_fixtures()}


def fixture_matrix_sha256() -> str:
    """Content digest over the fixture file bytes -- matches what
    ``scripts/qualify_management_readonly_release.py``'s
    ``severance_fixture_digest`` re-derives, and what a consumer receipt must
    pin against (see ``verify_severance_receipt`` in that script)."""
    return hashlib.sha256(FIXTURES_PATH.read_bytes()).hexdigest()


def fixtures_by_scenario(scenario: str) -> tuple[dict[str, Any], ...]:
    return tuple(row for row in all_fixtures() if row["scenario"] == scenario)
