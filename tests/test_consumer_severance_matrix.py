"""AC-SEV-1 conformance tests — MCP v1 consumer-severance fixture matrix.

Invariant: every value of the closed `meta.fallback.reason` enum, every
REQ-MCP-4 payload-shape failure, and a representative set of `canary.py`
contract-pin fail-closed codes must deterministically select the correct
consumer decision (`use` / `direct_source_fallback` / `park` /
`planning_only`), and AC-SEV-4's oracle-boundary must hold for every one of
them: no decision is ever a sole correctness/authorization oracle.

Ground truth: `tests/conformance/consumer_severance/fixtures.py` (fixture
data, hand-derived from `metadata.py`/`canary.py`/`server.py`) and
`apps/server/src/omniscience_server/mcp/contracts/v1/schemas/meta.schema.json`
(read as JSON, never imported as Python — see the exhaustiveness gate
below). This kit does not import `omniscience_server.mcp.metadata` or
`.canary`; those live under `apps/**`, out of this task's scope.

Test categories
----------------
1. Reason-fixture matrix — `(meta fixture) -> expected decision`.
2. Exhaustiveness gate — every non-null `fallback.reason` schema enum value
   is covered by exactly the fixture set, 1:1, both directions.
3. Payload-shape matrix (REQ-MCP-4) — transport-level failures.
4. Contract-pin matrix (canary.py fail-closed codes, taxonomy §2).
5. Fail-closed behaviour on `fallback.reason` schema/enum drift.
6. AC-SEV-4 oracle-boundary — no decision, and no terminal action, ever
   collapses the fitness gate into an authorization gate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.conformance.consumer_severance.fixtures import (
    CONTRACT_PIN_FIXTURES,
    PAYLOAD_SHAPE_FIXTURES,
    REASON_FIXTURES,
    ContractPinFixture,
    PayloadShapeFixture,
    ReasonFixture,
)
from tests.conformance.consumer_severance.reference_consumer import (
    TERMINAL_ACTIONS,
    decide_from_meta,
    decide_on_contract_pin_failure,
    decide_on_payload_shape,
    is_sole_oracle,
    requires_independent_authority,
)

META_SCHEMA_PATH = (
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

CANARY_PATH = (
    Path(__file__).resolve().parents[1]
    / "apps"
    / "server"
    / "src"
    / "omniscience_server"
    / "mcp"
    / "canary.py"
)

_FAIL_CALL_RE = re.compile(r'_fail\(\s*"([a-z_0-9]+)"\s*\)')

# canary.py:531-543 interpolates f"contract_info_{field}_mismatch" per key
# of the `expected_values` dict in `verify_runtime_observation` — a
# dynamic f-string, not a string literal, so `_FAIL_CALL_RE` can't see
# these 8 codes. Field names hardcoded here as ground truth (mirrors
# `fixtures._CONTRACT_INFO_FIELD_MISMATCH_FIELDS`).
_DYNAMIC_CONTRACT_INFO_FIELDS: tuple[str, ...] = (
    "version",
    "commit",
    "manifest_sha256",
    "schema_sha256",
    "schema_digests",
    "tool_registry_sha256",
    "token_profile_id",
    "capabilities",
)


# ---------------------------------------------------------------------------
# 1. Reason-fixture matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    REASON_FIXTURES,
    ids=[f.id for f in REASON_FIXTURES],
)
def test_reason_fixture_selects_expected_decision(fixture: ReasonFixture) -> None:
    decision = decide_from_meta(
        fixture.meta(),
        direct_source_available=fixture.direct_source_available,
    )
    assert decision == fixture.expected_decision


def test_reason_fixture_ids_are_unique() -> None:
    ids = [f.id for f in REASON_FIXTURES]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# 2. Exhaustiveness gate — 1:1 coverage of the closed fallback.reason enum
# ---------------------------------------------------------------------------


def test_fallback_reason_enum_is_covered_exactly_by_fixtures() -> None:
    schema = json.loads(META_SCHEMA_PATH.read_text(encoding="utf-8"))
    schema_enum = set(schema["properties"]["fallback"]["properties"]["reason"]["enum"])

    covered = {f.fallback_reason for f in REASON_FIXTURES}

    assert covered == schema_enum, (
        "fixtures.REASON_FIXTURES must cover exactly the fallback.reason enum "
        f"in meta.schema.json — missing={schema_enum - covered} "
        f"unexpected={covered - schema_enum}"
    )


def test_freshness_reason_is_deliberately_not_schema_constrained() -> None:
    """Taxonomy §3: freshness.reason rides along via additionalProperties, not an enum.

    Only `fallback.reason` is the closed set this kit's exhaustiveness gate
    binds to; asserting the schema shape here documents why the ground
    truth for freshness/consistency reasons is `metadata.py`'s code (read
    by hand into fixtures.py), not the schema.
    """
    schema = json.loads(META_SCHEMA_PATH.read_text(encoding="utf-8"))
    freshness_schema = schema["properties"]["freshness"]
    assert freshness_schema.get("additionalProperties") is True
    assert "reason" not in freshness_schema.get("properties", {})
    assert "reason" not in freshness_schema.get("required", [])


# ---------------------------------------------------------------------------
# 3. Payload-shape matrix (REQ-MCP-4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    PAYLOAD_SHAPE_FIXTURES,
    ids=[f.id for f in PAYLOAD_SHAPE_FIXTURES],
)
def test_payload_shape_fixture_selects_expected_decision(fixture: PayloadShapeFixture) -> None:
    assert decide_on_payload_shape(fixture.condition) == fixture.expected_decision


def test_payload_shape_covers_all_four_req_mcp_4_conditions() -> None:
    conditions = {f.condition for f in PAYLOAD_SHAPE_FIXTURES}
    assert conditions == {
        "response_invalid",
        "response_too_large",
        "missing_meta",
        "invalid_meta",
    }


# ---------------------------------------------------------------------------
# 4. Contract-pin matrix (canary.py fail-closed codes)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    CONTRACT_PIN_FIXTURES,
    ids=[f.id for f in CONTRACT_PIN_FIXTURES],
)
def test_contract_pin_fixture_selects_direct_source_fallback(
    fixture: ContractPinFixture,
) -> None:
    assert decide_on_contract_pin_failure(fixture.canary_code) == fixture.expected_decision
    assert fixture.expected_decision == "direct_source_fallback"


def test_contract_pin_failure_parks_when_direct_source_also_unavailable() -> None:
    decision = decide_on_contract_pin_failure(
        "contract_info_commit_mismatch",
        direct_source_available=False,
    )
    assert decision == "park"


def test_contract_pin_fixture_ids_are_unique() -> None:
    ids = [f.id for f in CONTRACT_PIN_FIXTURES]
    assert len(ids) == len(set(ids))


def test_contract_pin_fixtures_cover_every_canary_fail_closed_code() -> None:
    """AC-SEV-1 exhaustiveness gate for contract-pin codes (bp-review finding 1).

    Mirrors `test_fallback_reason_enum_is_covered_exactly_by_fixtures`:
    source-derived ground truth read from `canary.py` at test time (never
    imported as Python — out of this kit's scope), asserted against
    `CONTRACT_PIN_FIXTURES`. Must fail loudly, not pass silently, if
    `canary.py` is missing/unreadable or the regex sweep finds nothing —
    an empty derived set would otherwise trivially satisfy a superset
    check and mask the gate breaking.
    """
    text = CANARY_PATH.read_text(encoding="utf-8")
    literal_codes = set(_FAIL_CALL_RE.findall(text))
    assert literal_codes, (
        f'regex sweep of {CANARY_PATH} found zero literal _fail("...") '
        "call sites -- parsing broke or the file moved; this gate must "
        "fail loudly, not pass silently"
    )
    dynamic_codes = {f"contract_info_{field}_mismatch" for field in _DYNAMIC_CONTRACT_INFO_FIELDS}
    derived = literal_codes | dynamic_codes

    covered = {f.canary_code for f in CONTRACT_PIN_FIXTURES}
    missing = derived - covered
    assert not missing, (
        "CONTRACT_PIN_FIXTURES is missing canary.py fail-closed codes: "
        f"{sorted(missing)} -- add a ContractPinFixture entry for each"
    )


# ---------------------------------------------------------------------------
# 5. Fail-closed behaviour on schema/enum drift
# ---------------------------------------------------------------------------


def test_unknown_fallback_reason_string_fails_closed() -> None:
    """SPEC-EV REQ-EV-9 analogue: enum drift must never silently resolve to `use`."""
    meta = {
        "fallback": {"required": False, "reason": "some_future_reason_not_in_the_enum_yet"},
    }
    assert decide_from_meta(meta) == "direct_source_fallback"


def test_missing_fallback_block_fails_closed() -> None:
    assert decide_from_meta({}) == "direct_source_fallback"


def test_malformed_fallback_block_fails_closed() -> None:
    assert decide_from_meta({"fallback": "not-an-object"}) == "direct_source_fallback"


def test_required_false_with_known_nonnull_reason_fails_closed() -> None:
    """Defense-in-depth: `metadata.py:271` enforces `required == (reason is not None)`

    at the producer, so a real response can never emit this shape — but
    `decide_from_meta` must not trust `required` in isolation when it
    contradicts a known `reason`.
    """
    meta = {"fallback": {"required": False, "reason": "stale_source"}}
    assert decide_from_meta(meta) == "direct_source_fallback"


def test_required_true_with_null_reason_fails_closed() -> None:
    """Symmetric contradiction case — see the `required=False` test above."""
    meta = {"fallback": {"required": True, "reason": None}}
    assert decide_from_meta(meta) == "direct_source_fallback"


# ---------------------------------------------------------------------------
# 6. AC-SEV-4 oracle-boundary — these tests document *intent* (what a
#    conformant consumer must never do), not enforced behavior: real
#    enforcement lives in Omnius/SRE gate logic outside this repo and is
#    not verifiable here, same limitation as AC-SEV-2/3. `is_sole_oracle`
#    and `requires_independent_authority` are constant-return stubs; these
#    tests assert exactly the constant each is hardcoded to return.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "decision",
    ["use", "direct_source_fallback", "park", "planning_only"],
)
def test_no_decision_is_ever_a_sole_oracle_by_intent(decision: str) -> None:
    """`is_sole_oracle` takes no argument — the invariant it documents is
    decision-independent by construction. Parametrizing over `decision`
    here only records that every value in the vocabulary is covered by
    that same intent, not that the function inspects it."""
    del decision
    assert is_sole_oracle() is False


@pytest.mark.parametrize("action", TERMINAL_ACTIONS)
def test_terminal_actions_always_require_independent_authority_by_intent(action: str) -> None:
    assert requires_independent_authority(action) is True


def test_use_decision_does_not_bypass_the_authorization_gate() -> None:
    """The fitness gate (`decide_from_meta`) and the authorization gate are orthogonal.

    A conformant consumer reaching `use` on the fittest possible evidence
    (fresh, converged, pinnable contract) still may not merge, apply, act
    on an incident, or make any terminal decision from that alone — by
    intent, not verified enforcement (see the module comment above).
    """
    fresh_fixture = next(f for f in REASON_FIXTURES if f.expected_decision == "use")
    decision = decide_from_meta(fresh_fixture.meta())
    assert decision == "use"
    assert is_sole_oracle() is False
    for action in TERMINAL_ACTIONS:
        assert requires_independent_authority(action) is True
