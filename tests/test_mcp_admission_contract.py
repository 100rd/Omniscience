"""Conformance tests for the MCP-side overload error shape (issue #350,
AC-SCALE-3). `mcp/server.py` is out of this task's scope, so nothing here
wires a live MCP call site — these tests instead prove
`admission.mcp_contract` produces a contract-correct stable error rather
than a value that would violate the packaged, closed
`fallback.reason` enum in `mcp/contracts/v1/schemas/meta.schema.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from omniscience_server.admission.mcp_contract import (
    ADMISSION_BACKEND_UNAVAILABLE_CODE,
    RATE_LIMITED_CODE,
    admission_error_code,
    raise_admission_error,
)
from omniscience_server.admission.state_machine import (
    REASON_BACKGROUND_SUSPENDED,
    REASON_LANE_EXHAUSTED,
    STATE_BACKEND_LOST,
    STATE_NORMAL,
    AdmissionOutcome,
)

_META_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "apps"
    / "server"
    / "src"
    / "omniscience_server"
    / "mcp"
    / "contracts"
    / "v1"
    / "schemas"
    / "meta.schema.json"
)


def _closed_fallback_reason_enum() -> set[str | None]:
    schema = json.loads(_META_SCHEMA_PATH.read_text())
    return set(schema["properties"]["fallback"]["properties"]["reason"]["enum"])


def test_admission_codes_are_not_in_the_closed_fallback_reason_enum() -> None:
    """Proves the design choice this module documents: an admission-rejected
    code is deliberately NOT plugged into the closed `fallback.reason` enum
    (which is out of scope to edit) — it is a stable tool-level error
    instead, matching REQ-MCP-4."""
    closed_enum = _closed_fallback_reason_enum()
    assert RATE_LIMITED_CODE not in closed_enum
    assert ADMISSION_BACKEND_UNAVAILABLE_CODE not in closed_enum


def test_admission_error_codes_match_rest_contract_codes() -> None:
    """Cross-transport consistency: the same `code` strings REST's 429/503
    envelope uses (rest/rate_limit.py, rest/errors.py)."""
    assert RATE_LIMITED_CODE == "rate_limited"
    assert ADMISSION_BACKEND_UNAVAILABLE_CODE == "admission_backend_unavailable"


def test_admission_error_code_for_normal_lane_exhaustion_is_rate_limited() -> None:
    outcome = AdmissionOutcome(allowed=False, state=STATE_NORMAL, reason=REASON_LANE_EXHAUSTED)
    assert admission_error_code(outcome) == RATE_LIMITED_CODE


def test_admission_error_code_for_backend_loss_is_backend_unavailable() -> None:
    outcome = AdmissionOutcome(
        allowed=False, state=STATE_BACKEND_LOST, reason=REASON_BACKGROUND_SUSPENDED
    )
    assert admission_error_code(outcome) == ADMISSION_BACKEND_UNAVAILABLE_CODE


def test_raise_admission_error_follows_the_existing_stable_code_prefix_convention() -> None:
    """Mirrors every existing mcp/server.py rejection
    (`raise ValueError("forbidden:...")`, `raise ValueError("bad_request:...")`,
    etc.) — a `ValueError` whose message starts with `"<code>:"`, which the
    MCP framework turns into a stable tool-level error."""
    outcome = AdmissionOutcome(allowed=False, state=STATE_NORMAL, reason=REASON_LANE_EXHAUSTED)
    with pytest.raises(ValueError, match=r"^rate_limited:"):
        raise_admission_error(outcome)


def test_raise_admission_error_backend_loss_message_prefix() -> None:
    outcome = AdmissionOutcome(
        allowed=False, state=STATE_BACKEND_LOST, reason=REASON_BACKGROUND_SUSPENDED
    )
    with pytest.raises(ValueError, match=r"^admission_backend_unavailable:"):
        raise_admission_error(outcome)
